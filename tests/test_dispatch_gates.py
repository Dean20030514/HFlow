"""The gates that must close before an invocation is bought (plan 6, batch A).

Three classes of defect are covered here, and all three share one property: the run is
*wrong before it starts*. A task that waives a review the project requires, a task whose
scope cannot be written, a task verified by a check that executes nothing, or a task whose
budget cannot cover the review it demands - each of these can only be discovered after an
implementation turn has been paid for if it is not refused up front.

The review-floor tests are the four boolean combinations, because the two flags are a floor
and a request rather than a switch and an override (plan 6).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import (
    BudgetRequest,
    CheckDef,
    ProjectConfig,
    ReviewRequirement,
    RefusalCode,
    RefusedError,
    RunRequest,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.packet import render_implementer_packet
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import unit_only, write_project, write_task


class RecordingDriver:
    """A driver that records every dispatch and produces no change of its own."""

    driver_id = "recording-driver"

    def __init__(self, *, review_verdict: str | None = None) -> None:
        self.invocations: list[str] = []
        self.review_verdict = review_verdict

    def probe(self, binding):  # noqa: ANN001, ANN201
        raise AssertionError("no probe is expected in these tests")

    def start(self, request):  # noqa: ANN001, ANN201
        from hflow.contracts import InvocationOutcome, InvocationResult, ReviewOutput

        self.invocations.append(request.role)
        review = (
            ReviewOutput(verdict=self.review_verdict, findings=[])
            if request.role == "reviewer" and self.review_verdict
            else None
        )
        return InvocationResult(
            invocation_id=request.invocation_id, outcome=InvocationOutcome.COMPLETED, review=review
        )

    def cancel(self, invocation_id: str):  # noqa: ANN201
        raise AssertionError("no cancellation is expected in these tests")

    def reconcile(self, invocation_id: str):  # noqa: ANN201
        raise AssertionError("no reconciliation is expected in these tests")


def _project(project: ProjectConfig, *, review_required: bool) -> ProjectConfig:
    return project.model_copy(update={"review_required": review_required})


def _git_repo(workspace: Path) -> str:
    """Turn the workspace fixture into a real one-commit Git repository.

    The production gates now validate the base commit and derive the worktree path *before* the
    run exists, so a worktree task needs an actual repository. Without one, a gate test would
    fail on "not a git repository" and could pass for the wrong reason.
    """
    def git(*args: str) -> str:
        import subprocess

        completed = subprocess.run(  # noqa: S603 - fixed argv, test fixture only
            ["git", *args], cwd=str(workspace), capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "hflow@example.invalid")
    git("config", "user.name", "HFlow Test")
    git("add", ".")
    git("commit", "-q", "-m", "baseline")
    return git("rev-parse", "HEAD")


def _task(task_spec: TaskSpec, *, review_required: bool) -> TaskSpec:
    return task_spec.model_copy(update={"review": ReviewRequirement(required=review_required)})


def _controller(
    store: Store,
    driver: RecordingDriver,
    tmp_path: Path,
    *,
    production: bool,
    runners: CheckRunners | None = None,
) -> Controller:
    return Controller(
        store,
        driver,
        controller_build="gate-test",
        runners=runners or CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=tmp_path / "data",
        production=production,
    )


# --------------------------------------------------------------------------
# 1. the review floor and the review request
# --------------------------------------------------------------------------


def test_review_runs_unless_both_the_project_and_the_task_say_no(
    project: ProjectConfig, task_spec: TaskSpec
) -> None:
    """The four combinations, as one table: neither flag may silently cancel the other."""
    cases = {
        (True, True): True,  # the project floor and the task request agree
        (True, False): True,  # the project floor wins; admission refuses this spec anyway
        (False, True): True,  # the task asks for a review the project does not require
        (False, False): False,  # the one combination that may skip the review
    }
    for (project_requires, task_requests), expected in cases.items():
        spec = _task(task_spec, review_required=task_requests)
        assert spec.needs_review(_project(project, review_required=project_requires)) is expected, (
            f"project={project_requires} task={task_requests}"
        )


def test_a_task_that_waives_a_required_review_is_refused(
    project: ProjectConfig, task_spec: TaskSpec
) -> None:
    """The project's requirement is a floor: a task cannot waive it (unchanged behaviour)."""
    from hflow.admission import validate_task_spec

    report = validate_task_spec(_task(task_spec, review_required=False), project, Path.cwd())

    assert not report.ok
    assert RefusalCode.RISK_DOWNGRADE in {issue.code for issue in report.issues}
    assert any(issue.location == "review.required" for issue in report.issues)


def test_a_task_that_asks_for_a_review_actually_gets_one(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path
) -> None:
    """End to end: project floor off, task request on => the reviewer invocation happens."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=False)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(task_spec, review_required=True),
                project=_project(project, review_required=False),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert driver.invocations == ["implementer", "reviewer"], driver.invocations
    assert outcome.reviewer_invocations == 1, "a review the task asked for must not be skipped"


def test_both_saying_no_skips_the_review(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path
) -> None:
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=False)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(task_spec, review_required=False),
                project=_project(project, review_required=False),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert driver.invocations == ["implementer"], driver.invocations
    assert outcome.receipt is not None
    assert outcome.receipt.review.status == "not_required"


# --------------------------------------------------------------------------
# 2. the production gates in front of the first invocation
# --------------------------------------------------------------------------


def test_a_write_task_without_a_worktree_is_refused_before_dispatch(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path
) -> None:
    """An in-place run would write into the user's own checkout: refuse, do not try."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=True)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=unit_only(task_spec),  # default workspace mode is in_place
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        # Read while the store is open: a refused admission must leave no run state at all.
        runs_after = store.list_runs()
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.SCOPE_VIOLATION
    assert "worktree" in excinfo.value.message
    assert driver.invocations == [], "nothing may be dispatched for a task that cannot run"
    assert runs_after == [], "a refused task must not leave run state"


def test_a_write_task_without_the_write_opt_in_is_refused_before_dispatch(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """Writes off plus a write scope is a known-bad pair: it would spend a turn and then fail."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.delenv(ENV_ALLOW_WRITES, raising=False)
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=True)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.SCOPE_VIOLATION
    assert ENV_ALLOW_WRITES in excinfo.value.message
    assert driver.invocations == []


def test_a_reviewed_task_whose_budget_covers_one_turn_is_refused(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """A review is its own invocation: a budget of one turn cannot reach an accepted run."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={
            "budget": BudgetRequest(max_agent_turns=1, max_repair_cycles=0),
            "workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root)),
        }
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=True)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.BUDGET_EXCEEDED
    assert "review" in excinfo.value.message
    assert driver.invocations == []


def test_the_budget_gate_only_applies_when_a_review_is_actually_required(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """One turn is enough when neither the project nor the task asks for a review."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = _task(unit_only(task_spec), review_required=False).model_copy(
        update={
            "budget": BudgetRequest(max_agent_turns=1, max_repair_cycles=0),
            "workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root)),
        }
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(
        store, driver, tmp_path, production=True, runners=CheckRunners.offline_default()
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=_command_project(_project(project, review_required=False)),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    # One turn was enough: the implementer ran and the review never happened, so the gate did
    # not refuse a run it has no claim on. (The stub reviewer is not reached at all here.)
    assert driver.invocations == ["implementer"], driver.invocations
    assert outcome.receipt is not None, outcome.block_reason
    assert outcome.receipt.review.status == "not_required"
    assert outcome.block_code is None


# --------------------------------------------------------------------------
# 3. the authorization's *remaining* allowance
# --------------------------------------------------------------------------


def _authorization(tmp_path: Path, spec: TaskSpec, *, max_submissions: int):
    """A user-provenance artifact for this spec, as the CLI would have verified it."""
    from hflow.authorization import AuthorizationBinding, AuthorizationRecord

    return AuthorizationRecord(
        authorization_id="AUTH-gate-test",
        user_text="I approve one supervised run of this exact task.",
        authorized_at="2026-09-23T00:00:00Z",
        max_top_level_submissions=max_submissions,
        binding=AuthorizationBinding(
            mode="m2-live-change",
            driver="recording-driver",
            project_id="demo-project",
            repo_path=str(tmp_path),
            base_commit="0" * 40,
            spec_digest=spec.spec_digest(),
            spec_path=str(tmp_path / "task.json"),
        ),
    )


def _authorized_controller(
    store: Store,
    driver: RecordingDriver,
    tmp_path: Path,
    authorization,
    *,
    runners: CheckRunners | None = None,
) -> Controller:
    return Controller(
        store,
        driver,
        controller_build="gate-test",
        runners=runners or CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=tmp_path / "data",
        authorization=authorization,
        preflight=lambda: (True, "the pre-flight is not what this test measures"),
        production=True,
    )


def test_a_reviewed_task_is_refused_when_the_authorization_cannot_cover_both(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """The reported gap: 2 turns allowed by the task, 1 submission left in the authorization."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    authorization = _authorization(tmp_path, spec, max_submissions=1)
    controller = _authorized_controller(store, driver, tmp_path, authorization)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        runs_after = store.list_runs()
        state = store.authorization_state("AUTH-gate-test")
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED
    assert "1/1" in excinfo.value.message and "2" in excinfo.value.message
    assert driver.invocations == [], "the implementer must not run when the review cannot be bought"
    assert runs_after == [], "nothing may be created for a dispatch the authorization cannot cover"
    assert state is None, "the artifact must not even be registered for a refused dispatch"


def test_a_reviewed_task_is_refused_when_one_submission_was_already_used(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """max=2 with one already used is the same shortfall, and must be refused the same way."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    store.register_authorization(_authorization(tmp_path, spec, max_submissions=2).as_store_record())
    store.claim_authorized_submission("AUTH-gate-test")  # one submission already spent
    driver = RecordingDriver()
    controller = _authorized_controller(
        store, driver, tmp_path, _authorization(tmp_path, spec, max_submissions=2)
    )
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        used = store.authorization_state("AUTH-gate-test")["used_top_level_submissions"]
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED
    assert "1/2" in excinfo.value.message
    assert driver.invocations == []
    assert used == 1, "a refused dispatch must not consume another submission"


def test_a_reviewed_task_runs_when_the_authorization_covers_the_whole_loop(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """The same task with two submissions left is admitted and spends exactly two."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(review_verdict="accepted")
    authorization = _authorization(tmp_path, spec, max_submissions=2)
    controller = _authorized_controller(
        store, driver, tmp_path, authorization, runners=CheckRunners.offline_default()
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=_command_project(project),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        used = store.authorization_state("AUTH-gate-test")["used_top_level_submissions"]
    finally:
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert driver.invocations == ["implementer", "reviewer"]
    assert used == 2, "one submission per invocation, implementer and reviewer"


def test_an_identical_finished_task_returns_history_even_with_a_spent_authorization(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path
) -> None:
    """A history query is not a new dispatch: an exhausted artifact must not turn it into a refusal."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(review_verdict="accepted")
    first = _controller(
        store, driver, tmp_path, production=False, runners=CheckRunners.offline_default()
    )
    try:
        original = first.run_task(
            RunRequest(
                task=task_spec,
                project=_command_project(project),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        assert original.task_state is TaskState.ACCEPTED, original.block_reason

        # The same spec is now submitted through a controller whose artifact is fully spent.
        # (A real driver needs real approved checks; the fake-check gate is a different rule.
        # The spec itself must be byte-identical, or it would be a different run.)
        spent = _authorization(tmp_path, task_spec, max_submissions=1)
        store.register_authorization(spent.as_store_record())
        store.claim_authorized_submission("AUTH-gate-test")
        again = _authorized_controller(
            store,
            driver,
            tmp_path,
            spent,
            runners=CheckRunners.offline_default(),
        )
        outcome = again.run_task(
            RunRequest(
                task=task_spec,
                project=_command_project(project),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        used = store.authorization_state("AUTH-gate-test")["used_top_level_submissions"]
    finally:
        store.close()

    assert outcome.run_id == original.run_id
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert driver.invocations == ["implementer", "reviewer"], "no second dispatch may happen"
    assert used == 1, "returning recorded history must not consume an allowance"


# --------------------------------------------------------------------------
# 4. the packet bound is checked against the packet that would be sent
# --------------------------------------------------------------------------


def _goal_filling_the_bound(spec: TaskSpec, placeholder: str, real_workspace: str) -> str:
    """A goal that fits the packet bound only while the workspace path stays short.

    Measured against the real renderer, not estimated: the packet is rendered once with an empty
    goal, and the goal is padded to just fill the bound for ``placeholder`` plus a margin. That
    makes the case about the *path* the run actually gets, not about an oversized goal.
    """
    from hflow.packet import MAX_PACKET_BYTES

    empty = render_implementer_packet(
        task_id=spec.task_id,
        task_revision=spec.revision,
        goal="",
        acceptance=spec.acceptance,
        scope=spec.scope,
        workspace=placeholder,
        spec_digest=spec.spec_digest(),
        deadline_seconds=900,
        writes_allowed=True,
    )
    assert not real_workspace.startswith(placeholder) or len(real_workspace) > len(placeholder), (
        "the real workspace must be longer than the placeholder for this case to mean anything"
    )
    filler = "x" * (MAX_PACKET_BYTES - empty.byte_length - 64)
    return "This goal is deliberately sized against the packet bound. " + filler


def test_an_over_long_packet_is_refused_before_any_allowance_is_consumed(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """The reported gap: the placeholder path fit, the real worktree path did not, and a
    submission had already been claimed. The packet that is checked is now the packet sent."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES
    from hflow.gitworkspace import GitRepo
    from hflow.packet import MAX_PACKET_BYTES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    base_commit = _git_repo(project_root)
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(review_verdict="accepted")
    authorization = _authorization(tmp_path, task_spec, max_submissions=2)
    controller = _authorized_controller(
        store, driver, tmp_path, authorization, runners=CheckRunners.offline_default()
    )
    placeholder = "<workspace assigned at dispatch>"
    try:
        store.register_authorization(authorization.as_store_record())
        # The real worktree path this run will use. ``run_task`` chooses the run id, so the goal
        # is sized against the same placeholder path the old pre-check used.
        repo = GitRepo.discover(project_root)
        probe_workspace = str(repo.worktree_parent() / ("R-" + "0" * 10))
        assert len(probe_workspace) > len(placeholder)

        sized = unit_only(task_spec).model_copy(
            update={"workspace": WorkspaceSpec(mode="worktree", base_commit=base_commit)}
        )
        spec = sized.model_copy(
            update={"goal": _goal_filling_the_bound(sized, placeholder, probe_workspace)}
        )

        # Before the fix this goal passed the pre-check (which used the placeholder) and failed
        # at dispatch, after a submission had been claimed.
        placeholder_packet = render_implementer_packet(
            task_id=spec.task_id,
            task_revision=spec.revision,
            goal=spec.goal,
            acceptance=spec.acceptance,
            scope=spec.scope,
            workspace=placeholder,
            spec_digest=spec.spec_digest(),
            deadline_seconds=900,
            writes_allowed=True,
        )
        assert placeholder_packet.byte_length <= MAX_PACKET_BYTES, (
            "this case must fail on the real path, not on a goal that is simply too long"
        )

        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=_command_project(project),
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        runs_after = store.list_runs()
        used = store.authorization_state("AUTH-gate-test")["used_top_level_submissions"]
        turns = None
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.INVALID_SPEC
    assert "does not fit" in excinfo.value.message
    assert driver.invocations == [], "no driver call may happen for a packet that does not fit"
    assert runs_after == [], "no run may be created for a packet that does not fit"
    assert used == 0, "no allowance may be consumed for a packet that does not fit"
    assert turns is None


def test_the_sent_packet_is_the_checked_packet(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """A packet that fits is sent unchanged, and the recorded digest matches the driver's."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(review_verdict="accepted")
    controller = _controller(
        store, driver, tmp_path, production=True, runners=CheckRunners.offline_default()
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=_command_project(project),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        notes = list(store.notes_for(outcome.run_id))
    finally:
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    packet_notes = [note for note in notes if "role_input_packet" in note]
    assert any("role=implementer" in note for note in packet_notes)
    assert any("role=reviewer" in note for note in packet_notes)
    assert not any("MISMATCH" in note for note in notes), notes




def _command_project(project: ProjectConfig) -> ProjectConfig:
    """The same project contract with every check turned into a real command.

    The IDs are kept: a task's acceptance criteria reference them, and dropping one would turn a
    gate test into an unknown-check refusal.
    """
    import sys

    return project.model_copy(
        update={
            "checks": [
                CheckDef(id=check.id, kind="command", argv=[sys.executable, "-c", "pass"])
                for check in project.checks
            ]
        }
    )


def test_a_real_run_refuses_a_fake_check_before_creating_anything(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """``kind=fake`` executes nothing: accepting it would claim a verification that never ran."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=True)
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=spec,
                    project=project,  # its only checks are kind=fake
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        runs_after = store.list_runs()
    finally:
        store.close()

    assert excinfo.value.code is RefusalCode.NOT_IMPLEMENTED
    assert "fake" in excinfo.value.message
    assert driver.invocations == []
    assert runs_after == [], "a refused task must not leave run state"


def test_an_offline_run_may_still_use_a_fake_check(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path
) -> None:
    """The offline path keeps its fake checks: this is a delivery rule, not a ban on fixtures."""
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver()
    controller = _controller(store, driver, tmp_path, production=False)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    # The point is that admission did not refuse it: a fake check is refused only for a
    # production run. (This driver answers with no verdict, so the run itself may still block.)
    assert outcome.block_code is not RefusalCode.NOT_IMPLEMENTED, outcome.block_reason
    assert driver.invocations[:1] == ["implementer"]


def test_a_command_check_project_is_not_refused_by_the_fake_check_gate(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, monkeypatch
) -> None:
    """The gate must be about the check kind, not about being a production run at all."""
    from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES

    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    spec = unit_only(task_spec).model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit=_git_repo(project_root))}
    )
    store = Store(tmp_path / "hflow.sqlite")
    driver = RecordingDriver(review_verdict="accepted")
    controller = _controller(
        store, driver, tmp_path, production=True, runners=CheckRunners.offline_default()
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=_command_project(project),
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    # The run got past admission (its check is a real command, its budget covers the loop, its
    # base commit exists), so neither the fake-check nor the budget gate fired. A refusal here
    # would mean a stricter gate than intended.
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.receipt is not None
    assert driver.invocations == ["implementer", "reviewer"]


# --------------------------------------------------------------------------
# 6. the same refusals through the CLI, where the production flag is decided
# --------------------------------------------------------------------------


def test_the_cli_refuses_a_fake_check_for_a_real_driver(
    tmp_path: Path,
    project: ProjectConfig,
    task_spec: TaskSpec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--driver acpx-dsh`` means a delivery, so its approved checks must be real ones."""
    from hflow.cli import EXIT_REFUSED, main

    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    authorization = tmp_path / "auth.json"
    authorization.write_text(json.dumps({"user_text": "not reached"}), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--task",
            str(task_file),
            "--project",
            str(project_file),
            "--project-root",
            str(project_root),
            "--driver",
            "acpx-dsh",
            "--authorization-file",
            str(authorization),
            "--json",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )

    assert exit_code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True
    assert payload["issues"][0]["code"] == "not_implemented"
    assert payload["issues"][0]["location"] == "checks.unit"


def test_the_cli_still_runs_a_fake_check_offline(
    tmp_path: Path,
    project: ProjectConfig,
    task_spec: TaskSpec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--driver fake`` is the offline driver: its fake checks stay usable."""
    from hflow.cli import main

    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"src/parser.py": "def parse(text):\n    return text\n"}), encoding="utf-8"
    )

    exit_code = main(
        [
            "run",
            "--task",
            str(task_file),
            "--project",
            str(project_file),
            "--project-root",
            str(project_root),
            "--fake-write-plan",
            str(plan),
            "--json",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )

    assert exit_code == 0, capsys.readouterr()
