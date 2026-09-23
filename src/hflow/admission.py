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

#: The one delivery level this build implements. Everything else is refused up front rather
#: than silently delivered at this level.
_IMPLEMENTED_DELIVERY = "local_candidate"


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
        # Reusing or adapting a component is only decided once its compatibility question is
        # answered (plan 9.3). A *failed* fit test is a decision to change component or build,
        # never a reason to keep choice=reuse and dispatch anyway - reporting it as a pass
        # would be a false statement about an experiment that already ran.
        if reuse.choice in {"reuse", "adapt"} and reuse.fit_test_status in {"pending", "failed"}:
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail=(
                        f"reuse.choice={reuse.choice} with required_fit_test "
                        f"{reuse.fit_test_status!r} cannot dispatch: the compatibility question "
                        "must be answered by a recorded result before the component is adopted"
                    ),
                    location="reuse.fit_test_status",
                )
            )
        # Claiming no fit test is needed is a claim, so it carries its own short argument
        # (plan 9.3). Without one, "not_required" is indistinguishable from a forgotten test.
        if (
            reuse.choice in {"reuse", "adapt"}
            and reuse.fit_test_status == "not_required"
            and not reuse.reason.strip()
        ):
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail=(
                        f"reuse.choice={reuse.choice} without a required_fit_test must state, in "
                        "reuse.reason, why the component is already proven to fit this use"
                    ),
                    location="reuse.reason",
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

    # --- delivery level ---------------------------------------------------
    # This build can only deliver a local candidate. Asking for integrated/published used to
    # be a warning, which meant a run "succeeded" while the requested delivery level was never
    # reached - a success exit masking an unmet requirement. It is a refusal now: reaching a
    # lower level than requested needs an explicit operator decision, and there is no
    # auto-downgrade in this build (the run row keeps the requested mode, so a later decision
    # can still be recorded against what was actually asked for).
    if spec.delivery.mode != _IMPLEMENTED_DELIVERY:
        issues.append(
            ValidationIssue(
                code=RefusalCode.NOT_IMPLEMENTED,
                detail=(
                    f"delivery.mode={spec.delivery.mode!r} is not implemented by this build; only "
                    f"{_IMPLEMENTED_DELIVERY!r} is. Nothing was dispatched, so no lower delivery "
                    "level will be presented as the requested one."
                ),
                location="delivery.mode",
            )
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
