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
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    deadline = time.monotonic() + timeout_seconds
    while True:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return True  # no process object at all: gone
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
        self.labels: list[str] = []
        self.packets: list[str] = []

    def start(self, request):  # noqa: ANN001 - Protocol shape
        is_repair = "## Repair attempt" in request.packet
        self.labels.append(f"{request.role}{'-repair' if is_repair else ''}")
        self.packets.append(request.packet)
        # The plan for *this* implementer invocation: the first one changes nothing, the repair
        # one applies the fix.
        if request.role == "implementer":
            self.script.write_plan = dict(self.repair_plan if is_repair else self.first_plan)
        return super().start(request)


def _root_setup(store: Store, *, spec: TaskSpec, project_root: Path, max_submissions: int = 4):
    """A registered root plus its artifact: the shape a real run has since batch E1.

    E2's repair is only reachable through the root ledger - a root charge names the artifact that
    bought it - so an end-to-end test has to set one up. The ledger path is the store's own file,
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


def test_an_empty_reviewer_rejection_never_buys_a_repair(
    tmp_path: Path, sample_repo: Path
) -> None:
    """A rejection with no usable finding says nothing to change, so nothing is bought."""
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
                update={"review": ReviewOutput(verdict="changes_requested", findings=[])}
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
      means and what the plan's process-boundary rule rests on;
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
        # Half one: nothing is left in the managed boundary.
        assert boundary.active_processes() == 0, (
            f"the boundary still holds processes after teardown: {boundary.active_processes()}; "
            f"waits={waits}, detail={result.error_message}"
        )
        # Half two: the child was terminated, confirmed by its exit code within a bounded settle.
        settled = _wait_for_exit(child_pid, timeout_seconds=5.0)
        assert settled, (
            f"the child of the client survived the teardown: pid={child_pid}, "
            f"active={boundary.active_processes()}, detail={result.error_message}"
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
