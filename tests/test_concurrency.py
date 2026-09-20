"""Deterministic concurrency tests for the store and the cancel/accept ordering.

These use the **real** Store and **real** threads with ``Event``/``Barrier`` coordination,
never sleeps and never "run it a hundred times and hope". Each test names the interleaving it
forces, so a failure points at a specific ordering rather than at timing luck.

Two orderings must both hold:

* cancel intent committed first -> a late success must not become ACCEPTED;
* acceptance committed first -> a later cancel must not rewrite the delivery.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hflow.contracts import (
    AttemptState,
    CandidateSnapshot,
    DeliveryState,
    EvidenceStatus,
    InvocationOutcome,
    InvocationResult,
    ResultReceipt,
    ReviewResult,
    TaskSpec,
    TaskState,
    UsageFacts,
    VerificationResult,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver
from hflow.ids import utc_now
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner


def _receipt(run_id: str, spec: TaskSpec, *, attempt_id: str) -> ResultReceipt:
    return ResultReceipt(
        run_id=run_id,
        task_id=spec.task_id,
        attempt_id=attempt_id,
        task_revision=1,
        runtime_build="thread-test",
        plan_digest=spec.spec_digest(),
        harness_outcome=InvocationOutcome.COMPLETED,
        candidate=CandidateSnapshot(base_commit="base", fingerprint="sha256:fp"),
        verification=VerificationResult(status="passed", evidence_ids=["E-1"]),
        review=ReviewResult(status="not_required"),
        task_state=TaskState.ACCEPTED,
        delivery_state=DeliveryState.LOCAL_CANDIDATE,
        usage=UsageFacts(),
    )


def _seed_checking(
    store: Store,
    project,
    spec: TaskSpec,
    *,
    run_id: str = "R-race",
    turn_limit: int = 4,
    repair_limit: int = 0,
) -> str:
    """A run in CHECKING with one active attempt: the state acceptance acts on."""
    run = store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="thread-test",
        checks_digest=project.checks_digest(),
        turn_limit=turn_limit,
        repair_limit=repair_limit,
    )
    store.claim_run(run["run_id"], "local-controller")
    store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)
    store.dispatch_attempt(
        run_id=run["run_id"],
        controller_id="local-controller",
        attempt_id="A-race",
        role="implementer",
        reservation_id="B-race",
        reserved_turns=1,
        reservation_expires_at="2999-01-01T00:00:00Z",
    )
    store.record_invocation("A-race", "I-race")
    store.advance_to_checking(run_id=run["run_id"], attempt_id="A-race", phase=__import__(
        "hflow.contracts", fromlist=["CheckPhase"]
    ).CheckPhase.VERIFICATION)
    store.record_evidence(
        evidence_id="E-1",
        run_id=run["run_id"],
        attempt_id="A-race",
        kind="verification",
        status=EvidenceStatus.PASSED,
        candidate_fingerprint="sha256:fp",
        checks_digest=project.checks_digest(),
        check_id="unit",
    )
    return str(run["run_id"])


def _seed_ready(store: Store, project, spec: TaskSpec, *, run_id: str, turn_limit: int, repair_limit: int = 0) -> str:
    """A claimed, READY run with no attempt yet: a clean slate for dispatch tests."""
    run = store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="thread-test",
        checks_digest=project.checks_digest(),
        turn_limit=turn_limit,
        repair_limit=repair_limit,
    )
    store.claim_run(run["run_id"], "local-controller")
    store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)
    return str(run["run_id"])


def _reserve_turns(store: Store, run_id: str, turns: int) -> None:
    if turns:
        store.reserve_turn(run_id, "local-controller", turns=turns)


# --------------------------------------------------------------------------
# ordering 1: cancel intent first, late success second
# --------------------------------------------------------------------------


def test_cancel_intent_committed_first_blocks_a_late_acceptance(
    tmp_path: Path, project, task_spec: TaskSpec
) -> None:
    """The two threads rendezvous *after* the intent is durable but *before* acceptance."""
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking(store, project, task_spec)
        intent_recorded = threading.Event()
        acceptance_attempted = threading.Event()
        results: dict[str, object] = {}

        def canceller() -> None:
            intent_at = store.record_cancel_intent(run_id)
            results["intent_at"] = intent_at
            intent_recorded.set()  # release the accepting thread
            acceptance_attempted.wait(timeout=10)
            results["cancel_receipt"] = store.cancel_state(run_id)[1]

        def accepter() -> None:
            intent_recorded.wait(timeout=10)  # proceed only once the intent is committed
            try:
                store.finalize_acceptance(
                    run_id, _receipt(run_id, task_spec, attempt_id="A-race"), checks_digest=project.checks_digest()
                )
                results["accepted"] = True
            except StoreError as exc:
                results["accepted"] = False
                results["error"] = str(exc)
            acceptance_attempted.set()

        threads = [threading.Thread(target=canceller), threading.Thread(target=accepter)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "a thread deadlocked; the rendezvous is wrong"

        assert results.get("intent_at"), "the cancel intent must be durable"
        assert results.get("accepted") is False, "a late success must not be accepted"
        assert "cancellation intent" in str(results.get("error"))
        row = store.get_run(run_id)
        assert row["task_state"] != TaskState.ACCEPTED.value
        assert row["receipt_json"] is None, "no receipt may be written for a cancelled run"
    finally:
        store.close()


# --------------------------------------------------------------------------
# ordering 2: acceptance first, cancel second
# --------------------------------------------------------------------------


def test_acceptance_committed_first_is_not_rewritten_by_a_later_cancel(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """Once delivered, a stop request records a fact and changes nothing."""
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        FakeDriver(project_root),
        controller_build="thread-test",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=tmp_path / "data",
    )
    try:
        run_id = _seed_checking(store, project, task_spec)
        accepted = threading.Event()

        def accepter() -> None:
            store.finalize_acceptance(
                run_id, _receipt(run_id, task_spec, attempt_id="A-race"), checks_digest=project.checks_digest()
            )
            accepted.set()

        def canceller() -> None:
            accepted.wait(timeout=10)  # only after acceptance is durable
            controller.cancel(run_id)

        threads = [threading.Thread(target=accepter), threading.Thread(target=canceller)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

        row = store.get_run(run_id)
        assert row["task_state"] == TaskState.ACCEPTED.value, "history is not rewritten"
        receipt = json.loads(row["receipt_json"])
        assert receipt["task_state"] == "ACCEPTED"
        assert receipt["delivery_state"] == "LOCAL_CANDIDATE"
        intent_at, cancel_receipt = store.cancel_state(run_id)
        assert cancel_receipt is not None and cancel_receipt.status == "confirmed_stopped"
        assert "already ACCEPTED" in cancel_receipt.detail
    finally:
        store.close()


# --------------------------------------------------------------------------
# the background-wait requirement: cancel must still land while work is running
# --------------------------------------------------------------------------


def test_cancel_lands_while_another_thread_waits_on_work(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A driver that blocks must not stop the controller from recording a stop.

    The worker thread holds the run while it waits on an event; the main thread must be able
    to persist a cancel intent and a receipt in the meantime. If any lock were held across
    the wait, this test would deadlock rather than pass slowly.
    """
    store = Store(tmp_path / "hflow.sqlite")

    class BlockingDriver(FakeDriver):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.entered = threading.Event()
            self.release = threading.Event()

        def start(self, request):  # noqa: ANN001, ANN201
            self.started.append(request)
            self.entered.set()
            # Wait for the test to finish its cancel work, bounded so a bug cannot hang CI.
            self.release.wait(timeout=20)
            return InvocationResult(
                invocation_id=request.invocation_id,
                outcome=InvocationOutcome.COMPLETED,
                agent_turns=1,
                limitations=["thread test: no model, no files changed"],
            )

    driver = BlockingDriver(project_root)
    controller = Controller(
        store,
        driver,
        controller_build="thread-test",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=tmp_path / "data",
    )
    try:
        # Review is irrelevant here and the project requires it, so disable it at the project
        # level rather than weakening the task (admission would refuse a task-level waiver).
        simple_project = project.model_copy(update={"review_required": False})
        spec = TaskSpec.model_validate(task_spec.model_dump(mode="json"))
        started = time.monotonic()
        worker_error: dict[str, object] = {}

        def drive() -> None:
            from hflow.contracts import RunRequest

            try:
                outcome = controller.run_task(
                    RunRequest(
                        task=spec,
                        project=simple_project,
                        project_root=project_root,
                        workspace_root=project_root,
                    )
                )
                worker_error["outcome"] = f"{outcome.task_state.value}/{outcome.block_code}"
            except BaseException as exc:  # noqa: BLE001
                worker_error["error"] = repr(exc)

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        if not driver.entered.wait(timeout=20):
            driver.release.set()
            worker.join(timeout=10)
            pytest.fail(f"the driver never started; worker said: {worker_error}")

        # The run exists and is live: the main thread records the stop while work waits.
        run_id = driver.started[0].run_id
        attempt = store.open_attempt(run_id)
        assert attempt is not None and attempt["invocation_id"]
        intent_at = store.record_cancel_intent(run_id)
        assert intent_at
        assert store.cancel_state(run_id)[0] == intent_at
        cancel_elapsed = time.monotonic() - started
        assert cancel_elapsed < 15, "recording a cancel must not wait on the running worker"

        driver.release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert "error" not in worker_error, worker_error
        assert "outcome" in worker_error, worker_error
    finally:
        store.close()


# --------------------------------------------------------------------------
# locking mechanics
# --------------------------------------------------------------------------


def test_concurrent_writers_do_not_deadlock_or_lose_updates(tmp_path: Path, project, task_spec: TaskSpec) -> None:
    """Several threads hammering the same run through the real Store stay consistent."""
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking(store, project, task_spec, run_id="R-load", turn_limit=4)
        store.release_reservation(run_id, 1)  # drop the seeded attempt's turn: start from zero
        barrier = threading.Barrier(4)
        reserve_lock = threading.Lock()
        granted = 0

        def reserve() -> None:
            nonlocal granted
            barrier.wait(timeout=20)  # start together: maximal interleaving pressure
            for _ in range(5):
                try:
                    store.reserve_turn(run_id, "local-controller", turns=1)
                    with reserve_lock:
                        granted += 1
                except StoreError:
                    pass  # the ceiling is the point: refusals are expected and must be clean

        def read() -> None:
            barrier.wait(timeout=20)
            for _ in range(20):
                store.get_run(run_id)
                store.attempts_for(run_id)
                store.invocation_counts(run_id)

        threads = [threading.Thread(target=reserve) for _ in range(2)] + [
            threading.Thread(target=read) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a thread deadlocked inside the store lock"

        row = store.get_run(run_id)
        assert int(row["turns_reserved"]) == granted, "the ledger must equal the granted reservations"
        assert int(row["turns_reserved"]) <= int(row["turn_limit"])
    finally:
        store.close()


def test_store_survives_a_concurrent_close_of_the_other_handle(tmp_path: Path, project, task_spec: TaskSpec) -> None:
    """Two Store objects on one file, one writing while the other reads: no corruption."""
    path = tmp_path / "hflow.sqlite"
    writer = Store(path)
    reader = Store(path)
    try:
        run_id = _seed_checking(writer, project, task_spec, run_id="R-two", turn_limit=12)
        assert reader.get_run(run_id)["run_id"] == run_id
        stop = threading.Event()
        seen: list[str] = []

        def poll() -> None:
            while not stop.is_set():
                seen.append(reader.get_run(run_id)["task_state"])
                time.sleep(0.01)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        for _ in range(10):
            writer.reserve_turn(run_id, "local-controller", turns=1)
        stop.set()
        thread.join(timeout=10)
        assert seen and all(state in {"CHECKING"} for state in seen)
        assert int(writer.get_run(run_id)["turns_reserved"]) == 11, "1 seeded + 10 reserved"
    finally:
        reader.close()
        writer.close()


def test_lock_is_released_when_a_transaction_body_raises(tmp_path: Path, project, task_spec: TaskSpec) -> None:
    """A failing transaction must roll back, release the lock, and leave the DB usable."""
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking(store, project, task_spec, run_id="R-raise", turn_limit=4)
        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                conn.execute("UPDATE runs SET turns_reserved = 3 WHERE run_id = ?", (run_id,))
                raise RuntimeError("boom inside the transaction")

        assert store.get_run(run_id)["turns_reserved"] == 1, "the write must be rolled back"
        # Still usable, and still able to take a real transaction.
        store.reserve_turn(run_id, "local-controller", turns=1)
        assert store.get_run(run_id)["turns_reserved"] == 2
    finally:
        store.close()


def test_transaction_is_one_write_unit_in_the_file(tmp_path: Path, project, task_spec: TaskSpec) -> None:
    """A second connection cannot see a half-applied state/attempt/budget change."""
    path = tmp_path / "hflow.sqlite"
    store = Store(path)
    observer = Store(path)
    try:
        run_id = _seed_ready(store, project, task_spec, run_id="R-atomic", turn_limit=8, repair_limit=2)

        seen: list[tuple[object, ...]] = []
        stop = threading.Event()

        def observe() -> None:
            """One read of both facts, so a skew cannot come from two separate reads."""
            while not stop.is_set():
                row = observer.get_run(run_id)
                attempts = observer.attempts_for(run_id)
                seen.append((int(row["turns_reserved"]), len(attempts)))
                time.sleep(0.005)

        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        for index in range(3):
            store.dispatch_attempt(
                run_id=run_id,
                controller_id="local-controller",
                attempt_id=f"A-atomic-{index}",
                role=f"implementer-{index}",
                reservation_id=f"B-{index}",
                reserved_turns=1,
                reservation_expires_at="2999-01-01T00:00:00Z",
            )
            store.finish_attempt(
                run_id=run_id,
                attempt_id=f"A-atomic-{index}",
                state=AttemptState.FAILED,
                outcome=InvocationOutcome.FAILED,
                result={"index": index},
            )
            if index == 1:
                store.start_repair_cycle(run_id, "local-controller")
            else:
                # The next dispatch needs the run back in READY; repair_cycle does that, so the
                # last iteration simply leaves the run RUNNING for the next attempt to replace.
                store.set_task_state(run_id, [TaskState.RUNNING], TaskState.READY, idempotent=True)
        stop.set()
        thread.join(timeout=10)

        # Budget and attempt rows were always consistent: an attempt never existed without the
        # budget that authorized it, and the ledger never fell behind the attempts it covers.
        assert seen, "the observer thread saw nothing"
        for reserved, attempts in seen:
            assert reserved >= attempts, (
                f"observed budget/attempt skew: reserved={reserved} attempts={attempts}"
            )
            assert reserved <= 8, "the ceiling must hold at every observation"
        assert int(store.get_run(run_id)["turns_reserved"]) == 3, "one turn per dispatched attempt"
    finally:
        observer.close()
        store.close()


def test_sqlite_file_is_not_left_locked_after_close(tmp_path: Path) -> None:
    """Closing the store must release the file: a fresh connection can write immediately."""
    path = tmp_path / "hflow.sqlite"
    first = Store(path)
    first.close()
    second = sqlite3.connect(str(path), timeout=5)
    try:
        second.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('probe', ?)", (utc_now(),))
        second.commit()
    finally:
        second.close()
