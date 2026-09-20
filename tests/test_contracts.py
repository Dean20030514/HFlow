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
