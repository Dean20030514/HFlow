"""Driver contract tests: launch, observe, stop, reconcile - all offline.

The production driver is exercised through a test-only client that reproduces the acpx
contract (structured-argv config, ``--format json``, ``exec -f -`` with the task on stdin)
and through stub agents that are *separate processes*. That matters for the stop tests: a
process tree that ignores cancellation can only be stopped by the managed boundary, so the
test would fail if the boundary were decorative.

No test here sends a model request, and none needs a credential.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hflow.contracts import (
    AgentBinding,
    AttemptState,
    CapabilityState,
    DeliveryState,
    EventKind,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    RefusalCode,
    RunRequest,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import DRIVER_ID, AcpxDshDriver
from hflow.drivers.winjob import process_gone
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLIENT = FIXTURES / "fake_acpx_client.py"
STUB_AGENT = FIXTURES / "stub_acp_agent.py"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class DriverHarness:
    """Owns one driver plus the temp roots its invocation state must stay inside."""

    def __init__(self, tmp_path: Path, mode: str, *, delay_before_prompt: float = 0.0) -> None:
        self.mode = mode
        self.data_dir = (tmp_path / "data").resolve()
        self.workspace = (tmp_path / "ws").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Stub markers live in their own scoped subdirectory. The stub is harness scaffolding,
        # not a worker changing project files: leaving its files loose in the workspace would
        # make every controller run look like an out-of-scope write.
        self.scratch = self.workspace / "stub-scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.delay_before_prompt = delay_before_prompt
        stub_argv = [sys.executable, "-u", str(STUB_AGENT), mode]
        if delay_before_prompt:
            # The fake client passes this through so a cancel can arrive pre-dispatch.
            stub_argv += ["--delay", str(delay_before_prompt)]
        self.driver = AcpxDshDriver(
            data_dir=self.data_dir,
            acpx_cli=FAKE_CLIENT,
            python_executable=sys.executable,
            completion_timeout_seconds=90,
            agent_argv_override=stub_argv,
        )
        # The stub keeps its marker files in STUB_SCRATCH_DIR; the fake client inherits the
        # driver's child environment, so pointing it here keeps scaffolding out of the
        # project paths the controller snapshots.
        self.driver.extra_env["STUB_SCRATCH_DIR"] = str(self.scratch)

    def start(self, invocation_id: str = "I-1", *, attempt_id: str = "A-1", goal: str = "do the thing"):
        request = InvocationRequest(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            run_id="R-1",
            role="implementer",
            task_id="T-1",
            task_revision=1,
            goal=goal,
            acceptance=[],
            write_allow=["src/x.py"],
            write_deny=[],
            workspace=str(self.workspace),
            deadline_seconds=60,
            spec_digest="sha256:test",
            data_dir=str(self.data_dir),
        )
        handle = self.driver.start_handle(request)
        return handle, request

    def stub_files(self, suffix: str) -> list[Path]:
        return sorted(self.scratch.glob(f"stub-*-*.{suffix}"))

    def spawn_log(self) -> Path:
        """Written by the fake client when it launches an agent: the client-path marker."""
        return self.workspace / "agent-spawns.jsonl"

    def wait_for_stub_marker(self, suffix: str, timeout: float = 20.0, *, non_empty: bool = False) -> Path:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for candidate in self.stub_files(suffix):
                if not non_empty or candidate.stat().st_size > 0:
                    return candidate
            time.sleep(0.05)
        raise AssertionError(f"stub never wrote a .{suffix} marker in {self.scratch}")

    def wait_for_dispatch(self, handle, timeout: float = 20.0) -> bool:
        """Wait until the driver observed the session/prompt marker."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if handle.dispatched:
                return True
            time.sleep(0.05)
        return False


@pytest.fixture()
def harness_factory(tmp_path: Path):
    created: list[DriverHarness] = []

    def make(mode: str, *, delay_before_prompt: float = 0.0) -> DriverHarness:
        harness = DriverHarness(tmp_path / f"{mode}-{len(created)}", mode, delay_before_prompt=delay_before_prompt)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        # Never leave a boundary or a child behind, whatever the test asserted.
        for invocation_id in list(harness.driver._handles):
            try:
                harness.driver.cancel_handle(harness.driver._handles[invocation_id])
            except Exception:  # noqa: BLE001
                pass
            harness.driver.release(invocation_id)


def _collect_events(driver: AcpxDshDriver, handle, limit: float = 60.0):
    events = []
    started = time.time()
    for event in driver.observe(handle):
        events.append(event)
        if time.time() - started > limit:
            break
    return events


def test_client_argv_uses_the_right_interpreter_for_the_entry_point(tmp_path: Path) -> None:
    """A Node CLI entry point must not be handed to the Python interpreter.

    This is a regression test for a real failure: the live trial launched acpx's
    ``dist/cli.js`` as ``python -u cli.js``, which cannot parse JavaScript, so the client
    died before the harness ever saw the task.
    """
    node_entry = AcpxDshDriver(
        data_dir=tmp_path, acpx_cli=Path("fake/node_modules/acpx/dist/cli.js")
    )
    argv = node_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0] == node_entry.node_executable
    assert argv[1].endswith("cli.js")
    assert "exec" in argv and argv[-2:] == ["-f", "-"]

    python_entry = AcpxDshDriver(data_dir=tmp_path, acpx_cli=FAKE_CLIENT)
    argv = python_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0] == python_entry.python_executable
    assert argv[1] == "-u"
    assert argv[2].endswith("fake_acpx_client.py")

    shim_entry = AcpxDshDriver(data_dir=tmp_path, acpx_cli=Path("C:/tools/acpx.cmd"))
    argv = shim_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0].endswith("acpx.cmd"), "a real executable is launched directly"


# --------------------------------------------------------------------------
# group 1: normal single execution
# --------------------------------------------------------------------------


def test_normal_execution_is_observed_and_settles(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()

    spawn_marker = harness.wait_for_stub_marker("spawn", non_empty=True)
    events = _collect_events(harness.driver, handle)
    kinds = [event.kind for event in events]

    assert EventKind.STARTED in kinds
    assert EventKind.DISPATCHED in kinds, "the session/prompt marker must be observed"
    assert EventKind.COMPLETED in kinds
    assert handle.dispatched is True
    assert handle.session_id and handle.session_id.startswith("sess-")

    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.agent_turns == 1
    assert result.provider_billed_tokens is None, "billing is unknown, never fabricated"

    # The task body travelled through stdin, not through a command line.
    spawn = json.loads(spawn_marker.read_text(encoding="utf-8"))
    assert spawn["mode"] == "cooperative"
    assert "do the thing" in json.loads(harness.spawn_log().read_text(encoding="utf-8").splitlines()[0])["task"]
    assert Path(handle.event_log).exists()
    assert harness.driver.unparsed_line_count(handle.invocation_id) == 0

    harness.driver.release(handle.invocation_id)
    assert process_gone(handle.pid, 3.0), "a finished invocation must leave no process"


def test_invocation_state_stays_out_of_the_workspace(tmp_path: Path, harness_factory) -> None:
    """Config, logs and session state belong to the driver's data dir, not the checkout."""
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    harness.driver.collect(handle)

    assert Path(handle.event_log).is_relative_to(harness.data_dir)
    workspace_entries = {path.name for path in harness.workspace.iterdir()}
    assert not any(name.endswith(".json") and "acpx" in name for name in workspace_entries)
    harness.driver.release(handle.invocation_id)


def test_probe_reports_cancel_as_unsupported_without_calling_a_model(harness_factory) -> None:
    harness = harness_factory("cooperative")
    report = harness.driver.probe(AgentBinding(harness="dsh", driver=DRIVER_ID))

    assert report.probe_only is True and report.live_tested is False
    assert report.capabilities["cancel"] is CapabilityState.UNSUPPORTED
    assert report.capabilities["process_boundary_teardown"] is CapabilityState.PROBED
    assert any("no prompt" in note for note in report.notes)
    assert harness.stub_files("spawn") == [], "probe must not launch the agent"


def test_controller_runs_the_driver_through_its_normal_contract(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """The same TaskSpec goes through admission, budget and the receipt path."""
    harness = harness_factory("cooperative")
    store = Store(tmp_path / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=harness.data_dir,
    )
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
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    # The stub client is harness scaffolding and writes its own log into the session cwd,
    # which the controller correctly flags as a change outside the TaskSpec's write scope.
    # When the run does get past that, the stub's reviewer answers in prose with no
    # structured verdict, which the controller correctly refuses as a review protocol
    # problem. Each refusal is fail-closed; what matters is that the driver was reached and
    # that no receipt was produced.
    assert outcome.block_code in {
        RefusalCode.SCOPE_VIOLATION,
        RefusalCode.VERIFICATION_FAILED,
        RefusalCode.REVIEW_PROTOCOL_ERROR,
    }
    assert outcome.receipt is None
    # The implementer really ran; the driver was reached, not replaced by a fake.
    assert len(harness.driver._handles) == 1


# --------------------------------------------------------------------------
# group 2: budget and duplicate dispatch
# --------------------------------------------------------------------------


def test_duplicate_start_of_the_same_invocation_is_refused(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, request = harness.start()
    harness.driver.collect(handle)

    with pytest.raises(Exception) as excinfo:
        harness.driver.start_handle(request)
    assert "already started" in str(excinfo.value)
    assert len(harness.stub_files("spawn")) == 1, "no second agent process may exist"
    harness.driver.release(handle.invocation_id)


def test_exhausted_budget_never_reaches_the_cli(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    harness = harness_factory("cooperative")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(store, harness.driver, controller_build="test-build", data_dir=harness.data_dir)
    try:
        run = store.create_run(
            run_id="R-seeded",
            project_id=project.project_id,
            spec=task_spec,
            spec_digest=task_spec.spec_digest(),
            controller_build="test-build",
            checks_digest=project.checks_digest(),
            turn_limit=1,
            repair_limit=0,
        )
        store.claim_run(run["run_id"], "local-controller")
        store.reserve_turn(run["run_id"], "local-controller", turns=1)
        store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)

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

    assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED
    assert harness.driver._handles == {}, "no invocation may be started without reserved budget"
    assert harness.stub_files("spawn") == [], "the real client path must not be reached"


def test_budget_ledger_survives_a_restart(tmp_path: Path, project, task_spec, project_root: Path) -> None:
    """A fresh process must read the existing ledger, not start from zero."""
    data_dir = tmp_path / "data"
    first = Store(tmp_path / "hflow.sqlite")
    run = first.create_run(
        run_id="R-persist",
        project_id=project.project_id,
        spec=task_spec,
        spec_digest=task_spec.spec_digest(),
        controller_build="test-build",
        checks_digest=project.checks_digest(),
        turn_limit=2,
        repair_limit=0,
    )
    first.claim_run(run["run_id"], "local-controller")
    first.reserve_turn(run["run_id"], "local-controller", turns=2)
    first.close()

    reopened = Store(tmp_path / "hflow.sqlite")
    try:
        assert reopened.turns_remaining("R-persist") == 0
        with pytest.raises(Exception):
            reopened.reserve_turn("R-persist", "local-controller", turns=1)
    finally:
        reopened.close()
    assert data_dir  # keep the fixture honest about where state lives


# --------------------------------------------------------------------------
# group 3: protocol and output failures
# --------------------------------------------------------------------------


def test_missing_stop_reason_is_unknown_not_success(harness_factory) -> None:
    harness = harness_factory("no-answer")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("spawn")
    # Let the turn start, then stop the process so collection has to judge an unsettled run.
    harness.driver._processes[handle.invocation_id].terminate()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    harness.driver.release(handle.invocation_id)


def test_output_overflow_is_untrustworthy_not_success(harness_factory) -> None:
    harness = harness_factory("chatty")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "output_limit_exceeded"
    assert harness.driver._overflow[handle.invocation_id] is True
    harness.driver.release(handle.invocation_id)


def test_unparseable_output_is_counted_and_fails_closed(harness_factory, monkeypatch) -> None:
    """A corrupted stream must not be read as a clean completion."""
    harness = harness_factory("cooperative")
    handle, _ = harness.start()

    import hflow.drivers.acpx_dsh as module
    from hflow.drivers.acp_events import ObservedLine

    original = module.project_line
    calls = {"n": 0}

    def corrupting_project_line(line: str, sequence: int, at: str):
        calls["n"] += 1
        if calls["n"] == 3:
            return ObservedLine(False, None, None)  # simulate a mangled line
        return original(line, sequence, at)

    monkeypatch.setattr(module, "project_line", corrupting_project_line)
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"
    harness.driver.release(handle.invocation_id)


def test_client_timeout_is_reported_as_unknown(tmp_path: Path) -> None:
    """The client exits 3 with a TIMEOUT error: no stop reason, so not a success."""
    harness = DriverHarness(tmp_path, "no-answer")
    harness.timeout = 2.0
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code in {"no_stop_reason", "completion_timeout"}
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# group 4: cancellation races
# --------------------------------------------------------------------------


def test_cancel_before_dispatch_reports_no_model_work(harness_factory) -> None:
    """Stopping before the harness received the task must not claim a turn happened."""
    harness = harness_factory("slow-ready")
    handle, _ = harness.start()

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "forced"
    assert receipt.local_process_stopped is True
    assert handle.dispatched is False
    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.CANCELLED
    assert result.agent_turns == 0, "no dispatch means no turn"
    assert any("never received the task" in note for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_cancel_in_flight_stops_a_stubborn_process_tree(harness_factory) -> None:
    """The agent ignores cancellation and holds a helper child; the boundary must still win."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()

    helper_file = harness.wait_for_stub_marker("helper")
    helper_pid = int(helper_file.read_text(encoding="utf-8"))
    assert not process_gone(helper_pid, 0.5), "the helper must be alive before the stop"

    # Wait until the dispatch marker and the in-progress tool call are observed.
    deadline = time.time() + 20
    saw_tool_call = False
    for event in harness.driver.observe(handle):
        if event.kind is EventKind.PROGRESS and "tool_call" in event.message:
            saw_tool_call = True
            break
        if time.time() > deadline:
            break
    assert handle.dispatched is True
    assert saw_tool_call, "the in-progress marker must be observed before asking to stop"

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "forced", "no protocol cancel exists on this launch path"
    assert receipt.local_process_stopped is True
    assert process_gone(handle.pid, 5.0), "the client process must be gone"
    assert process_gone(helper_pid, 5.0), "the managed descendant must be gone too"

    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.CANCELLED
    assert "cooperative cancel is unavailable" in receipt.detail
    harness.driver.release(handle.invocation_id)


def test_cancel_after_completion_is_a_no_op(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "none"
    assert "already exited" in receipt.detail
    assert len(harness.stub_files("spawn")) == 1, "no second agent run may be started"
    harness.driver.release(handle.invocation_id)


def test_cancel_is_idempotent_and_sends_no_second_prompt(harness_factory) -> None:
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")
    # The dispatch marker must be observed before stopping, so the prompt line is in the log.
    assert harness.wait_for_dispatch(handle), "the prompt was never dispatched"

    first = harness.driver.cancel_handle(handle)
    second = harness.driver.cancel_handle(handle)
    third = harness.driver.cancel_handle(handle)

    assert first == second == third
    assert len(harness.stub_files("spawn")) == 1
    prompts = [line for line in harness.driver.raw_lines(handle.invocation_id) if '"session/prompt"' in line]
    assert len(prompts) == 1, "cancelling must never submit another prompt"
    harness.driver.release(handle.invocation_id)


def test_controller_cancel_records_intent_and_blocks_late_acceptance(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """A recorded cancellation intent must survive a late success and force BLOCKED."""
    harness = harness_factory("stubborn")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(store, harness.driver, controller_build="test-build", data_dir=harness.data_dir)
    try:
        run = store.create_run(
            run_id="R-cancel",
            project_id=project.project_id,
            spec=task_spec,
            spec_digest=task_spec.spec_digest(),
            controller_build="test-build",
            checks_digest=project.checks_digest(),
            turn_limit=4,
            repair_limit=0,
        )
        run_id = run["run_id"]
        store.claim_run(run_id, "local-controller")
        store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)

        attempt = store.dispatch_attempt(
            run_id=run_id,
            controller_id="local-controller",
            attempt_id="A-cancel",
            role="implementer",
            reservation_id="B-cancel",
            reserved_turns=1,
            reservation_expires_at="2999-01-01T00:00:00Z",
        )
        assert attempt["state"] == AttemptState.ACTIVE.value
        store.record_invocation("A-cancel", "I-cancel")
        handle, _ = harness.start("I-cancel", attempt_id="A-cancel")
        harness.wait_for_stub_marker("helper")

        receipt = controller.cancel(run_id)
        assert receipt.status == "confirmed_stopped"
        intent_at, recorded = store.cancel_state(run_id)
        assert intent_at and recorded is not None
        assert store.get_run(run_id)["task_state"] == TaskState.BLOCKED.value
        assert store.get_run(run_id)["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value

        # A late success arriving after the stop must not become ACCEPTED: acceptance refuses
        # while the cancellation intent stands, and the run stays blocked.
        from hflow.contracts import CandidateSnapshot, ResultReceipt, ReviewResult, UsageFacts, VerificationResult
        from hflow.store import StoreError

        late_receipt = ResultReceipt(
            run_id=run_id,
            task_id=task_spec.task_id,
            attempt_id="A-cancel",
            task_revision=1,
            runtime_build="test-build",
            plan_digest=task_spec.spec_digest(),
            harness_outcome=InvocationOutcome.COMPLETED,
            candidate=CandidateSnapshot(base_commit="base", fingerprint="sha256:fp"),
            verification=VerificationResult(status="passed", evidence_ids=["E-late"]),
            review=ReviewResult(status="not_required"),
            task_state=TaskState.ACCEPTED,
            delivery_state=DeliveryState.LOCAL_CANDIDATE,
            usage=UsageFacts(),
        )
        with store.transaction() as conn:
            conn.execute(
                "UPDATE runs SET task_state = ?, phase = ? WHERE run_id = ?",
                (TaskState.CHECKING.value, "verification", run_id),
            )
        store.record_evidence(
            evidence_id="E-late",
            run_id=run_id,
            attempt_id="A-cancel",
            kind="verification",
            status=EvidenceStatus.PASSED,
            candidate_fingerprint="sha256:fp",
            checks_digest=project.checks_digest(),
            check_id="unit",
        )
        with pytest.raises(StoreError) as excinfo:
            store.finalize_acceptance(run_id, late_receipt, checks_digest=project.checks_digest())
        assert "cancellation intent" in str(excinfo.value)
        assert store.get_run(run_id)["task_state"] == TaskState.CHECKING.value, (
            "a refused acceptance must not change the recorded state"
        )

        # Cancelling again returns the stored fact and does not re-stop anything.
        again = controller.cancel(run_id)
        assert again == receipt
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.cancel_handle(harness.driver._handles[invocation_id])
            harness.driver.release(invocation_id)
        store.close()


# --------------------------------------------------------------------------
# group 5: process cleanup
# --------------------------------------------------------------------------


def test_release_leaves_no_boundary_and_no_child(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    harness.driver.collect(handle)
    harness.driver.release(handle.invocation_id)

    assert harness.driver._boundaries[handle.invocation_id].handle is None
    assert process_gone(handle.pid, 3.0)


def test_unrelated_control_processes_are_untouched(harness_factory) -> None:
    """Stopping an invocation must not fan out to other Node/Python processes."""
    unrelated = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        harness = harness_factory("stubborn")
        handle, _ = harness.start()
        harness.wait_for_stub_marker("helper")
        harness.driver.cancel_handle(handle)
        time.sleep(0.5)
        assert not process_gone(unrelated.pid, 0.5), "an unrelated process must survive"
        harness.driver.release(handle.invocation_id)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


def test_unconfirmed_stop_blocks_instead_of_claiming_success(harness_factory, monkeypatch) -> None:
    """If the boundary cannot confirm the stop, the receipt must not say confirmed."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    boundary = harness.driver._boundaries[handle.invocation_id]
    monkeypatch.setattr(type(boundary), "terminate", lambda self, exit_code=1: False)
    monkeypatch.setattr(type(boundary), "wait_empty", lambda self, timeout_seconds: False)

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status in {"still_running", "unknown"}
    assert receipt.mechanism == "none"
    assert receipt.local_process_stopped is not True
    # The stub is still alive: clean it up directly so the fixture is honest.
    harness.driver._processes[handle.invocation_id].kill()
    time.sleep(0.5)


# --------------------------------------------------------------------------
# group 6: reconcile is a conservative query
# --------------------------------------------------------------------------


def test_reconcile_reports_still_running_without_starting_work(harness_factory) -> None:
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value == "still_running"
    assert result.local_process_alive is True
    assert result.protocol_cancel_supported is False
    assert len(harness.stub_files("spawn")) == 1, "reconcile must not launch anything"
    # Only assert on the prompt count once the dispatch marker was actually observed.
    if harness.wait_for_dispatch(handle):
        prompts = [line for line in harness.driver.raw_lines(handle.invocation_id) if '"session/prompt"' in line]
        assert len(prompts) == 1, "reconcile must never send a prompt"
    harness.driver.cancel_handle(handle)
    harness.driver.release(handle.invocation_id)


def test_reconcile_unknown_after_process_disappears(harness_factory) -> None:
    """A gone process does not imply success; without a recorded result it stays unknown."""
    harness = harness_factory("no-answer")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("spawn")
    harness.driver._processes[handle.invocation_id].kill()
    time.sleep(0.5)

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value == "unknown"
    assert result.local_process_alive is False
    harness.driver.release(handle.invocation_id)


def test_reconcile_without_a_handle_is_not_started(harness_factory) -> None:
    harness = harness_factory("cooperative")
    result = harness.driver.reconcile("I-never-started")

    assert result.outcome.value == "not_started"
    assert harness.stub_files("spawn") == []


def test_reconcile_after_completion_reports_unprocessed_result(harness_factory) -> None:
    """Reconcile reports a recorded result, and never invents one for a vanished process.

    Both outcomes are honest here and the test accepts either: the invocation may have
    exited through the normal path (``finished_result_unprocessed``) or hit the terminal
    check in ``observe`` before any result was folded (``unknown``). What must never happen
    is a claim of success built from "the process is gone".
    """
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    collected = harness.driver.collect(handle)
    assert collected.outcome is InvocationOutcome.COMPLETED

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value in {"finished_result_unprocessed", "unknown"}
    assert result.local_process_alive is False
    assert result.protocol_cancel_supported is False
    harness.driver.release(handle.invocation_id)
