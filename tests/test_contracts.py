"""Admission contract: what the controller refuses *before* spending anything (plan 16.1).

Every test here is a pure function of the TaskSpec plus the project contract. If one
of these fails, the failure is an admission-policy defect, not a model-quality issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hflow.admission import validate_task_spec
from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    ProjectConfig,
    ProjectLimits,
    RefusalCode,
    ReuseDecision,
    ReuseStatus,
    ReviewRequirement,
    Scope,
    TaskSpec,
    json_schema,
)
from hflow.workspace import check_scope, resolve_within


def _codes(spec: TaskSpec, project: ProjectConfig, root: Path) -> set[RefusalCode]:
    return {issue.code for issue in validate_task_spec(spec, project, root).issues}


def test_valid_spec_is_admitted(task_spec: TaskSpec, project: ProjectConfig, project_root: Path) -> None:
    report = validate_task_spec(task_spec, project, project_root)
    assert report.ok, [issue.detail for issue in report.issues]
    assert report.spec_digest == task_spec.spec_digest()


def test_unknown_check_is_refused(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(
        update={
            "acceptance": [
                AcceptanceCriterion(id="AC-1", statement="x", check_ids=["not-approved"])
            ]
        }
    )
    assert RefusalCode.UNKNOWN_CHECK in _codes(spec, project, project_root)


def test_budget_above_project_authorization_is_refused(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(update={"budget": BudgetRequest(max_agent_turns=9, max_repair_cycles=1)})
    assert RefusalCode.BUDGET_EXCEEDED in _codes(spec, project, project_root)


def test_scope_escape_is_refused(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(
        update={"scope": Scope(write_allow=["../outside/evil.py"])}
    )
    assert RefusalCode.SCOPE_VIOLATION in _codes(spec, project, project_root)


def test_scope_may_not_touch_project_control_files(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    """The workflow cannot rewrite its own rules or CI while a run is in flight (A30)."""
    spec = task_spec.model_copy(
        update={"scope": Scope(write_allow=[".hflow/project.json", ".github/workflows/ci.yml"])}
    )
    codes = _codes(spec, project, project_root)
    assert RefusalCode.SCOPE_VIOLATION in codes


def test_reuse_gate_blocks_unproven_reuse(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(
        update={
            "reuse": ReuseDecision(
                status=ReuseStatus.DECIDED,
                need="structured automation transport",
                choice="reuse",
                required_fit_test="DSH ACP single-task interop",
                fit_test_status="pending",
            )
        }
    )
    assert RefusalCode.REUSE_NOT_APPROVED in _codes(spec, project, project_root)


def _decided(**kwargs: object) -> ReuseDecision:
    """A decided reuse record with a stated need, plus whatever the case under test varies."""
    base: dict[str, object] = {
        "status": ReuseStatus.DECIDED,
        "need": "structured automation transport",
        "reason": "reuses the existing transport instead of writing a second client",
    }
    return ReuseDecision(**{**base, **kwargs})


#: choice / fit_test_status / reason -> is this combination admissible? (plan 9.3)
REUSE_FIT_CASES: list[tuple[str, str, str, bool]] = [
    # An unanswered or failed compatibility question is not a decision to adopt the component.
    ("reuse", "pending", "", False),
    ("adapt", "pending", "", False),
    ("reuse", "failed", "", False),
    # The same failure on `adapt` was previously admissible: adapting is still adoption.
    ("adapt", "failed", "", False),
    # An answered question, either way, is what the gate asks for.
    ("reuse", "passed", "", True),
    ("adapt", "passed", "", True),
    # Claiming no test is needed is a claim, so it needs its short argument.
    ("reuse", "not_required", "identical API, already used by this module", True),
    ("adapt", "not_required", "same library, only the call signature is adapted", True),
    ("reuse", "not_required", "", False),
    ("adapt", "not_required", "", False),
    # Choosing something other than reuse/adapt is not gated on being right about reuse:
    # `build`/`defer` may legitimately follow a failed fit test (plan 9.3).
    ("build", "failed", "", True),
    ("build", "pending", "", True),
    ("defer", "pending", "", True),
]


@pytest.mark.parametrize(
    ("choice", "fit_test_status", "reason", "admissible"), REUSE_FIT_CASES, ids=str
)
def test_reuse_fit_test_combination_decides_admission(
    task_spec: TaskSpec,
    project: ProjectConfig,
    project_root: Path,
    choice: str,
    fit_test_status: str,
    reason: str,
    admissible: bool,
) -> None:
    spec = task_spec.model_copy(
        update={
            "reuse": _decided(
                choice=choice,
                required_fit_test="DSH ACP single-task interop",
                fit_test_status=fit_test_status,
                reason=reason,
            )
        }
    )
    report = validate_task_spec(spec, project, project_root)
    assert report.ok is admissible
    if admissible:
        assert report.issues == []
        return
    # A refusal has to say which field is wrong, not only that something is.
    expected_location = (
        "reuse.fit_test_status"
        if fit_test_status in {"pending", "failed"}
        else "reuse.reason"
    )
    assert [(issue.code, issue.location) for issue in report.issues] == [
        (RefusalCode.REUSE_NOT_APPROVED, expected_location)
    ]


@pytest.mark.parametrize(
    "reuse",
    [
        ReuseDecision(status=ReuseStatus.NOT_REQUIRED),
        ReuseDecision(status=ReuseStatus.EXEMPT, reason="wording only, no component choice"),
        ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:parser-library-choice",
            reason="the recorded decision still covers this use",
        ),
        ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:transport-choice",
            choice="reuse",
            required_fit_test="acpx one-shot round trip",
            fit_test_status="passed",
        ),
    ],
)
def test_legal_reuse_records_stay_admissible(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path, reuse: ReuseDecision
) -> None:
    """The new gate must not turn already-legal records into refusals (plan 9.3)."""
    spec = task_spec.model_copy(update={"reuse": reuse})
    report = validate_task_spec(spec, project, project_root)
    assert report.ok, [issue.detail for issue in report.issues]


@pytest.mark.parametrize(
    "reuse",
    [
        ReuseDecision(status=ReuseStatus.EXISTING_DECISION),
        ReuseDecision(status=ReuseStatus.EXISTING_DECISION, reference="   "),
        ReuseDecision(status=ReuseStatus.DECIDED, need="a transport", choice="none"),
        ReuseDecision(status=ReuseStatus.DECIDED, need="   ", choice="build", reason="r"),
    ],
)
def test_reuse_records_that_were_already_refused_still_are(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path, reuse: ReuseDecision
) -> None:
    spec = task_spec.model_copy(update={"reuse": reuse})
    assert RefusalCode.REUSE_NOT_APPROVED in _codes(spec, project, project_root)


@pytest.mark.parametrize("mode", ["integrated", "published"])
def test_unsupported_delivery_level_is_refused_not_warned(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path, mode: str
) -> None:
    """A lower delivery than requested must not be reported as the requested one (plan 2.4 E)."""
    spec = task_spec.model_copy(update={"delivery": DeliveryRequirement(mode=mode)})
    report = validate_task_spec(spec, project, project_root)
    assert not report.ok
    assert RefusalCode.NOT_IMPLEMENTED in {issue.code for issue in report.issues}
    assert not report.warnings, "an unmet delivery requirement is a refusal, not a note"
    assert [issue.location for issue in report.issues] == ["delivery.mode"]


def test_local_candidate_delivery_is_admitted_without_comment(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    spec = task_spec.model_copy(update={"delivery": DeliveryRequirement(mode="local_candidate")})
    report = validate_task_spec(spec, project, project_root)
    assert report.ok
    assert report.warnings == []


def test_reuse_gate_allows_explicit_exemption(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    """A docs-only change may be exempted; the record still has to exist (A14)."""
    spec = task_spec.model_copy(
        update={"reuse": ReuseDecision(status=ReuseStatus.EXEMPT, reason="wording only, no component choice")}
    )
    assert validate_task_spec(spec, project, project_root).ok


def test_risk_downgrade_and_review_waiver_are_refused(
    task_spec: TaskSpec, project: ProjectConfig, project_root: Path
) -> None:
    strict_project = project.model_copy(update={"min_risk_for_review": "strict"})
    assert RefusalCode.RISK_DOWNGRADE in _codes(task_spec, strict_project, project_root)

    waived = task_spec.model_copy(update={"review": ReviewRequirement(required=False)})
    assert RefusalCode.RISK_DOWNGRADE in _codes(waived, project, project_root)


def test_empty_goal_or_duplicate_acceptance_ids_never_construct() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="T-2",
            goal="   ",
            acceptance=[AcceptanceCriterion(id="AC-1", statement="x", check_ids=["unit"])],
            scope=Scope(write_allow=["src/a.py"]),
            reuse=ReuseDecision(status=ReuseStatus.EXEMPT),
        )
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id="T-3",
            goal="valid goal",
            acceptance=[
                AcceptanceCriterion(id="AC-1", statement="x", check_ids=["unit"]),
                AcceptanceCriterion(id="AC-1", statement="y", check_ids=["unit"]),
            ],
            scope=Scope(write_allow=["src/a.py"]),
            reuse=ReuseDecision(status=ReuseStatus.EXEMPT),
        )


def test_command_check_requires_argv() -> None:
    with pytest.raises(ValidationError):
        CheckDef(id="lint", kind="command")


def test_unknown_fields_are_rejected_not_ignored() -> None:
    """An agent inventing `"task_state": "ACCEPTED"` in a spec must fail loudly."""
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "task_id": "T-4",
                "goal": "g",
                "acceptance": [{"id": "AC-1", "statement": "s", "check_ids": ["unit"]}],
                "scope": {"write_allow": ["src/a.py"]},
                "reuse": {"status": "exempt"},
                "task_state": "ACCEPTED",
            }
        )


def test_profile_rejects_unknown_role_binding() -> None:
    with pytest.raises(ValidationError):
        from hflow.contracts import MachineProfile

        MachineProfile(
            profile_id="p",
            role_bindings={"reviewer": "missing-agent"},
            agents={},
        )


def test_schema_is_generated_from_the_models() -> None:
    schema = json_schema(TaskSpec)
    assert schema["title"] == "TaskSpec"
    assert "goal" in schema["properties"]
    assert schema.get("additionalProperties") is False
    # A second hand-written schema would drift; the generated one is the only copy.
    assert json_schema(ProjectConfig)["title"] == "ProjectConfig"


def test_path_resolution_refuses_junctions_and_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    for bad in ("C:/Windows/system32/x.txt", "/etc/passwd", "sub/../../escape.txt"):
        with pytest.raises(Exception):
            resolve_within(root, bad)

    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this environment cannot create directory links")
    with pytest.raises(Exception):
        resolve_within(root, "link/escaped.py")


def test_scope_problems_report_deny_overlap(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    problems = check_scope(Scope(write_allow=[".hflow/project.json"]), root, [".hflow/**"])
    assert problems and "write_deny" in problems[0]


def test_checks_digest_changes_when_approved_commands_change(project: ProjectConfig) -> None:
    changed = project.model_copy(
        update={"checks": [*project.checks, CheckDef(id="extra", kind="fake")]}
    )
    assert project.checks_digest() != changed.checks_digest()


def test_project_limits_must_be_sane() -> None:
    with pytest.raises(ValidationError):
        ProjectLimits(max_agent_turns=0)
