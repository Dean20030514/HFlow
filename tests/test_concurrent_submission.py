"""Two identical submissions, and a stop during setup, must not relabel a run.

Every CLI process uses the default controller id, so ``claim_run`` does not tell two ``hflow run``
processes on the identical TaskSpec apart: the second one passes the "admitted but never
dispatched" branch while the first is still setting the run up. What keeps the run honest is
in the writes themselves:

* no writer that sets ``BLOCKED`` relabels a run that already ended (a terminal state or a
  delivery receipt);
* a controller that reserved none of the run's attempts does not block the run another
  controller is driving;
* the store's "second live attempt" and "terminal run" refusals return the existing run with a
  note instead of an ``internal_error`` block.

Owner identity across processes (leases, per-process controller ids) is not implemented; both
submissions may still run setup. These tests force each interleaving with real threads and
``Event`` coordination, never sleeps.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hflow.contracts import (
    CancellationReceipt,
    RefusalCode,
    RefusedError,
    RunRequest,
    TaskState,
)
from hflow.controller import Controller, RunOutcome
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner


def _controller(store: Store, project_root: Path, fake_script: FakeScript) -> Controller:
    runner = FakeCheckRunner()
    return Controller(
        store,
        FakeDriver(project_root, fake_script),
        controller_build="test-build",
        runners=CheckRunners({"fake": runner, "command": runner}),
    )


def _race_while_the_first_implementer_runs(
    store: Store,
    first: Controller,
    second: Controller,
    run_request: RunRequest,
) -> tuple[RunOutcome, object, str]:
    """B's identical submission passes ``run_task`` while A is in setup, then acts while A's
    implementer is in flight. Returns A's outcome, B's outcome (or exception) and the stored
    task state observed while A's implementer was still running."""
    b_ready, a_started = threading.Event(), threading.Event()
    result: dict[str, object] = {}

    original_b_cycle = second._attempt_cycle

    def b_cycle(*args, **kwargs):  # noqa: ANN002, ANN003
        b_ready.set()
        assert a_started.wait(10)
        return original_b_cycle(*args, **kwargs)

    second._attempt_cycle = b_cycle  # type: ignore[method-assign]

    def run_b() -> None:
        try:
            result["b"] = second.run_task(run_request)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            result["b"] = exc

    thread = threading.Thread(target=run_b)
    original_a_cycle = first._attempt_cycle

    def a_cycle(*args, **kwargs):  # noqa: ANN002, ANN003
        thread.start()  # B's identical submission arrives while A is still in setup
        assert b_ready.wait(10)
        return original_a_cycle(*args, **kwargs)

    first._attempt_cycle = a_cycle  # type: ignore[method-assign]

    original_start = first.driver.start

    def a_start(request):  # noqa: ANN001 - Protocol shape
        if request.role == "implementer":
            a_started.set()
            thread.join(10)
            assert not thread.is_alive()
            result["state_in_flight"] = store.get_run(request.run_id)["task_state"]
        return original_start(request)

    first.driver.start = a_start  # type: ignore[method-assign]

    outcome = first.run_task(run_request)
    return outcome, result["b"], str(result["state_in_flight"])


def test_a_second_identical_submission_does_not_block_the_live_run(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """B's reservation meets A's live attempt: B reports the run, A's paid work is accepted."""
    first = _controller(store, project_root, fake_script)
    second = _controller(store, project_root, fake_script)

    outcome, b_outcome, state_in_flight = _race_while_the_first_implementer_runs(
        store, first, second, run_request
    )

    assert isinstance(b_outcome, RunOutcome), b_outcome
    assert b_outcome.run_id == outcome.run_id
    assert b_outcome.block_code is None, b_outcome.block_reason
    assert any("concurrent identical submission" in note for note in b_outcome.notes), (
        b_outcome.notes
    )
    assert state_in_flight == TaskState.RUNNING.value, (
        "the second submission relabelled the run while the first one's implementer was running"
    )
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert outcome.implementer_invocations == 1


def test_a_pre_dispatch_refusal_of_the_second_submission_is_not_the_runs_outcome(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """B holds none of the run's attempts, so its own refusal is a note, not a block."""
    first = _controller(store, project_root, fake_script)
    second = _controller(store, project_root, fake_script)

    def refuse(**_kwargs):  # noqa: ANN003
        raise RefusedError(RefusalCode.INVALID_SPEC, "the packet does not fit (forced)")

    second._render_implementer_packet = refuse  # type: ignore[method-assign]

    outcome, b_outcome, state_in_flight = _race_while_the_first_implementer_runs(
        store, first, second, run_request
    )

    assert isinstance(b_outcome, RunOutcome), b_outcome
    assert b_outcome.block_code is None, b_outcome.block_reason
    notes = " ".join(b_outcome.notes)
    assert "invalid_spec block was not recorded" in notes, notes
    assert "reserved by another controller" in notes, notes
    assert state_in_flight == TaskState.RUNNING.value
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)


def test_an_accepted_run_keeps_its_outcome_when_the_other_submission_reserves_late(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """B completes the whole run first; A's late reservation must not relabel the receipt."""
    first = _controller(store, project_root, fake_script)
    second = _controller(store, project_root, fake_script)
    original = store.reserve_dispatch
    state: dict[str, object] = {"fired": False}

    def wrapped(**kwargs):  # noqa: ANN003
        if not state["fired"]:
            state["fired"] = True
            # B submits the identical TaskSpec while A is between create_run and its reservation.
            state["b"] = second.run_task(run_request)
        return original(**kwargs)

    store.reserve_dispatch = wrapped  # type: ignore[method-assign]
    outcome = first.run_task(run_request)

    b_outcome = state["b"]
    assert isinstance(b_outcome, RunOutcome)
    assert b_outcome.task_state is TaskState.ACCEPTED, b_outcome.block_reason
    assert outcome.run_id == b_outcome.run_id
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert any("concurrent identical submission" in note for note in outcome.notes), outcome.notes
    row = store.get_run(outcome.run_id)
    assert row["task_state"] == TaskState.ACCEPTED.value
    assert row["block_code"] is None
    assert row["receipt_json"]


# --------------------------------------------------------------------------
# the store's own guard: no BLOCKED writer relabels a recorded outcome
# --------------------------------------------------------------------------


def _accepted_run(controller: Controller, run_request: RunRequest) -> str:
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    return outcome.run_id


def test_no_blocked_writer_relabels_an_accepted_run(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    run_id = _accepted_run(_controller(store, project_root, fake_script), run_request)
    receipt = store.get_run(run_id)["receipt_json"]

    assert store.block_unless_stopped(run_id, RefusalCode.INTERNAL_ERROR, "late") is False
    row = store.set_blocked(run_id, RefusalCode.INTERNAL_ERROR, "late")
    assert row["task_state"] == TaskState.ACCEPTED.value
    stop = CancellationReceipt(
        invocation_id="",
        status="confirmed_stopped",
        mechanism="none",
        local_process_stopped=True,
        detail="late",
    )
    assert store.record_cancel_outcome(
        run_id, stop, RefusalCode.CANCELLED_BY_OPERATOR, "late"
    ) is False

    after = store.get_run(run_id)
    assert after["task_state"] == TaskState.ACCEPTED.value
    assert after["block_code"] is None
    assert after["receipt_json"] == receipt
    assert after["cancel_receipt_json"] is None


def test_a_blocked_run_keeps_its_first_block(store: Store, run_request: RunRequest) -> None:
    spec = run_request.task
    run_id = store.create_run(
        run_id="R-first-block",
        project_id=run_request.project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="test-build",
        checks_digest=run_request.project.checks_digest(),
        turn_limit=4,
        repair_limit=0,
    )["run_id"]

    assert store.block_unless_stopped(run_id, RefusalCode.DRIVER_FAILED, "first") is True
    assert store.block_unless_stopped(run_id, RefusalCode.INTERNAL_ERROR, "second") is False
    store.set_blocked(run_id, RefusalCode.INTERNAL_ERROR, "third")
    row = store.get_run(run_id)
    assert (row["block_code"], row["block_reason"]) == (RefusalCode.DRIVER_FAILED.value, "first")


# --------------------------------------------------------------------------
# a stop during _drive setup
# --------------------------------------------------------------------------


def test_a_cancel_during_setup_is_reported_not_raised(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """``hflow cancel`` from another shell lands before DRAFT -> READY: the stop is the outcome.

    Before the fix the transition raised ``StoreError`` out of ``run_task`` (the run was already
    ``BLOCKED``), and ``hflow run`` died with a traceback.
    """
    controller = _controller(store, project_root, fake_script)
    other_shell = _controller(store, project_root, fake_script)
    original = store.set_task_state
    fired: dict[str, object] = {}

    def wrapped(run_id, expected, new_state, **kwargs):  # noqa: ANN001, ANN003
        if not fired:
            fired["receipt"] = other_shell.cancel(run_id)
        return original(run_id, expected, new_state, **kwargs)

    store.set_task_state = wrapped  # type: ignore[method-assign]
    outcome = controller.run_task(run_request)

    receipt = fired["receipt"]
    assert isinstance(receipt, CancellationReceipt)
    assert receipt.status == "confirmed_stopped"
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR, outcome.block_reason
    assert any("a stop had already decided" in note for note in outcome.notes), outcome.notes
    assert outcome.implementer_invocations == 0
    assert store.attempts_for(outcome.run_id) == []
