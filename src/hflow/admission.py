"""Task admission: the deterministic gate in front of every dispatch (plan 11, 16.1).

Everything here is a pure function of the TaskSpec, the project contract, and the
run's already-granted budget. No model is involved, so ``status``/``report`` and
the gate itself cost nothing.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import (
    ProjectConfig,
    ReuseStatus,
    RefusalCode,
    RefusedError,
    TaskSpec,
    ValidationIssue,
    ValidationReport,
)
from .store import SCHEMA_VERSION as STORE_SCHEMA_VERSION
from .workspace import check_scope

# Ordered from least to most demanding; a task may not lower the project's floor.
_RISK_ORDER = {"low": 0, "standard": 1, "strict": 2}


def validate_task_spec(
    spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> ValidationReport:
    """Return every admission problem at once, instead of failing one at a time."""
    issues: list[ValidationIssue] = []
    warnings: list[str] = []

    if spec.schema_version != STORE_SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                code=RefusalCode.INVALID_SPEC,
                detail=(
                    f"schema_version {spec.schema_version} is not supported by this runtime "
                    f"(expected {STORE_SCHEMA_VERSION})"
                ),
                location="schema_version",
            )
        )

    if spec.task_id.strip() != spec.task_id or not spec.task_id.strip():
        issues.append(
            ValidationIssue(
                code=RefusalCode.INVALID_SPEC,
                detail="task_id must be a non-empty identifier without surrounding whitespace",
                location="task_id",
            )
        )

    known_checks = project.check_map()
    for criterion in spec.acceptance:
        for check_id in criterion.check_ids:
            if check_id not in known_checks:
                issues.append(
                    ValidationIssue(
                        code=RefusalCode.UNKNOWN_CHECK,
                        detail=(
                            f"acceptance {criterion.id!r} references check {check_id!r}, "
                            "which is not an approved check in the project contract"
                        ),
                        location=f"acceptance.{criterion.id}",
                    )
                )

    for problem in check_scope(spec.scope, project_root, project.write_deny):
        issues.append(
            ValidationIssue(code=RefusalCode.SCOPE_VIOLATION, detail=problem, location="scope")
        )

    # --- reuse admission (plan 11.1/11.3) ---------------------------------
    reuse = spec.reuse
    if reuse.status == ReuseStatus.DECIDED:
        if reuse.choice == "none":
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail="reuse.status=decided requires an explicit choice",
                    location="reuse.choice",
                )
            )
        if not reuse.need.strip():
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail="new-component tasks must state what capability is needed",
                    location="reuse.need",
                )
            )
        if reuse.choice == "reuse" and reuse.fit_test_status == "pending":
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail=(
                        "reuse.choice=reuse with an unrun required_fit_test cannot dispatch: "
                        "the compatibility question must be answered first"
                    ),
                    location="reuse.fit_test_status",
                )
            )
    elif reuse.status == ReuseStatus.EXISTING_DECISION and not reuse.reference.strip():
        issues.append(
            ValidationIssue(
                code=RefusalCode.REUSE_NOT_APPROVED,
                detail="existing_decision requires a reference to the recorded decision",
                location="reuse.reference",
            )
        )

    # --- budget authorization --------------------------------------------
    if spec.budget.max_agent_turns > project.limits.max_agent_turns:
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    f"task requests {spec.budget.max_agent_turns} agent turns but the project "
                    f"pre-authorizes at most {project.limits.max_agent_turns}"
                ),
                location="budget.max_agent_turns",
            )
        )
    if spec.budget.max_repair_cycles > project.limits.max_repair_cycles:
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    f"task requests {spec.budget.max_repair_cycles} repair cycles but the project "
                    f"pre-authorizes at most {project.limits.max_repair_cycles}"
                ),
                location="budget.max_repair_cycles",
            )
        )

    # --- risk floor -------------------------------------------------------
    if _RISK_ORDER[spec.risk] < _RISK_ORDER[project.min_risk_for_review]:
        issues.append(
            ValidationIssue(
                code=RefusalCode.RISK_DOWNGRADE,
                detail=(
                    f"task risk {spec.risk!r} is below the project minimum "
                    f"{project.min_risk_for_review!r}"
                ),
                location="risk",
            )
        )
    if project.review_required and not spec.review.required:
        issues.append(
            ValidationIssue(
                code=RefusalCode.RISK_DOWNGRADE,
                detail="this project requires independent review; a task cannot waive it",
                location="review.required",
            )
        )

    if spec.delivery.mode != "local_candidate":
        warnings.append(
            "delivery.mode requests integration/publish, which M1 does not implement; "
            "the run will stop at LOCAL_CANDIDATE"
        )

    return ValidationReport(
        ok=not issues, issues=issues, spec_digest=spec.spec_digest(), warnings=warnings
    )


def assert_admissible(spec: TaskSpec, project: ProjectConfig, project_root: Path) -> None:
    """``validate_task_spec`` as a fail-fast refusal, used before creating a run."""
    report = validate_task_spec(spec, project, project_root)
    if not report.ok:
        first = report.issues[0]
        raise RefusedError(first.code, first.detail)
