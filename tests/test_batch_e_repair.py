"""Batch E2 acceptance: one bounded business repair, end to end and offline.

The design is `docs/batch-e-plan.md` §5 and the acceptance list is §7 E2. These tests drive the
real `Controller.run_task` with the offline fake driver against a real one-commit Git project, so
the whole path is exercised: the worst-case budget gate, the two allowed triggers, the repair
round on the same worktree, fresh checks, a fresh review, and every way the run must not spend a
second attempt.

No model is called. The fake driver's write plan and the offline runner's verdicts are scripted by
the test, which is what makes these acceptance cases offline: they assert HFlow's own decisions.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    ProjectConfig,
    ProjectLimits,
    RefusalCode,
    RepairDecision,
    RepairPolicy,
    RepairTrigger,
    ReviewOutput,
    ReviewRequirement,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.contracts import RefusedError
from hflow.contracts import AttemptState, CancellationReceipt, InvocationStartState
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.store import Store
from hflow.verify import CheckOutcome, CheckRunners, FakeCheckRunner


def _wait_for_exit(pid: int, *, timeout_seconds: float) -> bool:
    """Has this process terminated, checked by its exit code rather than by its signal.

    The project's ``process_gone`` asks whether the process object is *signalled*, which is not the
    same question as whether the process object still exists - Windows keeps the object alive while
    any handle to it is open, so a terminated process can still be opened. The exit code separates
    the two facts cleanly: a running process reports ``STILL_ACTIVE`` (259), a terminated one
    reports its real code. Nothing here claims *why* a signal can lag; this measures the state.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    deadline = time.monotonic() + timeout_seconds
    while True:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Only "no such process" is absence; a process this user cannot open still exists.
            return ctypes.get_last_error() == error_invalid_parameter
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != still_active:
                return True
        finally:
            kernel32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Repair Test",
            "GIT_AUTHOR_EMAIL": "repair@example.invalid",
            "GIT_COMMITTER_NAME": "Repair Test",
            "GIT_COMMITTER_EMAIL": "repair@example.invalid",
        },
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """A real one-commit Git project with a bug the first attempt does not fix."""
    repo = tmp_path / "sample"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.py").write_text(
        "def parse(text):\n    return text\n", encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "sample project with an empty-input bug")
    return repo


FIXED_SOURCE = "def parse(text):\n    if text is None:\n        return ''\n    return text\n"


def _project() -> ProjectConfig:
    return ProjectConfig(
        project_id="repair-project",
        checks=[CheckDef(id="unit", kind="fake"), CheckDef(id="lint", kind="fake")],
        write_deny=[".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=1),
        min_risk_for_review="standard",
        review_required=True,
    )


def _spec(
    *,
    policy: RepairPolicy | None,
    base_commit: str,
    turns: int = 4,
    revision: int = 1,
    mode: str = "worktree",
) -> TaskSpec:
    return TaskSpec(
        task_id="T-REPAIR",
        revision=revision,
        goal="Make the approved check pass without breaking the other one",
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"]),
            AcceptanceCriterion(id="AC-2", statement="lint passes", check_ids=["lint"]),
        ],
        scope=Scope(write_allow=["src/parser.py"], write_deny=[".hflow/**"]),
        risk="standard",
        reuse=ReuseDecision(status=ReuseStatus.EXISTING_DECISION, reference="local", reason="test"),
        review=ReviewRequirement(required=True),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=turns, max_repair_cycles=1),
        workspace=WorkspaceSpec(mode=mode, base_commit=base_commit, keep=True),
        repair_policy=policy,
    )


def _policy(**overrides) -> RepairPolicy:
    payload: dict[str, object] = {
        "check_exit_codes": {"unit": [1]},
        "allow_reviewer_changes": True,
    }
    payload.update(overrides)
    return RepairPolicy(**payload)  # type: ignore[arg-type]


def _request(*, project: ProjectConfig, spec: TaskSpec, project_root: Path) -> RunRequest:
    return RunRequest(
        task=spec,
        project=project,
        project_root=str(project_root),
        workspace_root=str(project_root),
    )


class FailingOnceThenPassing(FakeCheckRunner):
    """``unit`` fails its first execution with a declared business code, then passes.

    The plan's first two repair paths differ only in which trigger fires first, so this runner
    models "the check that was failing is fixed after the repair" - the state a real repair is
    supposed to reach. ``lint`` always passes.
    """

    def __init__(self, *, fail_first: str | None = "unit", declared_exit: int = 1) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.declared_exit = declared_exit
        self.seen: list[str] = []

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        self.calls.append(check.id)
        first_time = check.id not in self.seen
        self.seen.append(check.id)
        if first_time and check.id == self.fail_first:
            return CheckOutcome(
                EvidenceStatus.FAILED,
                exit_code=self.declared_exit,
                detail=f"fake check {check.id}: business assertion failed",
                exit_reason="nonzero_exit",
            )
        return CheckOutcome(
            EvidenceStatus.PASSED,
            exit_code=0,
            detail=f"fake check {check.id}: passed",
            exit_reason="completed",
        )


class AlwaysFailingBusiness(FakeCheckRunner):
    """``unit`` fails every time with the declared code: no repair can fix it."""

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        self.calls.append(check.id)
        if check.id == "unit":
            return CheckOutcome(
                EvidenceStatus.FAILED,
                exit_code=1,
                detail="fake check unit: still failing",
                exit_reason="nonzero_exit",
            )
        return CheckOutcome(EvidenceStatus.PASSED, exit_code=0, exit_reason="completed")


class EnvironmentFailing(FakeCheckRunner):
    """``unit`` fails because the environment broke, not because the assertion failed."""

    def __init__(self, *, status: EvidenceStatus = EvidenceStatus.ERROR, reason: str = "timed_out"):
        super().__init__()
        self.status = status
        self.reason = reason

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        self.calls.append(check.id)
        if check.id == "unit":
            return CheckOutcome(
                self.status,
                exit_code=None,
                detail=f"fake check unit: {self.reason}",
                exit_reason=self.reason,
            )
        return CheckOutcome(EvidenceStatus.PASSED, exit_code=0, exit_reason="completed")


class RepairingDriver(FakeDriver):
    """A fake driver whose *second* implementer run writes the fix.

    The first attempt leaves the candidate as it was, so the check's first execution fails; when
    the run dispatches the repair this driver's write plan changes and the second candidate really
    differs from the first. The write lands in the run's isolated worktree, never the user's
    checkout. The packets it received are kept so a test can assert what the agent was actually
    handed.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        first_plan: dict[str, str],
        repair_plan: dict[str, str],
        review: ReviewOutput | None = None,
        first_remove: list[str] | None = None,
        repair_remove: list[str] | None = None,
    ) -> None:
        super().__init__(
            project_root,
            FakeScript(
                outcome=InvocationOutcome.COMPLETED,
                agent_turns=1,
                review=review or ReviewOutput(verdict="accepted", findings=[]),
            ),
        )
        self.first_plan = dict(first_plan)
        self.repair_plan = dict(repair_plan)
        self.first_remove = list(first_remove or [])
        self.repair_remove = list(repair_remove or [])
        self.labels: list[str] = []
        self.packets: list[str] = []

    def start(self, request):  # noqa: ANN001 - Protocol shape
        is_repair = "## Repair attempt" in request.packet
        self.labels.append(f"{request.role}{'-repair' if is_repair else ''}")
        self.packets.append(request.packet)
        # The plan for *this* implementer invocation: the first one changes nothing, the repair
        # one applies the fix. Deletions are per round too; a reviewer never deletes anything.
        if request.role == "implementer":
            self.script.write_plan = dict(self.repair_plan if is_repair else self.first_plan)
            self.script.remove_plan = list(self.repair_remove if is_repair else self.first_remove)
        else:
            self.script.remove_plan = []
        return super().start(request)


def _root_setup(store: Store, *, spec: TaskSpec, project_root: Path, max_submissions: int = 4):
    """A registered root plus its artifact: the shape a real run has since batch E1.

    A real transport's repair is only admitted with a root binding (plan 5.1; a fully offline
    fake-driver run may still repair without one), so these tests exercise the root path a real
    run takes - and a root charge names the artifact that bought it, so an end-to-end test has to
    set one up. The ledger path is the store's own file,
    so the binding and the record cannot drift apart, and the artifact's ceiling is what the
    worst-case gate is measured against.
    """
    from hflow.authorization import AuthorizationBinding, AuthorizationRecord
    from hflow.contracts import RootBudgetBinding, RootBudgetLimits

    limits = RootBudgetLimits(max_top_level_submissions=max_submissions, max_repairs=1)
    binding = RootBudgetBinding.derive(
        project_id="repair-project",
        repo_path=str(project_root),
        task_id=spec.task_id,
        ledger_path=store.path,
    )
    store.register_root_budget(binding, limits)
    record = AuthorizationRecord(
        authorization_id="AUTH-repair-test",
        user_text="I approve one bounded run of this exact task against this root.",
        authorized_at="2026-09-26T00:00:00Z",
        max_top_level_submissions=min(4, max_submissions),
        binding=AuthorizationBinding(
            mode="m2-live-change",
            driver="fake",
            project_id="repair-project",
            repo_path=str(project_root),
            base_commit=spec.workspace.base_commit,
            spec_digest=spec.spec_digest(),
            spec_path=str(project_root / "task.json"),
            root_budget=binding,
        ),
        root_limits=limits,
    )
    store.register_authorization(record.as_store_record())
    return binding, limits, record


def _controller(
    store: Store,
    *,
    project_root: Path,
    spec: TaskSpec,
    driver: FakeDriver,
    runners: CheckRunners,
    reviewer: FakeDriver | None = None,
) -> Controller:
    binding, limits, record = _root_setup(store, spec=spec, project_root=project_root)
    return Controller(
        store,
        driver,
        reviewer_driver=reviewer,
        controller_build="repair-test",
        runners=runners,
        data_dir=project_root.parent / "data",
        authorization=record,
        root_binding=binding,
        root_limits=limits,
        preflight=lambda: (True, "repair test: no launch binding to probe"),
    )


# --------------------------------------------------------------------------
# 8. no policy means no repair, whatever the budget field says
# --------------------------------------------------------------------------


def test_a_historical_budget_field_without_a_policy_still_runs_the_old_single_loop(
    tmp_path: Path, sample_repo: Path
) -> None:
    """``max_repair_cycles=1`` is a historical number, not consent: the failure ends the run."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=None, base_commit=base)
    assert spec.budget.max_repair_cycles == 1
    driver = RepairingDriver(sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE})
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.VERIFICATION_FAILED
        assert "no repair policy" in (outcome.block_reason or "")
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"], (
            "without a policy the run buys one implementer and stops"
        )
        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [RepairDecision.NOT_ENABLED]
    finally:
        store.close()


def test_a_policy_the_scope_cannot_support_is_refused_before_anything_is_dispatched(
    tmp_path: Path, sample_repo: Path
) -> None:
    """A repair needs a worktree, a review and a run ceiling that covers the worst case."""
    from hflow.admission import predictable_dispatch_problems

    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    in_place = _spec(policy=_policy(), base_commit=base, mode="in_place")
    issues = predictable_dispatch_problems(
        in_place, project, production=False, implementer_writes=True, launches=[]
    )
    assert any("repair" in issue.detail.lower() for issue in issues), (
        f"an in-place repair must be refused before dispatch: {issues}"
    )

    too_few_turns = _spec(policy=_policy(), base_commit=base, turns=2)
    issues = predictable_dispatch_problems(
        too_few_turns, project, production=False, implementer_writes=True, launches=[]
    )
    assert any("repair" in issue.detail.lower() for issue in issues), (
        f"a 2-turn ceiling cannot host a 4-dispatch worst case: {issues}"
    )


# --------------------------------------------------------------------------
# 9. the three dispatch counts
# --------------------------------------------------------------------------


def test_the_first_attempt_success_path_costs_two_dispatches_and_records_no_repair(
    tmp_path: Path, sample_repo: Path
) -> None:
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE})
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == ["implementer", "reviewer"]
        assert [entry.is_repair for entry in intents] == [False, False]
        assert store.invocation_state_counts(outcome.run_id).total == 2
        assert store.repair_records_for(outcome.run_id) == []
    finally:
        store.close()


def test_a_business_check_failure_buys_one_repair_and_three_dispatches(
    tmp_path: Path, sample_repo: Path
) -> None:
    """I1 fails a declared business check -> I2 -> fresh checks -> R2 accepts. Exactly 3."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason

        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == ["implementer", "implementer", "reviewer"]
        assert [entry.is_repair for entry in intents] == [False, True, False], (
            "the second implementer is the repair; the reviewer that follows is not"
        )
        assert store.invocation_state_counts(outcome.run_id).total == 3

        attempts = store.attempts_for(outcome.run_id)
        assert len(attempts) == 2, "the repair is a second implementer attempt on the same revision"
        assert [bool(attempt["is_repair"]) for attempt in attempts] == [False, True]

        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [RepairDecision.ALLOWED]
        assert decisions[0].trigger.value == "business_check_failed"
        assert decisions[0].failed_checks == ["unit"]

        # The repair packet carried the context; the first attempt's did not.
        assert "## Repair attempt" not in driver.packets[0]
        assert "## Repair attempt" in driver.packets[1]
        assert "business assertion failed" in driver.packets[1]
    finally:
        store.close()


def test_a_reviewer_rejection_buys_one_repair_and_four_dispatches(
    tmp_path: Path, sample_repo: Path
) -> None:
    """I1 passes -> R1 rejects with findings -> I2 -> fresh checks -> R2 accepts. Exactly 4."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)

    viewer = {"verdicts": 0}

    class RejectThenAccept(FakeDriver):
        def start(self, request):  # noqa: ANN001 - Protocol shape
            result = super().start(request)
            if request.role != "reviewer":
                return result
            viewer["verdicts"] += 1
            if viewer["verdicts"] > 1:
                return result
            return result.model_copy(
                update={
                    "review": ReviewOutput(
                        verdict="changes_requested",
                        findings=[
                            {
                                "severity": "major",
                                "statement": "the empty-input path still returns None",
                                "location": "src/parser.py",
                            }
                        ],
                    )
                }
            )

    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    reviewer = RejectThenAccept(
        sample_repo,
        FakeScript(
            outcome=InvocationOutcome.COMPLETED,
            agent_turns=1,
            review=ReviewOutput(verdict="accepted", findings=[]),
        ),
    )
    controller = _controller(
        store,
        project_root=sample_repo,
        spec=spec,
        driver=driver,
        reviewer=reviewer,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == [
            "implementer",
            "reviewer",
            "implementer",
            "reviewer",
        ]
        assert [entry.is_repair for entry in intents] == [False, False, True, False]
        assert store.invocation_state_counts(outcome.run_id).total == 4

        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [RepairDecision.ALLOWED]
        assert decisions[0].trigger.value == "review_changes_requested"
        assert "empty-input" in driver.packets[-1], (
            "the repair packet must carry the reviewer's finding it is acting on"
        )
    finally:
        store.close()


def _repair_packet_with(findings: list[dict[str, object]]) -> str:
    """Render the implementer packet of a review-triggered repair carrying *findings*."""
    from hflow.contracts import RepairContext
    from hflow.packet import render_implementer_packet

    return render_implementer_packet(
        task_id="T-REPAIR",
        task_revision=1,
        goal="Make the approved check pass",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"])],
        scope=Scope(write_allow=["src/parser.py"]),
        workspace="/work",
        spec_digest="sha256:test",
        deadline_seconds=60,
        writes_allowed=True,
        repair=RepairContext(
            trigger=RepairTrigger.REVIEW_CHANGES_REQUESTED,
            findings=findings,
            remaining_turns=2,
            deadline_seconds=60,
        ),
    ).text


@pytest.mark.parametrize(
    "finding",
    [
        # The plan 16.5 shape: only ``location`` of these keys was rendered before.
        {
            "id": "R1-F1",
            "location": "src/parser.py:42",
            "impact": "parse(None) raises instead of returning ''",
            "evidence": "unit test test_none_input fails with TypeError",
            "required_fix": "add a None guard before the split",
        },
        # A conforming reviewer is free to choose its own keys.
        {
            "severity": "high",
            "file": "src/parser.py",
            "issue": "parse(None) must return ''",
            "suggestion": "add a None guard",
        },
    ],
    ids=["plan-16-5", "free-form-keys"],
)
def test_the_repair_packet_renders_every_key_of_a_finding(finding: dict[str, object]) -> None:
    """The repair attempt acts on the packet alone, so no key of a finding may be dropped."""
    from hflow.contracts import canonical_json

    text = _repair_packet_with([finding])
    section = text.split("### Findings HFlow recorded", 1)[1]
    assert f"- {canonical_json(finding)}" in section, section
    for key, value in finding.items():
        assert f'"{key}"' in section and str(value) in section, (key, section)


def test_an_oversize_finding_value_is_truncated_with_an_explicit_marker() -> None:
    """A long value is capped with a marker that says how much was cut, never silently."""
    from hflow.packet import MAX_FINDING_VALUE_BYTES

    long_fix = "x" * (MAX_FINDING_VALUE_BYTES + 1234)
    text = _repair_packet_with(
        [{"severity": "major", "statement": "short and kept", "required_fix": long_fix}]
    )
    section = text.split("### Findings HFlow recorded", 1)[1]
    assert "short and kept" in section
    assert long_fix not in section
    assert "x" * MAX_FINDING_VALUE_BYTES + "…[truncated 1234 bytes]" in section, section
    assert len(text.encode("utf-8")) <= 32 * 1024


# --------------------------------------------------------------------------
# 10 and 11. what must NOT buy a second attempt
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (EnvironmentFailing(reason="timed_out"), RepairDecision.NOT_A_BUSINESS_FAILURE),
        (EnvironmentFailing(status=EvidenceStatus.ERROR, reason="settlement_forced"),
         RepairDecision.NOT_A_BUSINESS_FAILURE),
        (EnvironmentFailing(status=EvidenceStatus.ERROR, reason="output_capture_error"),
         RepairDecision.NOT_A_BUSINESS_FAILURE),
    ],
)
def test_an_environment_failure_never_buys_a_repair(
    tmp_path: Path, sample_repo: Path, runner: FakeCheckRunner, expected: RepairDecision
) -> None:
    """A timeout, a forced settlement or a capture error is not a business assertion failing."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": runner}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [expected], decisions
    finally:
        store.close()


def test_an_undeclared_exit_code_never_buys_a_repair(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The policy declares exit 1; the check exits 2. An undeclared code is not a diagnosis."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(declared_exit=2)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [RepairDecision.NOT_A_BUSINESS_FAILURE]
        assert "does not declare" in decisions[0].reason
    finally:
        store.close()


@pytest.mark.parametrize(
    "findings",
    [
        [],
        [{}],
        [{"severity": ""}],
        [{"statement": "  "}],
        # Only bookkeeping keys: an id, a severity and a place name no defect to act on.
        [{"id": "F1", "severity": "high", "status": "open", "location": "src/parser.py:3",
          "target": "src/parser.py"}],
        [{}, {"detail": "\n\t"}],
    ],
    ids=["empty-list", "empty-object", "blank-severity", "blank-statement", "metadata-only",
         "several-blank"],
)
def test_an_empty_reviewer_rejection_never_buys_a_repair(
    tmp_path: Path, sample_repo: Path, findings: list[dict[str, object]]
) -> None:
    """A rejection with no usable finding says nothing to change, so nothing is bought.

    "Usable" means at least one non-blank text value outside the bookkeeping keys (id, severity,
    status, location, target). An empty object or a whitespace statement is still a non-empty
    list, and it must not pay for a second implementer and a second reviewer.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)

    class EmptyRejection(FakeDriver):
        def start(self, request):  # noqa: ANN001 - Protocol shape
            result = super().start(request)
            if request.role != "reviewer":
                return result
            return result.model_copy(
                update={
                    "review": ReviewOutput(verdict="changes_requested", findings=list(findings))
                }
            )

    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store,
        project_root=sample_repo,
        spec=spec,
        driver=driver,
        reviewer=EmptyRejection(
            sample_repo,
            FakeScript(
                outcome=InvocationOutcome.COMPLETED,
                agent_turns=1,
                review=ReviewOutput(verdict="accepted", findings=[]),
            ),
        ),
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.REVIEW_REJECTED
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == [
            "implementer",
            "reviewer",
        ], "an empty rejection must not buy a second implementer"
        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [RepairDecision.NO_FINDINGS]
    finally:
        store.close()


def test_a_second_failure_does_not_buy_a_third_implementer(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The repair runs, fails again, and the run stops: one repair is the maximum."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": AlwaysFailingBusiness()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == ["implementer", "implementer"], (
            f"exactly one repair, never a third implementer: {[e.role for e in intents]}"
        )
        assert store.invocation_state_counts(outcome.run_id).total == 2, (
            "a failing check must not buy a reviewer"
        )
        decisions = store.repair_records_for(outcome.run_id)
        assert [record.decision for record in decisions] == [
            RepairDecision.ALLOWED,
            RepairDecision.ALREADY_REPAIRED,
        ], (
            "one repair, and the refusal of a second one recorded as a decision of its own"
        )
    finally:
        store.close()


def test_a_repair_that_changes_nothing_stops_without_buying_a_reviewer(
    tmp_path: Path, sample_repo: Path
) -> None:
    """No content change is no progress: the tree and fingerprint decide, not a new commit SHA."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    # Both rounds write the same bytes, so the second candidate is identical to the first.
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == ["implementer", "implementer"], (
            "the repair ran, changed nothing, and no reviewer was bought for it"
        )
        decisions = store.repair_records_for(outcome.run_id)
        assert decisions, "the run must record why it stopped"
        assert RepairDecision.NO_CONTENT_CHANGE in {record.decision for record in decisions}
    finally:
        store.close()


def test_the_final_candidate_carries_the_whole_change_from_the_original_base(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The receipt's diff is base -> final candidate, not the last round's patch."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        receipt = outcome.receipt
        assert receipt is not None
        assert receipt.candidate.base_commit == base, (
            "the receipt must name the task's original base, not the repaired candidate's parent"
        )
        worktree = Path(receipt.candidate.worktree)
        # The delivered candidate contains the fix, and the diff against the original base shows
        # it - which is what makes a repaired delivery readable as one change.
        assert "if text is None" in (worktree / "src" / "parser.py").read_text(encoding="utf-8")
        diff = _git(sample_repo, "diff", "--name-only", f"{base}..{receipt.candidate.git_commit}")
        assert diff.split() == ["src/parser.py"]
        # The first round's candidate ref survives, so the intermediate state is still reachable.
        assert _git(sample_repo, "for-each-ref", "--format=%(refname)", "refs/hflow/").strip()
    finally:
        store.close()


# --------------------------------------------------------------------------
# 11b. a stop that could not be confirmed, then the implementer's late COMPLETED
# --------------------------------------------------------------------------


class StopThenCompleteDriver(RepairingDriver):
    """An implementer that is past its spawn when an unconfirmable stop arrives, then completes.

    The order is the production one (``AcpxDshDriver`` asks ``stop_requested`` only inside its
    spawn gate): the fake reports its spawn and does the work, *then* the operator's stop is
    recorded - through the real ``Controller.cancel`` - and only after that does ``start`` return
    its ``COMPLETED`` result. ``cancel`` reports ``unknown``, the answer of a driver that cannot
    confirm the process boundary emptied (or that does not own the handle).
    """

    def __init__(self, *args, stop_round: int, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        #: 1 stops the first implementer (I1), 2 stops the repair (I2).
        self.stop_round = stop_round
        self.controller: Controller | None = None
        self.receipt: CancellationReceipt | None = None
        self.stopped_attempt = ""

    def start(self, request):  # noqa: ANN001 - Protocol shape
        result = super().start(request)
        implementer_round = sum(label.startswith("implementer") for label in self.labels)
        if request.role == "implementer" and implementer_round == self.stop_round:
            assert self.controller is not None
            self.stopped_attempt = request.attempt_id
            self.receipt = self.controller.cancel(request.run_id)
        return result

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancelled.append(invocation_id)
        return CancellationReceipt(
            invocation_id=invocation_id,
            status="unknown",
            mechanism="none",
            local_process_stopped=False,
            detail="offline stand-in: the process boundary could not confirm the stop",
        )


@pytest.mark.parametrize(
    ("mode", "stop_round"),
    [("in_place", 1), ("worktree", 1), ("worktree", 2)],
    ids=["in_place-I1", "worktree-I1", "worktree-I2"],
)
def test_a_late_completed_after_an_unconfirmed_stop_changes_nothing(
    tmp_path: Path, sample_repo: Path, mode: str, stop_round: int
) -> None:
    """AGENTS rules 4 and 8: the stop decided the run; the late success is only a note.

    The repair round (I2) exists only in worktree mode - admission refuses a ``repair_policy`` on
    an in-place task - so the in-place case is a first attempt without a policy.

    Before the fix the late ``COMPLETED`` settled the ledger entry as completed (unblocking the
    root while the block said "work may still be running"), marked the attempt ``SUCCEEDED``,
    froze a candidate commit and a ``refs/hflow/candidates`` ref after the stop, and then raised
    ``StoreError`` out of ``run_task`` from the transition into ``CHECKING``.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy() if mode == "worktree" else None, base_commit=base, mode=mode)
    driver = StopThenCompleteDriver(
        sample_repo,
        # Stopped in round 1, I1 writes the fix, so a freeze would have something to commit.
        # Stopped in round 2, I1 changes nothing, so ``unit`` fails once with its declared code
        # and buys the repair, which writes the fix.
        first_plan={"src/parser.py": FIXED_SOURCE} if stop_round == 1 else {},
        repair_plan={"src/parser.py": FIXED_SOURCE},
        stop_round=stop_round,
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    driver.controller = controller
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert driver.receipt is not None and driver.receipt.status == "unknown"
        assert outcome.task_state is TaskState.BLOCKED, outcome
        assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason

        attempts = store.attempts_for(outcome.run_id)
        assert len(attempts) == stop_round
        stopped = attempts[-1]
        assert stopped["attempt_id"] == driver.stopped_attempt
        assert bool(stopped["is_repair"]) is (stop_round == 2)
        assert stopped["state"] == AttemptState.ACTIVE.value, (
            "the late result was applied to an attempt a stop had already decided: "
            f"{stopped['state']} {stopped['outcome']}"
        )

        entries = [entry for entry in store.invocations_for(outcome.run_id)
                   if entry.attempt_id == driver.stopped_attempt]
        assert len(entries) == 1
        assert entries[0].state is InvocationStartState.STARTED, (
            "an unconfirmed stop keeps the entry open, so it keeps blocking the root until resume "
            f"marks it unknown: {entries[0].model_dump(mode='json')}"
        )

        notes = store.notes_for(outcome.run_id)
        late = [note for note in notes if note.startswith("late_result")]
        assert len(late) == 1 and "completed" in late[0] and driver.stopped_attempt in late[0], notes

        # Nothing was frozen for the stopped attempt: no candidate ref, no commit after the stop.
        refs = _git(sample_repo, "for-each-ref", "--format=%(refname)", "refs/hflow/candidates")
        assert driver.stopped_attempt not in refs, refs
        assert not any(note.startswith("candidate ref") and driver.stopped_attempt in note
                       for note in notes), notes
        if mode == "worktree":
            # Only round 1's candidate of an I2 stop is retained (it changed nothing, so it is the
            # base itself); the stopped attempt's change is in the worktree and was not committed.
            assert len(refs.split()) == stop_round - 1, refs
            worktree = Path(store.get_run(outcome.run_id)["worktree_path"])
            assert _git(worktree, "log", "--format=%s", f"{base}..HEAD").strip() == ""
            assert "if text is None" in (worktree / "src" / "parser.py").read_text(encoding="utf-8")
        else:
            assert refs.strip() == ""

        # ``resume`` still reconciles: the open entry becomes unknown, nothing is re-dispatched.
        dispatched = list(driver.labels)
        resumed = controller.resume(outcome.run_id)
        assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN
        assert driver.labels == dispatched
        entry = store.invocation(entries[0].invocation_id)
        assert entry is not None and entry.state is InvocationStartState.UNKNOWN
    finally:
        store.close()


@pytest.mark.parametrize("seam", ["settle_invocation", "advance_to_checking"])
def test_a_stop_after_the_result_was_applied_freezes_nothing_more_and_returns(
    tmp_path: Path, sample_repo: Path, monkeypatch, seam: str
) -> None:
    """The other order: the result is applied first, then the unconfirmed stop lands.

    The result stands - it arrived before the stop - but the stop still decides the run.
    Landing before the freeze (``settle_invocation`` seam), no candidate commit and no ref is
    made; landing after it (``advance_to_checking`` seam), the refused transition into
    ``CHECKING`` is the stop, and ``run_task`` returns the recorded block instead of raising.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = StopThenCompleteDriver(
        sample_repo,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={},
        stop_round=0,  # the driver itself never stops; the seam does
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    driver.controller = controller
    original = getattr(store, seam)
    receipts: list[CancellationReceipt] = []

    def stop_first(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - the wrapped method's shape
        if not receipts:
            run_id = str(store.list_runs()[0]["run_id"])
            receipts.append(controller.cancel(run_id))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, seam, stop_first)
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert [receipt.status for receipt in receipts] == ["unknown"]
        assert outcome.task_state is TaskState.BLOCKED, outcome
        assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
        (attempt,) = store.attempts_for(outcome.run_id)
        assert attempt["state"] == AttemptState.SUCCEEDED.value, "applied before the stop"
        assert driver.labels == ["implementer"], "no check or reviewer follows a stop"
        refs = _git(sample_repo, "for-each-ref", "--format=%(refname)", "refs/hflow/candidates")
        if seam == "settle_invocation":
            assert refs.strip() == ""
            worktree = Path(store.get_run(outcome.run_id)["worktree_path"])
            assert _git(worktree, "log", "--format=%s", f"{base}..HEAD").strip() == ""
            assert any("no candidate was frozen" in note for note in store.notes_for(outcome.run_id))
        else:
            assert attempt["attempt_id"] in refs, "frozen before the stop, so it is retained"
    finally:
        store.close()


# --------------------------------------------------------------------------
# the frozen candidate is the tree that was checked: deletions, write_deny, the stop rule
# --------------------------------------------------------------------------


@pytest.fixture()
def scoped_repo(tmp_path: Path) -> Path:
    """The sample project plus a second module and a file the task's scope will deny."""
    repo = tmp_path / "scoped"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.py").write_text(
        "def parse(text):\n    return text\n", encoding="utf-8"
    )
    (repo / "src" / "a.py").write_text("LEGACY = True\n", encoding="utf-8")
    (repo / "src" / "secret.txt").write_text("original secret\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "sample project with a denied file inside src")
    return repo


def _scoped_spec(
    base: str,
    *,
    allow: list[str],
    deny: list[str] | None = None,
    policy: RepairPolicy | None = None,
) -> TaskSpec:
    return _spec(policy=policy, base_commit=base).model_copy(
        update={"scope": Scope(write_allow=allow, write_deny=[".hflow/**", *(deny or [])])}
    )


def _run_worktree(repo: Path, run_id: str) -> Path:
    """Where the controller put this run's worktree (beside the repository, by run id)."""
    return repo.parent / f"{repo.name}.hflow-worktrees" / run_id


def _commits_touching(repo: Path, path: str) -> list[str]:
    """Every commit reachable from any ref - candidate refs included - that changed ``path``."""
    return _git(repo, "log", "--all", "--format=%H", "--", path).split()


@pytest.mark.parametrize(
    "write_plan",
    [{}, {"src/parser.py": FIXED_SOURCE}],
    ids=["deletion-only", "delete-plus-modify"],
)
def test_a_deleted_listed_file_is_frozen_into_the_candidate(
    tmp_path: Path, scoped_repo: Path, write_plan: dict[str, str]
) -> None:
    """The deletion the checks saw is in the commit the receipt names.

    The freeze used to stage an entry only while it existed on disk, so an exactly-listed file
    the worker deleted stayed in the commit: the receipt named a tree that still held the file
    while the checks and the reviewer ran on one that did not.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src/parser.py", "src/a.py"])
    driver = RepairingDriver(
        scoped_repo, first_plan=write_plan, repair_plan={}, first_remove=["src/a.py"]
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        receipt = outcome.receipt
        assert receipt is not None
        commit = receipt.candidate.git_commit
        assert commit != base, "a deletion is a change, so the candidate is a new commit"
        assert "src/a.py" not in _git(scoped_repo, "ls-tree", "-r", "--name-only", commit).split()
        assert set(receipt.candidate_paths) == {"src/a.py", *write_plan}
        worktree = Path(receipt.candidate.worktree)
        assert _git(worktree, "status", "--porcelain").strip() == "", (
            "the freeze must leave nothing of the checked change outside the commit"
        )
    finally:
        store.close()


def test_a_repair_round_that_only_deletes_a_listed_file_is_a_new_candidate(
    tmp_path: Path, scoped_repo: Path
) -> None:
    """A repair whose one change is a deletion must not be delivered under round one's commit."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src/parser.py", "src/a.py"], policy=_policy())
    driver = RepairingDriver(
        scoped_repo,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={},
        repair_remove=["src/a.py"],
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == [
            "implementer", "implementer", "reviewer",
        ]
        receipt = outcome.receipt
        assert receipt is not None
        commit = receipt.candidate.git_commit
        candidates = _git(
            scoped_repo, "for-each-ref", "--format=%(objectname)", "refs/hflow/candidates/"
        ).split()
        assert len(set(candidates)) == 2, f"two rounds, two different candidates: {candidates}"
        assert "src/a.py" not in _git(scoped_repo, "ls-tree", "-r", "--name-only", commit).split()
        assert set(receipt.candidate_paths) == {"src/parser.py", "src/a.py"}
        worktree = Path(receipt.candidate.worktree)
        assert _git(worktree, "status", "--porcelain").strip() == ""
    finally:
        store.close()


@pytest.mark.parametrize(
    ("first_plan", "repair_plan", "roles"),
    [
        (
            {"src/parser.py": FIXED_SOURCE, "src/secret.txt": "leaked\n"},
            {"src/parser.py": FIXED_SOURCE + "# repaired\n"},
            ["implementer"],
        ),
        (
            {"src/parser.py": FIXED_SOURCE},
            {"src/secret.txt": "leaked\n"},
            ["implementer", "implementer"],
        ),
    ],
    ids=["first-round", "repair-only-round"],
)
def test_a_denied_file_inside_an_allowed_directory_blocks_before_the_freeze(
    tmp_path: Path,
    scoped_repo: Path,
    first_plan: dict[str, str],
    repair_plan: dict[str, str],
    roles: list[str],
) -> None:
    """``write_deny`` is enforced on what the worker changed, not only quoted in its packet.

    ``src`` is allowed and ``src/secret.txt`` is denied. The write used to pass both scope checks
    (they only asked about ``write_allow``), ``git add -- src`` committed it, and a repair round
    whose only change was the denied file counted as progress and bought a reviewer.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"], deny=["src/secret.txt"], policy=_policy())
    driver = RepairingDriver(scoped_repo, first_plan=first_plan, repair_plan=repair_plan)
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "src/secret.txt" in (outcome.block_reason or "")
        assert outcome.receipt is None
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == roles, (
            "a denied change is refused before any reviewer is bought for it"
        )
        # The denied bytes are in no commit, and were never even staged.
        assert _commits_touching(scoped_repo, "src/secret.txt") == [base]
        worktree = _run_worktree(scoped_repo, outcome.run_id)
        assert _git(worktree, "diff", "--cached", "--name-only").strip() == ""
    finally:
        store.close()


@pytest.mark.parametrize("planted", ["src/.acpxrc.json", ".acpxrc.json"])
def test_an_implementer_written_acpx_config_blocks_before_review(
    tmp_path: Path, scoped_repo: Path, planted: str
) -> None:
    """acpx reads ``<cwd>/.acpxrc.json`` over HFlow's client config, in the worktree it wrote.

    The reviewer runs in the implementer's worktree, so a worker-written client config would
    reconfigure the next role. It is on the built-in deny list: refused before review wherever
    it sits, including inside a directory the task allows.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    driver = RepairingDriver(
        scoped_repo,
        first_plan={"src/parser.py": FIXED_SOURCE, planted: '{"agents": {}}\n'},
        repair_plan={},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert planted in (outcome.block_reason or "")
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
        assert _commits_touching(scoped_repo, planted) == []
    finally:
        store.close()


def test_a_repair_that_changes_the_tree_but_not_the_scoped_content_is_not_progress(
    tmp_path: Path, scoped_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged scoped fingerprint is no content change; a tree change beside it is refused.

    A bytecode cache under an allowed directory is outside the fingerprint (it skips
    ``__pycache__``) but inside ``git add -- src``. Such a round used to count as progress: every
    check ran again and a reviewer was bought for a candidate whose checked content had not moved.
    """
    # Keep a machine-wide ignore rule from hiding the cache from git: this test is about a path
    # git does commit.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg-config"))
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"], policy=_policy())
    driver = RepairingDriver(
        scoped_repo,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={"src/__pycache__/parser.cpython-314.pyc": "not really bytecode\n"},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "fingerprint" in (outcome.block_reason or "")
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == [
            "implementer", "implementer",
        ], "no reviewer is bought for a round that did not change the checked content"
        decisions = [record.decision for record in store.repair_records_for(outcome.run_id)]
        assert decisions == [RepairDecision.ALLOWED, RepairDecision.NO_CONTENT_CHANGE]
    finally:
        store.close()


class CommittingDriver(RepairingDriver):
    """An implementer that runs Git itself in its worktree after writing its plan.

    ``first_git`` / ``repair_git`` are argv lists run in the worktree for that round; the
    literal ``{base}`` is replaced by the task's base commit. The out-of-scope files are written
    first when ``plant_out_of_scope`` is set, under directories the manifest scan skips.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        base: str,
        first_git: list[list[str]] | None = None,
        repair_git: list[list[str]] | None = None,
        plant_out_of_scope: bool = False,
        **kwargs,  # noqa: ANN003
    ) -> None:
        super().__init__(project_root, **kwargs)
        self.base = base
        self.first_git = list(first_git or [])
        self.repair_git = list(repair_git or [])
        self.plant_out_of_scope = plant_out_of_scope

    def start(self, request):  # noqa: ANN001 - Protocol shape
        result = super().start(request)
        if request.role == "implementer":
            worktree = Path(request.workspace)
            is_repair = "## Repair attempt" in request.packet
            if self.plant_out_of_scope and not is_repair:
                (worktree / ".hflow").mkdir(exist_ok=True)
                # LF bytes: a line-ending conversion in the worker's own git must not leave
                # these looking modified to HFlow's status read; the worker's commit is clean.
                (worktree / ".hflow" / "project.json").write_bytes(b"{}\n")
                (worktree / "tools" / "__pycache__").mkdir(parents=True, exist_ok=True)
                (worktree / "tools" / "__pycache__" / "evil.py").write_bytes(b"x = 1\n")
            for argv in self.repair_git if is_repair else self.first_git:
                _git(worktree, *[arg.replace("{base}", self.base) for arg in argv])
        return result


_WORKER_COMMITS_OUT_OF_SCOPE = [
    ["add", "-f", ".hflow/project.json", "tools/__pycache__/evil.py"],
    ["commit", "-q", "-m", "worker commit"],
]


def test_a_worker_commit_outside_the_scope_is_never_delivered(
    tmp_path: Path, scoped_repo: Path
) -> None:
    """A worker's own commit is refused, not adopted as the freeze base.

    ``.hflow`` and ``__pycache__`` are skipped by the manifest scan and a committed change leaves
    ``git status`` clean, so this commit passed every scope gate: the run was ACCEPTED and the
    receipt delivered HFlow's own state and a file no check or fingerprint had seen.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    driver = CommittingDriver(
        scoped_repo,
        base=base,
        first_git=_WORKER_COMMITS_OUT_OF_SCOPE,
        plant_out_of_scope=True,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED, outcome.receipt
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "moved HEAD" in (outcome.block_reason or "")
        assert outcome.receipt is None
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
        assert _git(scoped_repo, "for-each-ref", "refs/hflow/").strip() == "", (
            "no candidate ref keeps the worker's commit"
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "repair_git",
    [
        [["add", "src"], ["commit", "-q", "--amend", "-m", "amended"]],
        [["reset", "-q", "--soft", "{base}"]],
    ],
    ids=["amend", "reset-soft"],
)
def test_a_repair_worker_that_rewrites_the_previous_candidate_is_refused(
    tmp_path: Path, scoped_repo: Path, repair_git: list[list[str]]
) -> None:
    """Round two must build on round one's candidate, so its parent is what it claims to be."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"], policy=_policy())
    driver = CommittingDriver(
        scoped_repo,
        base=base,
        repair_git=repair_git,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={"src/parser.py": FIXED_SOURCE + "# repaired\n"},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED, outcome.receipt
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "moved HEAD" in (outcome.block_reason or "")
        assert outcome.receipt is None
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == [
            "implementer", "implementer",
        ]
    finally:
        store.close()


def test_the_cumulative_change_is_held_to_the_scope_after_the_freeze(
    tmp_path: Path, scoped_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second guard: whatever the freeze returns, the whole base-to-candidate diff is checked.

    The freeze's HEAD check is disabled here, so the worker's commit reaches the controller as a
    candidate; the cumulative diff must still refuse it before a ref keeps it or a check runs.
    """
    from hflow.gitworkspace import GitRepo

    real_freeze = GitRepo.freeze_candidate

    def freeze_without_the_head_check(self, worktree, *args, expected_head, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return real_freeze(self, worktree, *args, expected_head=self.worktree_commit(worktree), **kwargs)

    monkeypatch.setattr(GitRepo, "freeze_candidate", freeze_without_the_head_check)
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    driver = CommittingDriver(
        scoped_repo,
        base=base,
        first_git=_WORKER_COMMITS_OUT_OF_SCOPE,
        plant_out_of_scope=True,
        first_plan={"src/parser.py": FIXED_SOURCE},
        repair_plan={},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.BLOCKED, outcome.receipt
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert ".hflow/project.json" in (outcome.block_reason or "")
        assert outcome.receipt is None
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
        assert _git(scoped_repo, "for-each-ref", "refs/hflow/").strip() == ""
    finally:
        store.close()


def test_a_renamed_file_delivers_both_its_old_and_its_new_path(
    tmp_path: Path, scoped_repo: Path
) -> None:
    """A move is a deletion plus an addition; the receipt names both paths."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    legacy = (scoped_repo / "src" / "a.py").read_text(encoding="utf-8")
    driver = RepairingDriver(
        scoped_repo,
        first_plan={"src/renamed.py": legacy},
        repair_plan={},
        first_remove=["src/a.py"],
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert outcome.receipt is not None
        assert outcome.receipt.candidate_paths == ["src/a.py", "src/renamed.py"]
    finally:
        store.close()


class CachingChecks(FailingOnceThenPassing):
    """Like ruff or mypy: every check run leaves a self-ignoring cache directory in its cwd.

    The cache is written after the round's freeze (checks run on the frozen candidate), so it is
    HFlow's own byproduct, not the worker's - and it is not on the ignored-artifact allowlist.
    """

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        cache = Path(cwd) / ".ruff_cache"
        cache.mkdir(exist_ok=True)
        (cache / ".gitignore").write_text("*\n", encoding="utf-8")
        (cache / "CACHEDIR.TAG").write_text("Signature: check cache\n", encoding="utf-8")
        return super().run(check, cwd, timeout_seconds)


def test_an_ignored_cache_left_by_round_one_checks_does_not_make_the_paid_repair_unfreezable(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Ignored files the round-1 checks left behind are carried into the repair's freeze.

    They existed before the repair was bought, the repair's own scope check still refuses any
    worker change to them, and the freeze never stages an ignored file. Refusing them only after
    the repair was reserved and dispatched blamed the worker for HFlow's own check output and
    spent the root's repair on nothing.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": CachingChecks()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
        assert driver.labels == ["implementer", "implementer-repair", "reviewer"]
        decisions = [record.decision for record in store.repair_records_for(outcome.run_id)]
        assert decisions == [RepairDecision.ALLOWED]
        view = store.root_budget_view(controller.root_binding.root_id)
        assert view is not None and view.used_repairs == 1
        assert not (sample_repo / ".ruff_cache").exists(), "the user's checkout is never written"
    finally:
        store.close()


def test_a_repair_that_rewrites_a_carried_check_cache_is_still_a_scope_violation(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Carrying the checks' ignored leftovers is not a write grant: the worker may not touch them."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo,
        first_plan={},
        repair_plan={"src/parser.py": FIXED_SOURCE, ".ruff_cache/CACHEDIR.TAG": "tampered\n"},
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": CachingChecks()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert ".ruff_cache/CACHEDIR.TAG" in (outcome.block_reason or "")
        assert driver.labels == ["implementer", "implementer-repair"]
    finally:
        store.close()


class LeavesAnIgnoredLogInScope(FailingOnceThenPassing):
    """A check that writes an ignored log *inside* the write scope (``src/run.log``)."""

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        (Path(cwd) / "src" / "run.log").write_text(f"{check.id} output\n", encoding="utf-8")
        return super().run(check, cwd, timeout_seconds)


@pytest.mark.parametrize(
    "repair_plan",
    [{}, {"src/run.log": "worker edit\n"}, {"src/parser.py": FIXED_SOURCE}],
    ids=["no-edit", "edits-only-the-log", "real-fix"],
)
def test_an_ignored_check_byproduct_inside_the_write_scope_refuses_the_repair_before_it_is_bought(
    tmp_path: Path,
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_plan: dict[str, str],
) -> None:
    """A carried ignored file the scoped fingerprint covers would make it disagree with the commit.

    The freeze never stages an ignored file, but the fingerprint hashes every file under a
    ``write_allow`` directory. Carried into the repair, ``src/run.log`` made the round-2
    fingerprint differ from round 1's while the commit and tree stayed the same: the
    no-content-change guard was skipped, a reviewer was bought for round 1's failing commit, and
    the evidence covered bytes no commit holds. It is refused before anything is bought.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg-config"))
    (sample_repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(sample_repo, "add", ".gitignore")
    _git(sample_repo, "commit", "-q", "-m", "ignore logs")
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"], policy=_policy())
    driver = RepairingDriver(sample_repo, first_plan={}, repair_plan=repair_plan)
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": LeavesAnIgnoredLogInScope()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "src/run.log" in (outcome.block_reason or "")
        decisions = [record.decision for record in store.repair_records_for(outcome.run_id)]
        assert decisions == [RepairDecision.ALLOWED, RepairDecision.WORKSPACE_DRIFT]
        assert driver.labels == ["implementer"], "nothing is dispatched, no reviewer is bought"
        view = store.root_budget_view(controller.root_binding.root_id)
        assert view is not None and view.used_repairs == 0
        assert not (sample_repo / "src" / "run.log").exists(), "the user's checkout is never written"
    finally:
        store.close()


class FlagsAndEditsTheCandidate(FailingOnceThenPassing):
    """A check that flags ``src/parser.py`` assume-unchanged and rewrites it.

    The flag makes ``git status`` trust the index, so the edit is invisible to the reconcile's
    status read: the worktree no longer holds the checked bytes, but looks clean.
    """

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        _git(Path(cwd), "update-index", "--assume-unchanged", "src/parser.py")
        (Path(cwd) / "src" / "parser.py").write_text("# rewritten by a check\n", encoding="utf-8")
        return super().run(check, cwd, timeout_seconds)


def test_an_index_flag_hiding_a_change_refuses_the_repair_as_workspace_drift_before_it_is_bought(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The reconcile's status read cannot vouch for a flagged entry, so the repair is refused."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FlagsAndEditsTheCandidate()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "assume-unchanged" in (outcome.block_reason or "")
        assert "src/parser.py" in (outcome.block_reason or "")
        decisions = [record.decision for record in store.repair_records_for(outcome.run_id)]
        assert decisions == [RepairDecision.ALLOWED, RepairDecision.WORKSPACE_DRIFT]
        assert driver.labels == ["implementer"], "nothing is dispatched for a flagged index"
    finally:
        store.close()


class LeavesAReportFile(FailingOnceThenPassing):
    """A check that writes a report file git does *not* ignore into its cwd."""

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        (Path(cwd) / "check-report.out").write_text("report\n", encoding="utf-8")
        return super().run(check, cwd, timeout_seconds)


def test_an_unignored_check_byproduct_refuses_the_repair_as_workspace_drift_before_it_is_bought(
    tmp_path: Path, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty worktree is refused before the reservation, and recorded as what it is.

    It used to be recorded as ``no_content_change``, which describes a repair that ran and
    changed nothing - not a repair that was never bought because the worktree was dirty.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg-config"))
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": LeavesAReportFile()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert "check-report.out" in (outcome.block_reason or "")
        decisions = [record.decision for record in store.repair_records_for(outcome.run_id)]
        assert decisions == [RepairDecision.ALLOWED, RepairDecision.WORKSPACE_DRIFT]
        assert driver.labels == ["implementer"], "nothing is dispatched for a dirty worktree"
        view = store.root_budget_view(controller.root_binding.root_id)
        assert view is not None and view.used_repairs == 0
    finally:
        store.close()


class JunctionDriver(RepairingDriver):
    """An implementer that replaces the worktree's ``src`` with a junction to a directory outside."""

    def start(self, request):  # noqa: ANN001 - Protocol shape
        result = super().start(request)
        if request.role == "implementer":
            import _winapi
            import shutil

            worktree = Path(request.workspace)
            outside = worktree.parent.parent / "outside-src"
            outside.mkdir(exist_ok=True)
            (outside / "parser.py").write_text("x = 1\n", encoding="utf-8")
            shutil.rmtree(worktree / "src")
            _winapi.CreateJunction(str(outside), str(worktree / "src"))
        return result


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are a Windows feature")
def test_a_scope_entry_that_leaves_the_worktree_blocks_the_run_instead_of_raising(
    tmp_path: Path, scoped_repo: Path
) -> None:
    """A write_allow entry the worker turned into a link out of the worktree is a recorded block.

    It used to raise out of ``run_task`` with the run left RUNNING, so ``resume`` did nothing and
    every later revision on the root was refused as "owned by run" until an operator cancelled.
    """
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    limits = RootBudgetLimits(max_top_level_submissions=12, max_repairs=1)
    spec = _scoped_spec(base, allow=["src"])
    binding = _root_binding_with(store, spec=spec, project_root=scoped_repo, limits=limits)

    def controller_for(spec: TaskSpec, authorization_id: str, driver: FakeDriver) -> Controller:
        record = _artifact_for(
            store, spec=spec, project_root=scoped_repo, binding=binding, limits=limits,
            authorization_id=authorization_id,
        )
        return _controller_on_root(
            store, project_root=scoped_repo, binding=binding, limits=limits, record=record,
            driver=driver, runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
        )

    try:
        driver = JunctionDriver(
            scoped_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={}
        )
        outcome = controller_for(spec, "AUTH-junction-1", driver).run_task(
            _request(project=project, spec=spec, project_root=scoped_repo)
        )
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.SCOPE_VIOLATION, outcome.block_reason
        assert store.get_run(outcome.run_id)["task_state"] == TaskState.BLOCKED.value
        assert driver.labels == ["implementer"], "nothing is frozen, checked or reviewed"
        assert not _git(scoped_repo, "for-each-ref", "refs/hflow").strip(), "no candidate ref"

        second = _spec(policy=None, base_commit=base, revision=2).model_copy(
            update={"scope": spec.scope}
        )
        second_driver = RepairingDriver(
            scoped_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={}
        )
        later = controller_for(second, "AUTH-junction-2", second_driver).run_task(
            _request(project=project, spec=second, project_root=scoped_repo)
        )
        assert "owned by run" not in (later.block_reason or ""), later.block_reason
        assert second_driver.labels, "the root is released for a later revision"
    finally:
        store.close()

# --------------------------------------------------------------------------
# 12. the input an offline agent actually received
# --------------------------------------------------------------------------


class PacketSensitiveDriver(RepairingDriver):
    """A driver that validates the packet it was handed before it acts on it.

    This is the offline stand-in for "an agent whose behaviour depends on its input": it refuses to
    start when a repair round's packet is missing the candidate it is supposed to repair, the
    failure facts it is supposed to act on, or the statement that the previous round's evidence
    does not carry over. An agent that acted anyway would be acting on nothing, so the controller
    must never hand it such a packet in the first place.
    """

    def __init__(self, project_root: Path, **kwargs) -> None:  # noqa: ANN003
        super().__init__(project_root, **kwargs)
        self.repair_packets: list[str] = []

    def start(self, request):  # noqa: ANN001 - Protocol shape
        packet = request.packet
        if "## Repair attempt" in packet:
            self.repair_packets.append(packet)
            required = (
                "- candidate commit:",
                "- candidate fingerpri" + "nt:",
                "- trigger: business_check_failed",
                "### What failed",
                "check unit",
                "previous round's passing checks and verdict do not carry over",
            )
            missing = [needle for needle in required if needle not in packet]
            if missing:
                raise AssertionError(
                    "the repair packet is missing input the agent depends on: "
                    + ", ".join(missing)
                )
        return super().start(request)


def test_an_input_sensitive_agent_sees_the_previous_candidate_and_the_failure_facts(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Acceptance 12: assert from the packet the agent actually received."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = PacketSensitiveDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert driver.repair_packets, "the repair round must have received a repair packet"
        packet = driver.repair_packets[0]
        decisions = store.repair_records_for(outcome.run_id)
        assert decisions[0].failed_checks == ["unit"]
        assert "business assertion failed" in packet, (
            "the packet must carry the recorded failure detail, not a summary invented later"
        )
        # The first attempt's packet is unchanged by E2: no repair section at all.
        assert "## Repair attempt" not in driver.packets[0]
    finally:
        store.close()


def test_a_repair_round_without_its_context_is_refused_before_any_dispatch(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Delete the repair context and the attempt is refused, not sent without one.

    Called directly on the cycle with ``previous`` set and no context, which is exactly the shape a
    wiring mistake would produce. The refusal happens before the dispatch transaction, so no
    allowance is consumed and no implementer is bought.
    """
    from hflow.contracts import CandidateIdentity

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        run_id = "R-repair-no-context"
        store.create_run(
            run_id=run_id,
            project_id=project.project_id,
            spec=spec,
            spec_digest=spec.spec_digest(),
            controller_build="repair-test",
            checks_digest=project.checks_digest(),
            turn_limit=4,
            repair_limit=1,
        )
        # The run has to be claimed by the controller that will drive it: `reopen_for_repair`
        # refuses a run owned by somebody else, which is the ownership rule working.
        store.claim_run(run_id, "local-controller")
        previous = CandidateIdentity(
            round=1,
            attempt_id="A-previous",
            base_commit=base,
            fingerprint="fp-previous",
        )
        cycle = controller._attempt_cycle(
            run_id,
            _request(project=project, spec=spec, project_root=sample_repo),
            repo=None,
            worktree=None,
            execution_root=sample_repo,
            user_tree_before="",
            dirty_target=False,
            implementer_writes=True,
            implementer_packet=None,
            previous=previous,
            trigger=RepairTrigger.BUSINESS_CHECK_FAILED,
            round_number=2,
            original_base_commit=base,
            original_base_ref=f"base:{base}",
            policy=_policy(),
            repair_context=None,
        )
        assert cycle.outcome is not None and cycle.repair is None
        assert cycle.outcome.task_state is TaskState.BLOCKED
        assert "without the recorded repair context" in (cycle.outcome.block_reason or "")
        assert store.invocations_for(run_id) == [], (
            "the refusal must happen before the dispatch transaction"
        )
    finally:
        store.close()


# --------------------------------------------------------------------------
# 14. a root that ran out of time buys nothing
# --------------------------------------------------------------------------


def test_a_root_whose_clock_ran_out_does_not_buy_a_repair(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Acceptance 14: the deadline is a fact in the ledger, not a local timer."""
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    binding, limits, record = _root_setup(store, spec=spec, project_root=sample_repo)

    # A root whose clock has already run out: the same limits a run would normally have,
    # registered as if they had been recorded long ago. The clock is set by the test, not slept
    # for, so the assertion cannot depend on machine speed.
    expired = RootBudgetLimits(
        max_top_level_submissions=limits.max_top_level_submissions,
        max_repairs=limits.max_repairs,
        deadline_seconds=limits.deadline_seconds,
    )
    store.register_root_budget(binding, expired)
    # Move the recorded clock into the past. The deadline is a stored fact, so this is the same
    # state a root that has been alive too long is in - no sleep, no fake clock to install.
    with store.transaction() as conn:
        conn.execute(
            "UPDATE root_budgets SET deadline_at = ? WHERE root_id = ?",
            ("2000-01-01T00:00:00Z", binding.root_id),
        )

    controller = Controller(
        store,
        driver,
        controller_build="repair-test",
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
        data_dir=sample_repo.parent / "data",
        authorization=record,
        root_binding=binding,
        root_limits=expired,
        preflight=lambda: (True, "repair test: no launch binding to probe"),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        # Whatever the code, no second implementer may have been dispatched for an expired root.
        assert len(store.invocations_for(outcome.run_id)) <= 1
    finally:
        store.close()

# --------------------------------------------------------------------------
# 10/13. the gate is the recorded reason, and it is one vocabulary
# --------------------------------------------------------------------------


def test_the_controller_classifies_on_the_same_reason_vocabulary_the_runner_produces() -> None:
    """One vocabulary, two readers: the runner writes `exit_reason`, the gate classifies on it.

    `verify-chain` flagged the drift risk: the offline classification table and the controller's
    real gate both transcribe plan §5.2, so this asserts that the set the gate measures against is
    the set `verify.py` defines - not a copy that can silently diverge.
    """
    from hflow import verify as verify_module
    from hflow.controller import Controller

    source = Path("src/hflow/controller.py").read_text(encoding="utf-8")
    assert "CLEAN_EXIT_REASONS" in source, (
        "the controller must measure against the runner's own vocabulary, not a literal list"
    )
    assert "completed" in verify_module.CLEAN_EXIT_REASONS
    assert "nonzero_exit" in verify_module.CLEAN_EXIT_REASONS
    # Everything else a check can end as is *not* a clean completion, so none of it may repair.
    for reason in (
        "timed_out",
        "settlement_forced",
        "settlement_unknown",
        "output_capture_error",
        "boundary",
        "startup",
        "not_launched",
        "",
    ):
        assert reason not in verify_module.CLEAN_EXIT_REASONS, reason
    assert Controller is not None


def test_the_repaired_candidates_evidence_never_reuses_the_first_rounds_rows(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Acceptance 13: the first round's passing checks cannot accept the second candidate.

    The repair round runs every required check again and gets its own rows, and those rows name the
    new candidate. A receipt therefore cites evidence for the candidate it delivers - which is the
    whole reason `force_refresh` exists.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        verification_rows = [
            row for row in store.evidence_for(outcome.run_id) if row["kind"] == "verification"
        ]
        # Two rounds x two checks: the repaired candidate re-ran all of them.
        assert len(verification_rows) == 4, (
            f"the repair must re-run every required check: {len(verification_rows)} rows"
        )
        fingerprints = {row["candidate_fingerprint"] for row in verification_rows}
        assert len(fingerprints) == 2, "one fingerprint per round, never a shared row"

        receipt = outcome.receipt
        assert receipt is not None
        cited = set(receipt.verification.evidence_ids)
        assert cited, "the receipt must cite the evidence it was accepted on"
        for row in verification_rows:
            if row["evidence_id"] in cited:
                assert row["candidate_fingerprint"] == receipt.candidate.fingerprint, (
                    "an accepted receipt may only cite evidence for the candidate it delivers"
                )
    finally:
        store.close()


def test_the_recorded_failure_detail_survives_into_the_decision(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The decision names what failed and how it ended - the fact an operator reads later."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        decisions = store.repair_records_for(outcome.run_id)
        allowed = decisions[0]
        assert allowed.exit_codes == {"unit": 1}, (
            "the decision records the exit code it acted on, not just the check id"
        )
        assert allowed.trigger.value == "business_check_failed"
        assert allowed.policy_digest, "the decision names the policy that allowed it"
        assert allowed.round == 1, "the decision says which round it was taken in"

        # And the recorded evidence carries the reason, which is what made the classification
        # possible at all: without it the same failure would be ineligible.
        rows = [row for row in store.evidence_for(outcome.run_id) if row["kind"] == "verification"]
        reasons = {row["exit_reason"] for row in rows}
        assert "nonzero_exit" in reasons and "completed" in reasons, reasons
    finally:
        store.close()

class PacketRecorder(FakeDriver):
    """A fake driver that keeps every packet it was handed, in order."""

    def __init__(self, project_root: Path, script: FakeScript) -> None:
        super().__init__(project_root, script)
        self.packets: list[str] = []

    def start(self, request):  # noqa: ANN001 - Protocol shape
        self.packets.append(request.packet)
        return super().start(request)


def test_the_second_rounds_reviewer_is_given_only_the_second_rounds_evidence(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Plan 5.3: the reviewer reads the current attempt's rows, never the run's whole history.

    Both rounds run the same two checks, so the run ends up with four verification rows. The
    reviewer that accepts the repaired candidate is handed one row per required check, for *its*
    candidate - not two rounds of rows, and not the first round's failing row presented as the
    evidence for a candidate that passed.

    What this does **not** prove: that the `attempt_id` filter alone is load-bearing. In this
    scenario the fingerprint filter already separates the rounds (a repair with an identical
    fingerprint is stopped as a no-content-change before a reviewer is bought), so removing the
    attempt filter would leave this test green. The filter is kept because the plan asks for both,
    and because it makes this reader agree with `_accept`; the assertion below is about the
    observable result, not about which of the two filters produced it.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    reviewer = PacketRecorder(
        sample_repo,
        FakeScript(
            outcome=InvocationOutcome.COMPLETED,
            agent_turns=1,
            review=ReviewOutput(verdict="accepted", findings=[]),
        ),
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver, reviewer=reviewer,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert len(reviewer.packets) == 1, (
            "the accepted round is reviewed once: " + str(len(reviewer.packets))
        )
        packet = reviewer.packets[0]
        rows = [row for row in store.evidence_for(outcome.run_id) if row["kind"] == "verification"]
        assert len(rows) == 4, f"two rounds x two checks: {len(rows)}"
        for check_id in ("unit", "lint"):
            occurrences = packet.count(f"- {check_id}:")
            assert occurrences == 1, (
                f"{check_id} appears {occurrences} times in the reviewer packet: a previous "
                "round's row leaked into this round's evidence, or its own row is missing"
            )
            assert f"- {check_id}: not executed" not in packet, (
                f"{check_id} ran in this round, so the reviewer must not be told it did not"
            )
    finally:
        store.close()

def _controller_with_ceiling(
    store: Store,
    *,
    project_root: Path,
    spec: TaskSpec,
    driver: FakeDriver,
    runners: CheckRunners,
    max_submissions: int,
) -> Controller:
    """A controller whose root/authorization can afford exactly `max_submissions` dispatches."""
    binding, limits, record = _root_setup(
        store, spec=spec, project_root=project_root, max_submissions=max_submissions
    )
    return Controller(
        store,
        driver,
        controller_build="repair-test",
        runners=runners,
        data_dir=project_root.parent / "data",
        authorization=record,
        root_binding=binding,
        root_limits=limits,
        preflight=lambda: (True, "repair test: no launch binding to probe"),
    )


def test_a_root_that_cannot_cover_the_worst_case_refuses_before_the_first_dispatch(
    tmp_path: Path, sample_repo: Path
) -> None:
    """A repair armed on a root with only two submissions left must not start at all.

    Three dispatches is the cheapest repair there is (I1 fails a check, I2 fixes it, R2 accepts),
    and two is the normal loop. A root that can afford two but not three would run the first
    implementer and then discover it cannot finish - which is the exact waste the plan's
    worst-case gate removes. Nothing may be dispatched, so the root's counters stay at zero.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller_with_ceiling(
        store,
        project_root=sample_repo,
        spec=spec,
        driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
        max_submissions=2,
    )
    try:
        # The refusal happens before a run row exists, so it surfaces as a refusal rather than as a
        # blocked run. Either shape is fine; what must hold is that nothing was dispatched and the
        # root's ledger did not move.
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert "needs 4 more" in excinfo.value.message, excinfo.value.message
        assert "Nothing was dispatched" in excinfo.value.message
        view = store.root_budget_view(controller.root_binding.root_id)
        assert view is not None and view.used_top_level_submissions == 0, (
            "nothing was bought, so the root's ledger must not have moved"
        )
    finally:
        store.close()


def test_a_root_that_can_cover_the_worst_case_still_only_spends_two_on_success(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The gate is a ceiling, not a quota: a first-attempt success spends two of the four."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller_with_ceiling(
        store,
        project_root=sample_repo,
        spec=spec,
        driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
        max_submissions=4,
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        view = store.root_budget_view(controller.root_binding.root_id)
        assert view is not None
        assert view.used_top_level_submissions == 2, (
            "a run that never needed its repair must not spend the worst case"
        )
        assert view.used_repairs == 0, (
            "no repair was dispatched, so the root's repair counter must still be zero"
        )
    finally:
        store.close()


# --------------------------------------------------------------------------
# the root's repair counter is a pre-I1 fact, and a real transport's repair needs a root
# --------------------------------------------------------------------------


def _root_binding_with(store: Store, *, spec: TaskSpec, project_root: Path, limits):
    """Register this task's root with explicit ceilings; ``_root_setup`` fixes max_repairs=1."""
    from hflow.contracts import RootBudgetBinding

    binding = RootBudgetBinding.derive(
        project_id="repair-project",
        repo_path=str(project_root),
        task_id=spec.task_id,
        ledger_path=store.path,
    )
    store.register_root_budget(binding, limits)
    return binding


def _artifact_for(
    store: Store, *, spec: TaskSpec, project_root: Path, binding, limits, authorization_id: str
):
    """One registered artifact for this exact revision and root: each revision needs its own."""
    from hflow.authorization import AuthorizationBinding, AuthorizationRecord

    record = AuthorizationRecord(
        authorization_id=authorization_id,
        user_text="I approve one bounded run of this exact task against this root.",
        authorized_at="2026-09-26T00:00:00Z",
        max_top_level_submissions=4,
        binding=AuthorizationBinding(
            mode="m2-live-change",
            driver="fake",
            project_id="repair-project",
            repo_path=str(project_root),
            base_commit=spec.workspace.base_commit,
            spec_digest=spec.spec_digest(),
            spec_path=str(project_root / "task.json"),
            root_budget=binding,
        ),
        root_limits=limits,
    )
    store.register_authorization(record.as_store_record())
    return record


def _controller_on_root(
    store: Store,
    *,
    project_root: Path,
    binding,
    limits,
    record,
    driver: FakeDriver,
    runners: CheckRunners,
    reviewer: FakeDriver | None = None,
) -> Controller:
    return Controller(
        store,
        driver,
        reviewer_driver=reviewer,
        controller_build="repair-test",
        runners=runners,
        data_dir=project_root.parent / "data",
        authorization=record,
        root_binding=binding,
        root_limits=limits,
        preflight=lambda: (True, "repair test: no launch binding to probe"),
    )


@pytest.mark.parametrize("trigger", ["business_check", "reviewer"])
def test_a_root_with_no_repair_left_refuses_an_armed_policy_before_the_first_dispatch(
    tmp_path: Path, sample_repo: Path, trigger: str
) -> None:
    """``max_repairs`` defaults to 0: an armed policy on such a root must not buy I1 (or R1).

    Before, the run paid for I1 (and R1 on the reviewer path), recorded the repair as allowed and
    then died as an internal error when the repair's reservation hit the ceiling.
    """
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    limits = RootBudgetLimits(max_top_level_submissions=4)
    assert limits.max_repairs == 0
    binding = _root_binding_with(store, spec=spec, project_root=sample_repo, limits=limits)
    record = _artifact_for(
        store, spec=spec, project_root=sample_repo, binding=binding, limits=limits,
        authorization_id="AUTH-no-repair-left",
    )
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    reviewer = None
    runner = FailingOnceThenPassing()
    if trigger == "reviewer":
        runner = FailingOnceThenPassing(fail_first=None)
        reviewer = RepairingDriver(
            sample_repo,
            first_plan={},
            repair_plan={},
            review=ReviewOutput(
                verdict="changes_requested",
                findings=[{"severity": "major", "statement": "parse(None) still returns None"}],
            ),
        )
    controller = _controller_on_root(
        store, project_root=sample_repo, binding=binding, limits=limits, record=record,
        driver=driver, runners=CheckRunners({"fake": runner}), reviewer=reviewer,
    )
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED, excinfo.value
        assert "0 repair" in excinfo.value.message, excinfo.value.message
        assert driver.labels == [] and (reviewer is None or reviewer.labels == []), (
            "nothing may be dispatched for a repair the root cannot pay for"
        )
        assert store.find_run_by_spec_digest(project.project_id, spec.spec_digest()) is None
        view = store.root_budget_view(binding.root_id)
        assert view is not None
        assert (view.used_top_level_submissions, view.used_repairs) == (0, 0)
        assert store.invocations_for_root(binding.root_id) == []
    finally:
        store.close()


def test_a_root_refused_for_its_repair_counter_records_nothing_and_a_corrected_root_runs(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The refusal's own advice works: the refused run never registered the root.

    Before, the root row was written with ``max_repairs=0`` before the repair gate refused, so
    the corrected root file (``max_repairs=1``) was then refused as a limits mismatch forever -
    there is no top-up path. Every refusal decidable before a write now runs first.
    """
    from hflow.authorization import AuthorizationBinding, AuthorizationRecord
    from hflow.contracts import RootBudgetBinding, RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    binding = RootBudgetBinding.derive(
        project_id="repair-project",
        repo_path=str(sample_repo),
        task_id=spec.task_id,
        ledger_path=store.path,
    )

    def artifact(limits: RootBudgetLimits, authorization_id: str) -> AuthorizationRecord:
        # Not registered by the test: whether the refused run registers it is what is measured.
        return AuthorizationRecord(
            authorization_id=authorization_id,
            user_text="I approve one bounded run of this exact task against this root.",
            authorized_at="2026-09-26T00:00:00Z",
            max_top_level_submissions=4,
            binding=AuthorizationBinding(
                mode="m2-live-change",
                driver="fake",
                project_id="repair-project",
                repo_path=str(sample_repo),
                base_commit=spec.workspace.base_commit,
                spec_digest=spec.spec_digest(),
                spec_path=str(sample_repo / "task.json"),
                root_budget=binding,
            ),
            root_limits=limits,
        )

    def controller(limits: RootBudgetLimits, authorization_id: str, driver) -> Controller:
        return _controller_on_root(
            store, project_root=sample_repo, binding=binding, limits=limits,
            record=artifact(limits, authorization_id), driver=driver,
            runners=CheckRunners({"fake": FailingOnceThenPassing()}),
        )

    try:
        zero = RootBudgetLimits(max_top_level_submissions=4)
        refused_driver = RepairingDriver(
            sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
        )
        with pytest.raises(RefusedError) as excinfo:
            controller(zero, "AUTH-zero-repairs", refused_driver).run_task(
                _request(project=project, spec=spec, project_root=sample_repo)
            )
        assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED, excinfo.value
        assert "0 repair" in excinfo.value.message, excinfo.value.message
        assert refused_driver.labels == []
        assert store.root_budget_view(binding.root_id) is None, (
            "a refused run must not lock the root at the ceilings it was refused for"
        )
        assert store.find_run_by_spec_digest(project.project_id, spec.spec_digest()) is None
        assert store.authorization_state("AUTH-zero-repairs") is None

        one = RootBudgetLimits(max_top_level_submissions=4, max_repairs=1)
        driver = RepairingDriver(
            sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
        )
        outcome = controller(one, "AUTH-one-repair", driver).run_task(
            _request(project=project, spec=spec, project_root=sample_repo)
        )
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        view = store.root_budget_view(binding.root_id)
        assert view is not None
        assert view.limits.max_repairs == 1 and view.used_repairs == 1
    finally:
        store.close()


def test_a_later_revision_after_the_root_spent_its_repair_is_a_budget_refusal(
    tmp_path: Path, sample_repo: Path
) -> None:
    """A later revision's I1 is the root's repair (E1), so a spent counter refuses it as budget.

    Without a policy the store's own ceiling refuses the reservation, which must read as
    BUDGET_EXHAUSTED rather than INTERNAL_ERROR. With a policy the run would need two repairs
    (its I1 and its own repair), so it is refused before a run row exists.
    """
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    limits = RootBudgetLimits(max_top_level_submissions=12, max_repairs=1)
    first = _spec(policy=_policy(), base_commit=base)
    binding = _root_binding_with(store, spec=first, project_root=sample_repo, limits=limits)

    def controller_for(spec: TaskSpec, authorization_id: str, driver: FakeDriver) -> Controller:
        record = _artifact_for(
            store, spec=spec, project_root=sample_repo, binding=binding, limits=limits,
            authorization_id=authorization_id,
        )
        return _controller_on_root(
            store, project_root=sample_repo, binding=binding, limits=limits, record=record,
            driver=driver, runners=CheckRunners({"fake": FailingOnceThenPassing()}),
        )

    try:
        first_driver = RepairingDriver(
            sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
        )
        outcome = controller_for(first, "AUTH-rev-1", first_driver).run_task(
            _request(project=project, spec=first, project_root=sample_repo)
        )
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        view = store.root_budget_view(binding.root_id)
        assert view is not None and view.used_repairs == 1

        second = _spec(policy=None, base_commit=base, revision=2)
        second_driver = RepairingDriver(sample_repo, first_plan={}, repair_plan={})
        outcome = controller_for(second, "AUTH-rev-2", second_driver).run_task(
            _request(project=project, spec=second, project_root=sample_repo)
        )
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED, (
            outcome.block_code,
            outcome.block_reason,
        )
        assert "repair attempt" in (outcome.block_reason or ""), outcome.block_reason
        assert second_driver.labels == [], "the refused revision must not start a driver"

        third = _spec(policy=_policy(), base_commit=base, revision=3)
        third_driver = RepairingDriver(sample_repo, first_plan={}, repair_plan={})
        with pytest.raises(RefusedError) as excinfo:
            controller_for(third, "AUTH-rev-3", third_driver).run_task(
                _request(project=project, spec=third, project_root=sample_repo)
            )
        assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED, excinfo.value
        assert third_driver.labels == []
        assert store.find_run_by_spec_digest(project.project_id, third.spec_digest()) is None

        view = store.root_budget_view(binding.root_id)
        assert view is not None
        assert (view.used_top_level_submissions, view.used_repairs) == (3, 1), (
            "neither refused revision may move the root's counters"
        )
    finally:
        store.close()


class SpendsTheRootRepairWhileChecking(FailingOnceThenPassing):
    """Fails ``unit`` once - and while that check runs, the root's last repair is used up.

    Stands in for whatever spent the counter between admission and the repair decision. The
    decision has to read the root it would charge, not assume admission's answer still holds.
    """

    def __init__(self, store: Store, root_id: str) -> None:
        super().__init__()
        self.store = store
        self.root_id = root_id

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
        if not self.seen:
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE root_budgets SET used_repairs = max_repairs WHERE root_id = ?",
                    (self.root_id,),
                )
        return super().run(check, cwd, timeout_seconds)


def test_a_repair_decision_with_no_root_repair_left_records_budget_exhausted(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The decision reads the root view: no repair left is BUDGET_EXHAUSTED, never 'allowed'."""
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=1)
    binding = _root_binding_with(store, spec=spec, project_root=sample_repo, limits=limits)
    record = _artifact_for(
        store, spec=spec, project_root=sample_repo, binding=binding, limits=limits,
        authorization_id="AUTH-spent-mid-run",
    )
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller_on_root(
        store, project_root=sample_repo, binding=binding, limits=limits, record=record,
        driver=driver,
        runners=CheckRunners({"fake": SpendsTheRootRepairWhileChecking(store, binding.root_id)}),
    )
    request = _request(project=project, spec=spec, project_root=sample_repo)
    try:
        outcome = controller.run_task(request)
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.VERIFICATION_FAILED, outcome.block_reason
        decisions = store.repair_records_for(outcome.run_id)
        assert [entry.decision for entry in decisions] == [RepairDecision.BUDGET_EXHAUSTED]
        assert "1/1" in decisions[0].reason, decisions[0].reason
        assert driver.labels == ["implementer"], "no repair may be dispatched"
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == ["implementer"]
    finally:
        store.close()


def _packet_deadlines(packet: str) -> tuple[int, int]:
    """``(runtime deadline, repair section's remaining deadline)`` as rendered in one packet."""
    import re

    runtime = re.search(r"^- deadline: (\d+) seconds$", packet, re.MULTILINE)
    remaining = re.search(r"^- remaining deadline: (\d+) seconds$", packet, re.MULTILINE)
    assert runtime is not None and remaining is not None, packet
    return int(runtime.group(1)), int(remaining.group(1))


@pytest.mark.parametrize("rooted", [False, True])
def test_the_repair_packet_states_the_deadline_the_repair_actually_has(
    tmp_path: Path, sample_repo: Path, rooted: bool
) -> None:
    """Never ``0`` for a clock that never started, never more than the attempt is given.

    Offline and rootless (the fake driver may still repair without a root) the repair has its
    configured deadline; on a root it has that, capped by what the root's clock has left. Either
    way the repair section and the packet's own runtime line state one number.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    runners = CheckRunners({"fake": FailingOnceThenPassing()})
    if rooted:
        controller = _controller(
            store, project_root=sample_repo, spec=spec, driver=driver, runners=runners
        )
    else:
        controller = Controller(
            store,
            driver,
            controller_build="repair-test",
            runners=runners,
            data_dir=sample_repo.parent / "data",
        )
    request = _request(project=project, spec=spec, project_root=sample_repo)
    try:
        outcome = controller.run_task(request)
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert driver.labels == ["implementer", "implementer-repair", "reviewer"]
        runtime, remaining = _packet_deadlines(driver.packets[1])
        assert remaining > 0, "a repair is never told it has no time left by a fallback"
        assert remaining == runtime <= request.deadline_seconds
    finally:
        store.close()


class DeadlineRecorder(RepairingDriver):
    """Keeps the deadline each invocation was actually given, next to the packet it was sent."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.deadlines: list[int] = []

    def start(self, request):  # noqa: ANN001 - Protocol shape
        self.deadlines.append(int(request.deadline_seconds))
        return super().start(request)


def test_the_first_packet_states_the_deadline_the_first_invocation_is_given(
    tmp_path: Path, sample_repo: Path
) -> None:
    """A root whose deadline is shorter than the request's: the packet and the invocation agree.

    The first packet used to be rendered with the configured 900 seconds while the invocation was
    given the root-capped value, so the worker was told it had more time than was enforced.
    """
    from hflow.contracts import RootBudgetLimits

    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=None, base_commit=base)
    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=1, deadline_seconds=600)
    binding = _root_binding_with(store, spec=spec, project_root=sample_repo, limits=limits)
    record = _artifact_for(
        store, spec=spec, project_root=sample_repo, binding=binding, limits=limits,
        authorization_id="AUTH-deadline",
    )
    driver = DeadlineRecorder(sample_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={})
    controller = _controller_on_root(
        store, project_root=sample_repo, binding=binding, limits=limits, record=record,
        driver=driver, runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    request = _request(project=project, spec=spec, project_root=sample_repo)
    assert request.deadline_seconds > limits.deadline_seconds, "the case needs a capped deadline"
    try:
        outcome = controller.run_task(request)
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        import re

        stated = re.search(r"^- deadline: (\d+) seconds$", driver.packets[0], re.MULTILINE)
        assert stated is not None
        assert int(stated.group(1)) == driver.deadlines[0] <= limits.deadline_seconds
    finally:
        store.close()


class RealTransportStandIn(RepairingDriver):
    """Identifies as the production transport; still reaches no model (it is the fake inside)."""

    driver_id = AcpxDshDriver.driver_id


def test_a_real_transport_repair_without_a_root_is_refused_before_a_run_exists(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Plan 5.1: the run's own gate refuses what ``prepare`` reports, before anything is created."""
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = RealTransportStandIn(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = Controller(
        store,
        driver,
        controller_build="repair-test",
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
        data_dir=sample_repo.parent / "data",
    )
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert excinfo.value.code is RefusalCode.BUDGET_EXCEEDED, excinfo.value
        assert "--root-budget-file" in excinfo.value.message, excinfo.value.message
        assert driver.labels == []
        assert store.find_run_by_spec_digest(project.project_id, spec.spec_digest()) is None
    finally:
        store.close()

# --------------------------------------------------------------------------
# E2 review round: the three counterexamples, now formal regression
# --------------------------------------------------------------------------


def test_the_delivery_base_and_paths_cover_both_rounds(tmp_path: Path, sample_repo: Path) -> None:
    """The receipt must describe the whole change, not the last round's patch.

    Round one changes A, round two changes B. The final candidate really contains both, but the
    second round's freeze only knows its own parent and the paths it touched - so a receipt built
    from that would hand an integration step a delivery that appears to change only B, and A would
    be silently dropped. The receipt's base is therefore the task's original base and its paths are
    the diff between that base and the final candidate, computed from the two commits.
    """
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base).model_copy(
        update={
            "scope": Scope(
                write_allow=["src/parser.py", "src/second.py"], write_deny=[".hflow/**"]
            )
        }
    )
    driver = RepairingDriver(
        sample_repo,
        # Round one edits A and fails the check; round two edits B only.
        first_plan={"src/parser.py": "# first round\ndef parse(text):\n    return text\n"},
        repair_plan={"src/second.py": "# second round\n"},
    )
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        receipt = outcome.receipt
        assert receipt is not None
        actual = _git(
            sample_repo, "diff", "--name-only", base, receipt.candidate.git_commit
        ).split()
        assert set(actual) == {"src/parser.py", "src/second.py"}, (
            f"both rounds' changes must be in the final candidate: {actual}"
        )
        assert receipt.candidate.base_commit == base, (
            "the receipt's base must be the task's original base, not the repaired round's parent"
        )
        assert set(receipt.candidate_paths) == set(actual), (
            f"the receipt must name the cumulative paths {sorted(actual)}, "
            f"not just the last round's: {sorted(receipt.candidate_paths)}"
        )
    finally:
        store.close()


# --------------------------------------------------------------------------
# one resolved base: a ref names a commit once, and the reviewer sees the whole change
# --------------------------------------------------------------------------


def _reviewer_packets(driver: RepairingDriver) -> list[str]:
    """The packets this driver was handed as the reviewer, in order."""
    return [packet for label, packet in zip(driver.labels, driver.packets) if label == "reviewer"]


class BranchMovingDriver(RepairingDriver):
    """Commits an unrelated file to the user's ``main`` while the first implementer works.

    This is the user carrying on in their own checkout during a run, which worktree isolation
    exists to allow. The commit is the test's (the user's), never the run's.
    """

    def __init__(self, project_root: Path, **kwargs: object) -> None:
        super().__init__(project_root, **kwargs)  # type: ignore[arg-type]
        self.user_repo = project_root
        self.moved_to = ""

    def start(self, request):  # noqa: ANN001 - Protocol shape
        if request.role == "implementer" and not self.moved_to:
            (self.user_repo / "user_notes.txt").write_text("the user's own work\n", encoding="utf-8")
            _git(self.user_repo, "add", "user_notes.txt")
            _git(self.user_repo, "commit", "-q", "-m", "user work while the run is going")
            self.moved_to = _git(self.user_repo, "rev-parse", "HEAD").strip()
        return super().start(request)


def test_a_head_base_is_resolved_to_the_commit_the_run_started_from(
    tmp_path: Path, sample_repo: Path
) -> None:
    """``base_commit='HEAD'`` is a ref, not a base: the receipt names the commit it resolved to.

    Inside the run's worktree HEAD *is* the frozen candidate, so a delivery diff taken against the
    literal name came out empty, and the receipt named 'HEAD' as the base of a change it then hid.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    start = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=None, base_commit="HEAD")
    driver = RepairingDriver(
        sample_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FakeCheckRunner()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        receipt = outcome.receipt
        assert receipt is not None
        assert receipt.candidate.base_commit == start, (
            f"the receipt must name the resolved commit, not the ref: {receipt.candidate.base_commit!r}"
        )
        assert receipt.candidate_paths == ["src/parser.py"], receipt.candidate_paths
        notes = store.notes_for(outcome.run_id)
        assert any(f"base 'HEAD' -> {start}" in note for note in notes), notes
        (packet,) = _reviewer_packets(driver)
        assert f"git diff {start} {receipt.candidate.git_commit}" in packet
    finally:
        store.close()


def test_a_branch_base_that_moves_mid_run_keeps_the_commit_the_run_started_from(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The user commits to ``main`` while the run works: the delivery is still measured from the start.

    Resolving the name again at acceptance would diff the candidate against the moved tip, and the
    user's unrelated file would appear in the delivery as if the candidate had removed it.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    start = _git(sample_repo, "rev-parse", "main").strip()
    spec = _spec(policy=None, base_commit="main")
    driver = BranchMovingDriver(
        sample_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={}
    )
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FakeCheckRunner()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert driver.moved_to and driver.moved_to != start, "the branch really moved mid-run"
        receipt = outcome.receipt
        assert receipt is not None
        assert receipt.candidate.base_commit == start, receipt.candidate.base_commit
        assert receipt.candidate_paths == ["src/parser.py"], (
            f"the user's commit is not part of this delivery: {receipt.candidate_paths}"
        )
        assert not (Path(receipt.candidate.worktree) / "user_notes.txt").exists()
        assert any(f"base 'main' -> {start}" in note for note in store.notes_for(outcome.run_id))
        (packet,) = _reviewer_packets(driver)
        assert f"git diff {start} {receipt.candidate.git_commit}" in packet
        assert "user_notes.txt" not in packet
    finally:
        store.close()


def test_the_repair_rounds_reviewer_sees_the_whole_change_from_the_original_base(
    tmp_path: Path, scoped_repo: Path
) -> None:
    """I1 fails a check, I2 repairs, R2 is the only reviewer: it must be shown round one's change.

    Round one writes ``src/a.py`` and fails ``unit``, so no reviewer is bought for it. Round two
    writes ``src/parser.py``. The receipt delivers both from the original base, so the one reviewer
    that votes on that delivery is told the original base, both paths and the cumulative diff -
    and, separately and labelled, the patch this round added on top of round one's candidate.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src/parser.py", "src/a.py"], policy=_policy())
    driver = RepairingDriver(
        scoped_repo,
        first_plan={"src/a.py": "LEGACY = False\n"},
        repair_plan={"src/parser.py": FIXED_SOURCE},
    )
    controller = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=scoped_repo))
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert driver.labels == ["implementer", "implementer-repair", "reviewer"], driver.labels
        receipt = outcome.receipt
        assert receipt is not None
        final = receipt.candidate.git_commit
        first_round = _git(scoped_repo, "rev-parse", f"{final}^").strip()
        assert first_round != base, "round one really committed a change"
        assert receipt.candidate_paths == ["src/a.py", "src/parser.py"]

        (packet,) = _reviewer_packets(driver)
        assert f"- base commit the candidate was produced from: {base}" in packet
        assert f"git diff {base} {final}" in packet, "the diff to read is the whole delivery"
        assert "- paths changed from the base commit: src/a.py, src/parser.py" in packet
        assert f"produced from: {first_round}" not in packet
        # This round's own patch is offered too, labelled, never in place of the cumulative one.
        assert "this round's change (repair round 2" in packet
        assert f"git diff {first_round} {final}" in packet
        assert "- paths this round changed: src/parser.py" in packet
    finally:
        store.close()


def test_an_oversize_repair_packet_is_refused_before_any_reservation(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Size is decided for the *repair* packet, before the dispatch transaction buys it.

    A repair packet carries the previous candidate and the failure facts, so it can be the first
    packet that does not fit even though the first round's did. Rendering it after the reservation
    would charge the root for an attempt that never started, and the refusal would claim nothing
    had been consumed while the ledger said otherwise.
    """
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base).model_copy(update={"goal": "G" * 30000})
    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        error: Exception | None = None
        outcome = None
        try:
            outcome = controller.run_task(
                _request(project=project, spec=spec, project_root=sample_repo)
            )
        except Exception as exc:  # noqa: BLE001 - the assertion is that this does not happen
            error = exc
        assert len(driver.labels) == 1, (
            f"only the first attempt may reach the driver: labels={driver.labels}, error={error}"
        )
        first_bytes = len(driver.packets[0].encode("utf-8"))
        assert first_bytes <= 32768, f"the first packet must fit to reach this case: {first_bytes}B"
        root = store.root_budget_view(controller.root_binding.root_id)
        assert root is not None
        assert root.used_top_level_submissions == 1, (
            f"the refused repair must not be charged: used={root.used_top_level_submissions} "
            f"(first packet {first_bytes}B); error={error!r}; outcome={outcome}"
        )
        assert error is None, f"an oversized packet is a decision, not an exception: {error!r}"
        assert outcome is not None and outcome.task_state is TaskState.BLOCKED, (
            "the run must block with a recorded reason rather than raise"
        )
    finally:
        store.close()


def test_findings_that_overflow_the_repair_packet_are_refused_before_the_repair_is_bought(
    tmp_path: Path, sample_repo: Path
) -> None:
    """Every finding is rendered whole (within its per-value cap), so many of them can overflow.

    The bound is not met by dropping findings: the repair packet is rendered before the
    reservation, its size is refused, and neither the second implementer nor the root pays.
    """
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    findings = [
        {"severity": "major", "required_fix": f"fix {index}: " + "y" * 1500}
        for index in range(30)
    ]

    class FloodRejection(FakeDriver):
        def start(self, request):  # noqa: ANN001 - Protocol shape
            result = super().start(request)
            if request.role != "reviewer":
                return result
            return result.model_copy(
                update={"review": ReviewOutput(verdict="changes_requested", findings=findings)}
            )

    driver = RepairingDriver(
        sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    controller = _controller(
        store,
        project_root=sample_repo,
        spec=spec,
        driver=driver,
        reviewer=FloodRejection(
            sample_repo,
            FakeScript(
                outcome=InvocationOutcome.COMPLETED,
                agent_turns=1,
                review=ReviewOutput(verdict="accepted", findings=[]),
            ),
        ),
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED, outcome
        assert "repair implementer input packet" in (outcome.block_reason or ""), outcome
        assert [entry.role for entry in store.invocations_for(outcome.run_id)] == [
            "implementer",
            "reviewer",
        ], "an oversize repair packet must not reach the driver"
        assert len(driver.labels) == 1
        root = store.root_budget_view(controller.root_binding.root_id)
        assert root is not None and root.used_top_level_submissions == 2
    finally:
        store.close()


def test_a_wide_first_round_does_not_push_the_repair_packet_over_its_bound(
    tmp_path: Path, sample_repo: Path
) -> None:
    """The previous round's paths are listed with a cap and a reference, not in full.

    One directory entry in write_allow admits any number of changed files, so a rename sweep in
    round one used to make every repair packet exceed 32 KiB after ``allowed`` was recorded.
    """
    count = 1100
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base).model_copy(
        update={"scope": Scope(write_allow=["src"], write_deny=[".hflow/**"])}
    )
    first_plan = {f"src/generated/module_{index:04d}.py": f"X = {index}\n" for index in range(count)}
    driver = RepairingDriver(
        sample_repo, first_plan=first_plan, repair_plan={"src/parser.py": FIXED_SOURCE}
    )
    store = Store(tmp_path / "hflow.sqlite")
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing()}),
    )
    try:
        outcome = controller.run_task(_request(project=_project(), spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
        assert driver.labels == ["implementer", "implementer-repair", "reviewer"]
        repair_packet = driver.packets[1]
        assert len(repair_packet.encode("utf-8")) <= 32 * 1024
        assert f"(+{count - 20} more)" in repair_packet
        # ``--no-renames``, like every path list HFlow records: with rename detection the command
        # would print only a moved file's new name, not the path set HFlow scope-checked.
        assert (
            f"- full path list: git diff --no-renames --name-only {base} " in repair_packet
        ), "the full list travels by reference"
    finally:
        store.close()


def test_the_root_clock_caps_checks_and_the_reviewer_and_refuses_an_expired_acceptance(
    tmp_path: Path, sample_repo: Path, monkeypatch
) -> None:
    """A deadline must bound the work, not sit beside it.

    With a controlled clock: the implementer leaves one second on the root, and the reviewer
    consumes it. Before this case was fixed, each check still received its full configured 600
    seconds, the reviewer received 900, and the run was accepted after the deadline had passed -
    so the recorded deadline bounded nothing.
    """
    from datetime import datetime, timedelta, timezone

    import hflow.controller as controller_module
    import hflow.store as store_module

    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]

    def utc_now() -> str:
        return now[0].isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(controller_module, "utc_now", utc_now)
    monkeypatch.setattr(store_module, "utc_now", utc_now)
    observed: list[tuple[str, int]] = []

    class ClockDriver(RepairingDriver):
        def start(self, request):  # noqa: ANN001 - Protocol shape
            observed.append((request.role, request.deadline_seconds))
            result = super().start(request)
            # The root's default clock is 86400s: leave 1 second after the implementer, and let the
            # reviewer run past the deadline.
            now[0] += timedelta(seconds=86399 if request.role == "implementer" else 2)
            return result

    class CheckClock(FailingOnceThenPassing):
        def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
            observed.append((check.id, timeout_seconds))
            return super().run(check, cwd, timeout_seconds)

    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = ClockDriver(sample_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={})
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": CheckClock(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is not TaskState.ACCEPTED, (
            "a run whose clock ran out must not be accepted"
        )
        # Every step after the implementer got at most the 1 second the root had left.
        over_budget = [
            (name, timeout) for name, timeout in observed if name != "implementer" and timeout > 1
        ]
        assert not over_budget, (
            f"steps were given more time than the root had left: {over_budget} "
            f"(all configured values: {observed})"
        )
    finally:
        store.close()

# --------------------------------------------------------------------------
# E2 review round 2: the two remaining deadline branches
# --------------------------------------------------------------------------


def test_the_checks_share_one_countdown_rather_than_a_fresh_budget_each(
    tmp_path: Path, sample_repo: Path, monkeypatch
) -> None:
    """A budget handed to every check is not a bound: it has to be decremented by what each spent.

    With a controlled clock: the implementer leaves two seconds on the root, the first check
    consumes more than that, and the second check must therefore never start. Before this was
    fixed each check received the same two seconds again, so a two-second remainder could buy four
    seconds of checking - and the run would then accept a candidate produced after its deadline.
    """
    import hflow.controller as controller_module
    import hflow.store as store_module
    import hflow.verify as verify_module
    from types import SimpleNamespace

    epoch = datetime(2030, 1, 1, tzinfo=timezone.utc)
    elapsed = [0]

    def utc_now() -> str:
        return (epoch + timedelta(seconds=elapsed[0])).isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(controller_module, "utc_now", utc_now)
    monkeypatch.setattr(store_module, "utc_now", utc_now)
    # The check loop measures elapsed time monotonically; the test drives that clock explicitly.
    monkeypatch.setattr(verify_module, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    seen: list[tuple[str, int]] = []

    class ClockDriver(RepairingDriver):
        def start(self, request):  # noqa: ANN001 - Protocol shape
            result = super().start(request)
            if request.role == "implementer":
                elapsed[0] = 86398  # two seconds left on the recorded root clock
            return result

    class SpendingChecks(FailingOnceThenPassing):
        def run(self, check, cwd, timeout_seconds):  # noqa: ANN001 - Protocol shape
            seen.append((check.id, timeout_seconds))
            result = super().run(check, cwd, timeout_seconds)
            elapsed[0] += 3  # the check plus its finite settlement consumed the remainder
            return result

    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    spec = _spec(policy=_policy(), base_commit=base)
    driver = ClockDriver(sample_repo, first_plan={"src/parser.py": FIXED_SOURCE}, repair_plan={})
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    controller = _controller(
        store, project_root=sample_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": SpendingChecks(fail_first=None)}),
    )
    try:
        outcome = controller.run_task(_request(project=project, spec=spec, project_root=sample_repo))
        assert outcome.task_state is TaskState.BLOCKED
        assert [name for name, _ in seen] == ["unit"], (
            f"the second check was started after the root expired: {seen}; elapsed={elapsed[0]}"
        )
        assert seen and seen[0][1] <= 2, (
            f"the first check must be capped by the two seconds left, not its own 600: {seen}"
        )
        # The detail records the exhausted budget without starting a process for that check.
        rows = [
            row
            for row in store.evidence_for(outcome.run_id)
            if row["kind"] == "verification" and row["check_id"] == "lint"
        ]
        assert rows and "not started" in rows[0]["detail"], rows
    finally:
        store.close()


def test_the_local_driver_wait_is_bounded_by_the_invocation_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    """A hung client cannot enforce its own `--timeout`, so the driver must hold the deadline.

    The client is handed its deadline as a flag, but a client stuck before protocol startup never
    reads it. The local wait used the driver's own completion timeout instead, so a 1-second
    invocation deadline waited 3 seconds, reported unknown, and left the client process alive. The
    fix waits for the smaller of the two and stops the client through the managed process boundary,
    keeping the unknown outcome - what the client might have produced was never observed.
    """
    client = tmp_path / "hung_client.py"
    client.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    driver = AcpxDshDriver(
        data_dir=tmp_path / "data",
        acpx_cli=client,
        python_executable=sys.executable,
        completion_timeout_seconds=3,
        agent_argv_override=[sys.executable, "-c", "pass"],
    )
    invocation = InvocationRequest(
        invocation_id="I-deadline",
        attempt_id="A-deadline",
        run_id="R-deadline",
        role="implementer",
        task_id="T-deadline",
        task_revision=1,
        goal="offline deadline probe",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(workspace),
        deadline_seconds=1,
        spec_digest="sha256:test",
        data_dir=str(tmp_path / "data"),
    )
    handle = driver.start_handle(invocation)
    process = driver._processes[handle.invocation_id]
    waits: list[float | None] = []
    original_wait = process.wait

    def recorded_wait(timeout=None):  # noqa: ANN001 - mirrors Popen.wait
        waits.append(timeout)
        return original_wait(timeout=timeout)

    monkeypatch.setattr(process, "wait", recorded_wait)
    try:
        result = driver.collect(handle)
        alive = process.poll() is None
        assert result.outcome is not InvocationOutcome.COMPLETED
        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN, (
            "an unobserved client is unknown, not failed: " + repr(result.outcome)
        )
        assert waits and waits[0] is not None and waits[0] <= 1, (
            f"the wait must be bounded by the 1s invocation deadline, not 3s: {waits}"
        )
        assert not alive, (
            f"the hung client was left running after the deadline (pid={handle.pid}); "
            f"outcome={result.outcome}, error={result.error_code}"
        )
    finally:
        if process.poll() is None:
            driver.cancel_handle(handle)
        driver.release(handle.invocation_id)

@pytest.mark.skipif(os.name != "nt", reason="observes the Windows managed process boundary")
def test_an_expired_deadline_stops_the_whole_process_tree_not_just_the_client(
    tmp_path: Path, monkeypatch
) -> None:
    """The client exiting (or being killed) is not the tree being stopped.

    The client starts a child of its own and then blocks. The deadline expires, the client is
    released during the graceful teardown, and the driver must still stop the *tree*: an earlier
    version returned early when the client had exited, reported the boundary as empty, and skipped
    the one action that would have stopped the surviving child.

    Both halves are asserted, and they are different facts:

    * the boundary is empty - the managed job holds nothing, which is what "the tree is stopped"
      means and what the plan's process-boundary rule rests on. ``collect`` closes the job once
      the teardown is done, so this is read from what the driver recorded when it closed it;
    * the child has terminated - checked by its exit code, then confirmed gone within a bounded
      settle. An un-timed "not gone" is not evidence of a live process: ``process_gone`` asks
      whether the process object is signalled, and Windows keeps that object alive while any handle
      to it is open, so a terminated process can still be opened. The exit code is the fact that
      separates them.
    """
    client = tmp_path / "parent.py"
    child_pid_file = tmp_path / "child.pid"
    release = tmp_path / "parent.release"
    client.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path(os.environ['PROBE_CHILD_PID']).write_text(str(child.pid))\n"
        "print('{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"session/prompt\",\"params\":{}}',"
        " flush=True)\n"
        "while not Path(os.environ['PROBE_PARENT_RELEASE']).exists():\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    driver = AcpxDshDriver(
        data_dir=tmp_path / "data",
        acpx_cli=client,
        python_executable=sys.executable,
        completion_timeout_seconds=3,
        agent_argv_override=[sys.executable, "-c", "pass"],
    )
    driver.extra_env.update(
        PROBE_CHILD_PID=str(child_pid_file), PROBE_PARENT_RELEASE=str(release)
    )
    invocation = InvocationRequest(
        invocation_id="I-tree-timeout",
        attempt_id="A-tree-timeout",
        run_id="R-tree-timeout",
        role="implementer",
        task_id="T-tree-timeout",
        task_revision=1,
        goal="offline deadline teardown",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(workspace),
        deadline_seconds=1,
        spec_digest="sha256:test",
        data_dir=str(tmp_path / "data"),
    )
    handle = driver.start_handle(invocation)
    process = driver._processes[handle.invocation_id]
    boundary = driver._boundaries[handle.invocation_id]
    child_pid = 0
    try:
        readiness_deadline = time.monotonic() + 5
        while not (child_pid_file.exists() and handle.dispatched):
            assert time.monotonic() < readiness_deadline, "client never reported readiness"
            time.sleep(0.01)
        child_pid = int(child_pid_file.read_text())
        assert boundary.contains(child_pid) is True, (
            "the probe's child must be inside the managed boundary, or this case tests nothing"
        )

        original_wait = process.wait
        waits: list[float | None] = []

        def exit_client_during_grace(timeout=None):  # noqa: ANN001 - mirrors Popen.wait
            waits.append(timeout)
            if len(waits) == 2:
                # The first wait is collect's deadline; the second is the graceful teardown.
                # Release the real client here, leaving its real child in the same job.
                release.touch()
            return original_wait(timeout=timeout)

        monkeypatch.setattr(process, "wait", exit_client_during_grace)
        result = driver.collect(handle)

        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN, (
            "an unobserved client is unknown, not failed: " + repr(result.outcome)
        )
        # Half one: nothing was left in the managed boundary when the driver closed it.
        recorded = driver._exit_boundaries.get(handle.invocation_id)
        assert recorded is not None and recorded.emptied is True, (
            f"the boundary still held processes after teardown: {recorded}; "
            f"waits={waits}, detail={result.error_message}"
        )
        assert any("boundary_empty=True" in note for note in result.limitations), result.limitations
        assert boundary.handle is None, "the job is closed once the teardown is recorded"
        assert process._hflow_stdout.closed is True, "the stdout file handle is closed too"
        # Half two: the child was terminated, confirmed by its exit code within a bounded settle.
        settled = _wait_for_exit(child_pid, timeout_seconds=5.0)
        assert settled, (
            f"the child of the client survived the teardown: pid={child_pid}, "
            f"recorded={recorded}, detail={result.error_message}"
        )
    finally:
        boundary.terminate()
        boundary.wait_empty(5)
        if child_pid and not _wait_for_exit(child_pid, timeout_seconds=1.0):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(child_pid)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        driver.release(handle.invocation_id)
