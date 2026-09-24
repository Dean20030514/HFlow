"""Cancelling the *active* role's invocation, with the reviewer mid-flight.

The README used to say that ``cancel`` targets the attempt's implementer invocation, so a stop
arriving while the review ran was not routed to the reviewer's own invocation. These tests pin
that behaviour down offline:

* the stop reaches the invocation that is actually running, by its own invocation id, through
  the driver that owns it - whether both roles share one driver object or have their own;
* a stop that a driver cannot confirm stays ``unknown`` and blocks as ``outcome_unknown``;
* a recorded intent is never overwritten by a late ``accepted`` verdict;
* nothing is re-dispatched and no turn is reserved after the intent is recorded;
* reconciliation follows the same routing (the reviewer's driver, not the implementer's).

The role drivers here are ``FakeDriver`` subclasses with a gate: they block inside ``start``
until the test releases them, which is what makes "a stop arrives while the role is running"
deterministic instead of a sleep race. No model is contacted.

The last group covers the *windows inside the review handoff*: a stop recorded while the
reviewer packet is being rendered, while the review turn is being reserved, and while the
verification step is still running. Those were found by an external counterexample after the
first version of this fix checked the intent only once, at the top of ``_review``. They are
reproduced here through the real ``run_task`` path rather than by seeding a state, because the
defect was precisely that the loop kept going after the stop.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import hflow.controller as controller_module
import hflow.drivers.acpx_dsh as acpx_dsh_module
from hflow.contracts import (
    AttemptState,
    CancellationReceipt,
    CheckPhase,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    RefusalCode,
    ReviewOutput,
    RunRequest,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from .test_driver_acpx_dsh import FAKE_CLIENT, STUB_AGENT


class RaceSeam:
    """Freezes a store operation, so a test decides the interleaving instead of guessing it.

    ``install`` wraps one of the store's own atomic operations. The wrapper raises ``entered``,
    waits for ``proceed``, then performs the *real* call - so the concurrent cancel happens at a
    chosen point relative to a write that is itself one statement. That is the shape being
    tested: a stop that lands the instant before the conditional write decides who wins.
    """

    def __init__(self, store: Store, method: str) -> None:
        self.store = store
        self.method = method
        self.original = getattr(store, method)
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def install(self, monkeypatch) -> RaceSeam:  # noqa: ANN001 - pytest's MonkeyPatch
        def gated(*args, **kwargs):  # noqa: ANN002, ANN003 - the wrapped method's shape
            self.entered.set()
            assert self.proceed.wait(timeout=30), "the race seam was never released"
            return self.original(*args, **kwargs)

        monkeypatch.setattr(self.store, self.method, gated)
        return self


class GateDriver(FakeDriver):
    """A fake role driver that can be held inside ``start`` and reports its own stops.

    ``cancel`` records what the driver was actually told, so a test can assert *which*
    invocation was stopped rather than only that something was.
    """

    def __init__(self, project_root: Path, *, label: str) -> None:
        super().__init__(
            project_root, FakeScript(review=ReviewOutput(verdict="accepted", findings=[]))
        )
        self.label = label
        #: Set when this driver is inside ``start``; the test waits on it, never on a sleep.
        self.entered = threading.Event()
        #: When true, ``start`` blocks for each role in ``gated_roles``.
        self.gate = False
        #: Roles whose ``start`` blocks, when the gate is on.
        self.gated_roles = {"implementer", "reviewer"}
        #: Roles the test has let through; empty means every gated role waits.
        self.released_roles: set[str] = set()
        #: Open when the currently blocked role is released; re-closed for the next role.
        self.gate_open = threading.Event()
        #: Open only while an invocation is blocked inside the gate.
        self.blocked = threading.Event()
        #: ``(invocation_id, label)`` per cancel call, in order.
        self.cancel_calls: list[tuple[str, str]] = []
        #: A driver that cannot confirm the stop reports ``unknown`` instead.
        self.cancel_status = "confirmed_stopped"
        #: Reconcile calls, in order.
        self.reconcile_calls: list[str] = []

    def release_role(self, role: str) -> None:
        """Let the invocation of ``role`` past the gate (and only that one)."""
        self.released_roles.add(role)
        self.gate_open.set()
        self.blocked.wait(timeout=30)
        self.gate_open.clear()

    def release_all(self) -> None:
        """Open the gate for every role, so no invocation is left blocked."""
        self.gate_open.set()

    def start(self, request: InvocationRequest) -> InvocationResult:
        self.started.append(request)
        if (
            self.gate
            and request.role in self.gated_roles
            and request.role not in self.released_roles
        ):
            self.entered.set()
            self.blocked.set()
            self.gate_open.wait(timeout=30)
            self.blocked.clear()
        if request.role == "reviewer":
            # The reviewer's answer, not a file change: the verdict is the whole product.
            return InvocationResult(
                invocation_id=request.invocation_id,
                outcome=InvocationOutcome.COMPLETED,
                review=self.script.review,
                agent_turns=1,
                limitations=["gate driver: no model was invoked"],
            )
        return super().start(request)

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancel_calls.append((invocation_id, self.label))
        stopped = self.cancel_status == "confirmed_stopped"
        return CancellationReceipt(
            invocation_id=invocation_id,
            status=self.cancel_status,  # type: ignore[arg-type]
            mechanism="forced" if stopped else "none",
            local_process_stopped=stopped,
            detail=f"{self.label} driver: cancel asked for {invocation_id}",
        )

    def reconcile(self, invocation_id: str):  # noqa: ANN201 - the fake driver's shape
        self.reconcile_calls.append(invocation_id)
        return super().reconcile(invocation_id)


class RunningRun:
    """One dispatched run driven in a background thread, held at a chosen role."""

    def __init__(self, controller: Controller, request: RunRequest) -> None:
        self.controller = controller
        self.request = request
        self.outcome = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._drive, daemon=True)

    def _drive(self) -> None:
        try:
            self.outcome = self.controller.run_task(self.request)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            self.error = exc

    def __enter__(self) -> RunningRun:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for driver in self.drivers():
            driver.release_all()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "the run thread is stuck; the gate was never released"

    def drivers(self) -> list[GateDriver]:
        found = [self.controller.driver, self.controller.reviewer_driver]
        return [driver for driver in found if isinstance(driver, GateDriver)]

    def wait_until(
        self, predicate, timeout: float = 30.0, describe=None
    ) -> None:
        """Wait for an observable fact, never for a fixed sleep."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        detail = f" (last state: {describe()})" if describe is not None else ""
        raise AssertionError(f"the expected fact never became observable{detail}")

    def wait_for_role(
        self, driver: GateDriver, role: str, timeout: float = 30.0
    ) -> InvocationRequest:
        """The recorded request for one role, once that request has become observable."""
        self.wait_until(
            lambda: any(request.role == role for request in driver.started), timeout=timeout
        )
        for request in reversed(driver.started):
            if request.role == role:
                return request
        raise AssertionError(f"no {role} invocation on the {driver.label} driver")

    def run_id(self) -> str:
        return str(self.controller.store.list_runs()[0]["run_id"])


def _controller(
    store: Store, implementer: GateDriver, reviewer: GateDriver, data_dir: Path
) -> Controller:
    """A controller with an explicit implementer/reviewer pair, offline and deterministic."""
    runner = FakeCheckRunner()
    return Controller(
        store,
        implementer,
        reviewer_driver=reviewer,
        controller_build="cancel-routing-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=data_dir,
        production=False,
    )


def _pair(project_root: Path) -> tuple[GateDriver, GateDriver]:
    return (
        GateDriver(project_root, label="implementer"),
        GateDriver(project_root, label="reviewer"),
    )


def _assert_no_redispatch(store: Store, run_id: str, *, before: tuple[int, int, int]) -> None:
    """No new invocation, no new reserved turn, no new allowance after the stop."""
    after = (
        *store.invocation_counts(run_id),
        int(store.get_run(run_id)["turns_reserved"]),
    )
    assert after == before, f"a stop changed the dispatch accounting: {before} -> {after}"


def _seed_implementer_run(
    store: Store,
    project,
    task_spec,
    *,
    run_id: str,
    attempt_id: str,
    invocation_id: str,
    phase: CheckPhase | None = None,
    block_code: RefusalCode | None = None,
) -> None:
    """A claimed run whose implementer attempt exists, optionally mid-check or blocked.

    Seeds the rows a controller would have written, so a coordination rule can be exercised at
    a point of the loop that is otherwise hard to reach deterministically.
    """
    store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=task_spec,
        spec_digest=task_spec.spec_digest(),
        controller_build="cancel-routing-test",
        checks_digest=project.checks_digest(),
        turn_limit=4,
        repair_limit=0,
    )
    store.claim_run(run_id, "local-controller")
    store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)
    store.dispatch_attempt(
        run_id=run_id,
        controller_id="local-controller",
        attempt_id=attempt_id,
        role="implementer",
        reservation_id=f"B-{attempt_id}",
        reserved_turns=1,
        reservation_expires_at="2999-01-01T00:00:00Z",
    )
    store.record_invocation(attempt_id, invocation_id)
    if phase is not None:
        store.advance_to_checking(run_id=run_id, attempt_id=attempt_id, phase=phase)
    if block_code is not None:
        store.set_blocked(run_id, block_code, "seeded for a coordination test")


# --------------------------------------------------------------------------
# routing: the stop names the invocation that is running
# --------------------------------------------------------------------------


def test_cancel_during_review_reaches_the_reviewer_invocation(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """The stop must name the reviewer's invocation - not the implementer's, which already ended."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        reviewer_request = running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        assert store.get_run(run_id)["phase"] == "review"
        accounted = (*store.invocation_counts(run_id), int(store.get_run(run_id)["turns_reserved"]))

        receipt = controller.cancel(run_id)
        _assert_no_redispatch(store, run_id, before=accounted)

    assert receipt.invocation_id == reviewer_request.invocation_id, (
        "the stop must target the invocation that is running"
    )
    assert reviewer.cancel_calls == [(reviewer_request.invocation_id, "reviewer")]
    assert implementer.cancel_calls == [], "a finished implementer invocation is not the target"

    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert row["receipt_json"] is None, "a stop never produces a delivery receipt"
    assert running.outcome is not None and running.outcome.receipt is None


def test_a_separately_bound_reviewer_is_stopped_through_its_own_driver(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """Two role bindings are two drivers: the stop must go to the one that owns the process."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        reviewer_request = running.wait_for_role(reviewer, "reviewer")
        receipt = controller.cancel(running.run_id())

    assert receipt.status == "confirmed_stopped"
    assert receipt.invocation_id == reviewer_request.invocation_id
    assert reviewer.cancel_calls == [(reviewer_request.invocation_id, "reviewer")]
    assert implementer.cancel_calls == [], "the implementer's driver owns no live reviewer process"


def test_cancel_during_review_on_one_shared_driver_targets_the_reviewer_id(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """One driver object for both roles is still two invocations: the stop names the live one."""
    shared = GateDriver(project_root, label="shared")
    shared.gate = True
    # The implementer runs first and is released as soon as it is observable; the reviewer is
    # then the role the gate holds, so the stop lands on a *running review* by construction.
    controller = _controller(store, shared, shared, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        implementer_request = running.wait_for_role(shared, "implementer")
        shared.release_role("implementer")
        reviewer_request = running.wait_for_role(shared, "reviewer")
        run_id = running.run_id()
        assert store.get_run(run_id)["phase"] == "review"
        assert reviewer_request.invocation_id != implementer_request.invocation_id

        receipt = controller.cancel(run_id)

    assert receipt.invocation_id == reviewer_request.invocation_id
    assert shared.cancel_calls == [(reviewer_request.invocation_id, "shared")]


def test_cancel_before_the_reviewer_starts_targets_the_implementer(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """Routing adds a case; it does not move the stop that already worked."""
    implementer, reviewer = _pair(project_root)
    implementer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        implementer_request = running.wait_for_role(implementer, "implementer")
        receipt = controller.cancel(running.run_id())

    assert receipt.invocation_id == implementer_request.invocation_id
    assert implementer.cancel_calls == [(implementer_request.invocation_id, "implementer")]
    assert reviewer.started == [], "the reviewer must never be started after a cancellation"


# --------------------------------------------------------------------------
# an unconfirmed stop stays unconfirmed
# --------------------------------------------------------------------------


def test_an_unconfirmed_reviewer_stop_blocks_and_is_not_reported_as_success(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """A driver that cannot confirm the stop yields ``unknown`` and an ``outcome_unknown`` block."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    reviewer.cancel_status = "unknown"
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        receipt = controller.cancel(run_id)

    assert receipt.status == "unknown"
    assert receipt.local_process_stopped is False
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value
    assert row["receipt_json"] is None
    assert row["cancel_intent_at"], "the intent stays recorded whatever the stop reported"


def test_a_cancelled_reason_for_the_workspace_is_preserved_by_an_unconfirmed_stop(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """The workspace must stay cleanable-blocked: an unconfirmed stop is not a stop."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    reviewer.cancel_status = "still_running"
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        running.wait_for_role(reviewer, "reviewer")
        receipt = controller.cancel(running.run_id())

    assert receipt.status == "still_running"
    assert receipt.local_process_stopped is False
    assert "reviewer driver: cancel asked for" in receipt.detail


# --------------------------------------------------------------------------
# a late acceptance never overrides the intent, and nothing is re-dispatched
# --------------------------------------------------------------------------


def test_a_late_accepted_verdict_does_not_override_a_recorded_stop(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """The reviewer answers ``accepted`` after the stop: the run stays blocked, with no receipt."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        reviewer_request = running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        receipt = controller.cancel(run_id)
        # Only now does the reviewer finish - with an accepting verdict.
        reviewer.release_role("reviewer")
        running.wait_until(lambda: running.outcome is not None)

    assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
    assert receipt.invocation_id == reviewer_request.invocation_id
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value, "a late acceptance must not be applied"
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert row["receipt_json"] is None
    assert running.outcome is not None and running.outcome.receipt is None
    assert running.outcome.task_state is TaskState.BLOCKED


def test_a_recorded_stop_prevents_the_review_invocation_from_being_bought(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """A stop recorded before the reviewer starts must not buy a review turn afterwards."""
    implementer, reviewer = _pair(project_root)
    implementer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        implementer_request = running.wait_for_role(implementer, "implementer")
        run_id = running.run_id()
        controller.cancel(run_id)
        implementer.release_role("implementer")
        running.wait_until(
            lambda: running.outcome is not None,
            describe=lambda: (
                store.get_run(run_id)["task_state"],
                store.get_run(run_id)["block_code"],
                store.invocation_counts(run_id),
            ),
        )

    assert implementer.cancel_calls == [(implementer_request.invocation_id, "implementer")]
    assert reviewer.started == [], "no review may be dispatched after a recorded stop"
    assert store.invocation_counts(run_id) == (1, 0)
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value


# --------------------------------------------------------------------------
# reconciliation follows the same routing
# --------------------------------------------------------------------------


def test_resume_reconciles_the_active_roles_invocation(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """An interrupted review is reconciled on the reviewer's driver, and never re-dispatched."""
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    _seed_implementer_run(
        store,
        project,
        task_spec,
        run_id="R-reconcile-review",
        attempt_id="A-review",
        invocation_id="I-implementer",
    )
    store.finish_attempt(
        run_id="R-reconcile-review",
        attempt_id="A-review",
        state=AttemptState.SUCCEEDED,
        outcome=InvocationOutcome.COMPLETED,
        result={"invocation_id": "I-implementer"},
    )
    store.advance_to_checking(
        run_id="R-reconcile-review", attempt_id="A-review", phase=CheckPhase.REVIEW
    )
    store.record_review_invocation("A-review", "I-reviewer")
    store.set_blocked(
        "R-reconcile-review",
        RefusalCode.OUTCOME_UNKNOWN,
        "controller was interrupted during review",
    )
    run_id = "R-reconcile-review"

    outcome = controller.resume(run_id)

    assert reviewer.reconcile_calls == ["I-reviewer"], "the reviewer's driver owns that invocation"
    assert implementer.reconcile_calls == [], "the implementer's driver never ran that invocation"
    assert reviewer.started == [] and implementer.started == []
    assert outcome.block_code == RefusalCode.OUTCOME_UNKNOWN
    assert store.get_run(run_id)["task_state"] == TaskState.BLOCKED.value, (
        "resume never re-dispatches"
    )


def test_resume_reconciles_the_implementer_when_no_review_was_dispatched(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """Routing is by recorded fact, not by role: an interrupted implementation stays its own."""
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    _seed_implementer_run(
        store,
        project,
        task_spec,
        run_id="R-reconcile-implementer",
        attempt_id="A-impl",
        invocation_id="I-implementer",
        block_code=RefusalCode.OUTCOME_UNKNOWN,
    )

    controller.resume("R-reconcile-implementer")

    assert implementer.reconcile_calls == ["I-implementer"]
    assert reviewer.reconcile_calls == []
    assert implementer.started == [] and reviewer.started == []


def test_reconcile_reports_not_started_instead_of_asking_the_wrong_driver(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """A run with no recorded invocation has nothing to observe - not a reason to call a driver."""
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    store.create_run(
        run_id="R-reconcile-none",
        project_id=project.project_id,
        spec=task_spec,
        spec_digest=task_spec.spec_digest(),
        controller_build="cancel-routing-test",
        checks_digest=project.checks_digest(),
        turn_limit=4,
        repair_limit=0,
    )
    assert controller.reconcile("R-reconcile-none").value == "not_started"
    assert implementer.reconcile_calls == [] and reviewer.reconcile_calls == []


# --------------------------------------------------------------------------
# the stop is an auditable fact, not only a boolean
# --------------------------------------------------------------------------


def test_the_recorded_stop_names_the_role_driver_and_invocation(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """An operator reading the run afterwards must see which process was asked to stop."""
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        reviewer_request = running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        controller.cancel(run_id)

    notes = [note for note in store.notes_for(run_id) if note.startswith("cancel_target")]
    assert len(notes) == 1, notes
    assert "role=reviewer" in notes[0]
    assert f"invocation={reviewer_request.invocation_id}" in notes[0]
    assert "reported=confirmed_stopped" in notes[0]


def test_a_late_verdict_cannot_erase_the_record_of_what_was_stopped(
    store: Store, project, task_spec, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """The stop's own record survives the late answer that follows it.

    ``set_blocked`` clears the block reason on a terminal transition, so the confirmed stop is
    read back from the note table rather than from whatever the last writer put in the column.
    """
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        reviewer_request = running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        controller.cancel(run_id)
        reviewer.release_role("reviewer")
        running.wait_until(lambda: running.outcome is not None)

    notes = [note for note in store.notes_for(run_id) if note.startswith("cancel_target")]
    assert len(notes) == 1, notes
    assert "role=reviewer" in notes[0]
    assert f"invocation={reviewer_request.invocation_id}" in notes[0]
    assert "reported=confirmed_stopped" in notes[0]
    row = store.get_run(run_id)
    assert row["receipt_json"] is None
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value


def test_a_stop_during_verification_blocks_without_buying_the_review(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """A stop recorded while the checks run must not be followed by a review turn."""
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    _seed_implementer_run(
        store,
        project,
        task_spec,
        run_id="R-stop-at-verification",
        attempt_id="A-impl",
        invocation_id="I-implementer",
        phase=CheckPhase.VERIFICATION,
    )

    receipt = controller.cancel("R-stop-at-verification")

    assert receipt.status == "confirmed_stopped"
    assert implementer.cancel_calls == [("I-implementer", "implementer")]
    assert reviewer.started == [], "a recorded stop must not be followed by a review invocation"
    assert store.invocation_counts("R-stop-at-verification") == (1, 0)
    row = store.get_run("R-stop-at-verification")
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value


# --------------------------------------------------------------------------
# the windows inside the review handoff (external counterexample, kept here)
# --------------------------------------------------------------------------


def test_a_stop_during_verification_is_carried_through_the_real_check_path(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The whole loop after a stop *inside* ``verify_candidate``, not a seeded phase.

    The state transition into review asks for ``CHECKING`` while the run is already ``BLOCKED``,
    which used to raise ``StoreError`` out of the run thread. A stop is not a state-machine
    error, and the implementer's checks having finished does not restart anything.
    """
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    original = controller_module.verify_candidate

    def verify_then_stop(**kwargs):
        verification = original(**kwargs)
        controller.cancel(kwargs["run_id"])
        return verification

    monkeypatch.setattr(controller_module, "verify_candidate", verify_then_stop)
    outcome = controller.run_task(run_request)

    assert outcome.receipt is None
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR
    assert reviewer.started == [], "no review may be dispatched after a stop"
    assert store.invocation_counts(outcome.run_id) == (1, 0)


def test_a_stop_while_the_reviewer_packet_is_rendered_cancels_the_handoff(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """A stop recorded during packet rendering must be read *after* the rendering.

    The earliest check, at the top of ``_review``, cannot see a stop that arrives while the
    packet is being built - so nothing may be bought after the fact.
    """
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    original = controller_module.render_reviewer_packet

    def cancel_then_render(**kwargs):
        controller.cancel(store.list_runs()[0]["run_id"])
        return original(**kwargs)

    monkeypatch.setattr(controller_module, "render_reviewer_packet", cancel_then_render)
    outcome = controller.run_task(run_request)

    row = store.get_run(outcome.run_id)
    assert len(reviewer.started) == 0, "the reviewer must not be started after the stop"
    assert row["turns_reserved"] == 1, "no review turn may be reserved after the stop"
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert row["receipt_json"] is None


def test_a_stop_after_the_review_turn_is_reserved_still_prevents_the_start(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The tightest window: the turn is reserved, the stop lands, the process must not start.

    The review turn is already spent when this stop is recorded - a reservation is not refunded,
    and it never will be - so the only thing left to get right is that the reviewer process is
    not launched for a run a human already stopped.
    """
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    original = store.reserve_review_turn

    def reserve_then_stop(run_id: str, controller_id: str) -> None:
        original(run_id, controller_id)
        controller.cancel(run_id)

    monkeypatch.setattr(store, "reserve_review_turn", reserve_then_stop)
    outcome = controller.run_task(run_request)

    row = store.get_run(outcome.run_id)
    assert len(reviewer.started) == 0, "no reviewer process may start after a confirmed stop"
    assert row["turns_reserved"] == 2, "the reserved review turn is spent, never refunded"
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert row["receipt_json"] is None


def test_a_late_failed_review_does_not_relabel_an_unconfirmed_stop(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """An unconfirmed stop stays ``outcome_unknown`` even when the reviewer then fails.

    A late failure is a fact about an invocation the stop already ended. Letting it write
    ``review_protocol_error`` would hide that the work may still be running.
    """
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    reviewer.cancel_status = "unknown"
    original_start = reviewer.start

    def start_then_fail(request: InvocationRequest) -> InvocationResult:
        original_start(request)
        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=InvocationOutcome.FAILED,
            error_message="the reviewer transport failed after the stop",
        )

    reviewer.start = start_then_fail  # type: ignore[method-assign]
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        receipt = controller.cancel(run_id)
        assert receipt.status == "unknown"

        # Only now does the reviewer fail - after the stop, and after the intent is durable.
        reviewer.release_role("reviewer")
        running.wait_until(lambda: running.outcome is not None)

    assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, (
        "a late failure must not overwrite an unconfirmed stop"
    )
    assert row["receipt_json"] is None
    assert store.get_run(run_id)["cancel_intent_at"], "the intent stays recorded"


def test_a_late_failed_review_does_not_relabel_a_confirmed_stop(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """The confirmed counterpart: ``review_protocol_error`` must not replace the stop either.

    Both stop codes are pinned this way, because both are read by an operator to decide whether
    a process might still be running - one value per stop outcome, whatever fails afterwards.
    """
    implementer, reviewer = _pair(project_root)
    reviewer.gate = True
    original_start = reviewer.start

    def start_then_fail(request: InvocationRequest) -> InvocationResult:
        original_start(request)
        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=InvocationOutcome.FAILED,
            error_message="the reviewer transport failed after the stop",
        )

    reviewer.start = start_then_fail  # type: ignore[method-assign]
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    with RunningRun(controller, run_request) as running:
        running.wait_for_role(reviewer, "reviewer")
        run_id = running.run_id()
        receipt = controller.cancel(run_id)
        assert receipt.status == "confirmed_stopped"

        reviewer.release_role("reviewer")
        running.wait_until(lambda: running.outcome is not None)

    assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
    row = store.get_run(run_id)
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value, (
        "a late failure must not overwrite a confirmed stop"
    )
    assert row["receipt_json"] is None


# --------------------------------------------------------------------------
# the same windows again, decided by thread interleaving rather than by ordering
#
# The tests above pin the *orders* a stop can arrive in. These pin the windows where a stop
# arrives between a decision and the write that acts on it - a check-then-write cannot close
# them, so the write itself is conditional on the stop fact. The seam is a store method, so
# these fail if the controller goes back to an unconditional call.
# --------------------------------------------------------------------------


def test_a_stop_racing_the_reviewer_registration_refuses_the_handoff(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The stop commits the instant before registration: the reviewer must still not start.

    This is the case an extra read cannot fix. The registration carries
    ``WHERE cancel_intent_at IS NULL``, so the stop wins the row and no invocation id exists to
    start a process for.
    """
    implementer, reviewer = _pair(project_root)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    seam = RaceSeam(store, "register_attempt_invocation_unless_stopped").install(monkeypatch)

    with RunningRun(controller, run_request) as running:
        assert seam.entered.wait(timeout=30), "the reviewer handoff was never reached"
        run_id = running.run_id()
        receipt = controller.cancel(run_id)
        seam.proceed.set()

    assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
    assert receipt.status == "confirmed_stopped"
    attempt = store.open_attempt(run_id)
    assert attempt is not None and not attempt["review_invocation_id"], (
        "no reviewer invocation may be registered once a stop won the handoff"
    )
    assert reviewer.started == [], "the reviewer process must not be started"
    assert store.invocation_counts(run_id) == (1, 0)
    assert running.outcome is not None
    assert running.outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR


def test_a_stop_racing_a_failure_block_keeps_the_stop_state_atomic(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The ordinary failure write is conditional too, so an interleaved stop cannot be replaced.

    The failure path has decided to block with ``review_protocol_error``; the stop commits
    before that write reaches the database. One statement decides it, and the stop's state is
    what a reader gets.
    """
    implementer, reviewer = _pair(project_root)
    reviewer.cancel_status = "unknown"
    original_start = reviewer.start

    def failing_review(request: InvocationRequest) -> InvocationResult:
        original_start(request)
        return InvocationResult(
            invocation_id=request.invocation_id, outcome=InvocationOutcome.FAILED
        )

    reviewer.start = failing_review  # type: ignore[method-assign]
    controller = _controller(store, implementer, reviewer, tmp_path / "data")
    seam = RaceSeam(store, "block_unless_stopped").install(monkeypatch)

    with RunningRun(controller, run_request) as running:
        assert seam.entered.wait(timeout=30), "the failure block was never reached"
        run_id = running.run_id()
        receipt = controller.cancel(run_id)
        assert receipt.status == "unknown"
        seam.proceed.set()

    assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
    row = store.get_run(run_id)
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, (
        "the ordinary failure block must not overwrite an interleaved stop"
    )
    assert row["receipt_json"] is None
    assert running.outcome is not None
    assert running.outcome.block_code is RefusalCode.OUTCOME_UNKNOWN


def test_a_stop_after_registration_but_before_the_driver_enters_start(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path
) -> None:
    """Two Windows later: a stop must not be *reported* as a stop while a child is created.

    This test used to end at the run's block code, and that was the mistake: the run read
    ``outcome_unknown`` while the driver went on to create a real reviewer process. The
    assertions below are about the process boundary, because "no work continued" is the claim
    being made - a state column cannot make it. The driver is the production
    ``AcpxDshDriver`` over the offline client stand-in and a stub agent, so a created child is a
    real process and its stub marks the moment it ran. No model is contacted.
    """
    implementer, _ = _pair(project_root)
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "cooperative"],
    )
    scratch = tmp_path / "stub-scratch"
    scratch.mkdir()
    reviewer.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    entered, proceed = threading.Event(), threading.Event()
    original_start = reviewer.start
    launches: list[tuple[int | None, bool]] = []
    original_launch = reviewer.start_handle

    def observe_launch(request: InvocationRequest):  # noqa: ANN201 - the driver's own handle
        handle = original_launch(request)
        launches.append((handle.pid, handle.start_cancelled))
        return handle

    def pause_before_start(request: InvocationRequest) -> InvocationResult:
        entered.set()
        assert proceed.wait(timeout=30), "the pre-start gate was never released"
        return original_start(request)

    reviewer.start = pause_before_start  # type: ignore[method-assign]
    reviewer.start_handle = observe_launch  # type: ignore[method-assign]
    try:
        with RunningRun(controller, run_request) as running:
            assert entered.wait(timeout=30), "the reviewer handoff was never reached"
            run_id = running.run_id()
            attempt = store.open_attempt(run_id)
            assert attempt is not None and attempt["review_invocation_id"], (
                "the registration is the window being tested: it must already be committed"
            )
            receipt = controller.cancel(run_id)
            assert receipt.status == "unknown", "no process existed yet, so nothing was confirmed"
            proceed.set()

        assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
        real_children = [pid for pid, _cancelled in launches if pid is not None]
        assert real_children == [], (
            f"a reviewer process was created after the stop: pids={real_children}"
        )
        assert launches and all(cancelled for _pid, cancelled in launches), (
            "the driver must report the invocation as stopped before its process was created"
        )
        assert list(scratch.glob("stub-*.spawn")) == [], "the agent stub must never have run"
        assert store.get_run(run_id)["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value
        assert store.get_run(run_id)["receipt_json"] is None
    finally:
        proceed.set()
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)


def test_a_stop_that_loses_the_race_lands_on_the_published_child(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The other order: the child exists first, and the stop must reach that child.

    The seam is the spawn itself: the test lets the driver create the process (inside its own
    gate) and only then asks to stop, so the stop takes the gate *after* the publication. It
    must find the published handle and terminate the process - ``mechanism=forced`` - rather
    than concluding that nothing exists. What is asserted is the process boundary: the stub
    client and its child are gone.
    """
    implementer, _ = _pair(project_root)
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "stubborn"],
    )
    scratch = tmp_path / "stub-scratch"
    scratch.mkdir()
    reviewer.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    spawned = threading.Event()
    original_popen = acpx_dsh_module.popen_in_boundary
    pids: list[int] = []

    def popen_then_signal(*args, **kwargs):  # noqa: ANN002, ANN003
        child = original_popen(*args, **kwargs)
        pids.append(child.pid)
        spawned.set()
        return child

    monkeypatch.setattr(acpx_dsh_module, "popen_in_boundary", popen_then_signal)
    try:
        with RunningRun(controller, run_request) as running:
            assert spawned.wait(timeout=30), "the reviewer process was never created"
            run_id = running.run_id()
            receipt = controller.cancel(run_id)

        assert pids and pids[0] is not None
        assert receipt.invocation_id, "the stop must name the reviewer invocation"
        assert receipt.status == "confirmed_stopped"
        assert receipt.mechanism == "forced", "a live child is stopped by the boundary, not by a claim"
        assert receipt.local_process_stopped is True
        assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
        assert store.get_run(run_id)["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    finally:
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)


def test_a_stop_inside_the_spawn_gate_waits_for_publication_and_stops_the_child(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch
) -> None:
    """The critical section itself: the stop is requested while the child is being created.

    The stop decision has already passed and the spawn gate is held, but no handle is published
    yet. A stop that reads "no handle" here and returns is how a child gets created after the
    controller recorded that the run was stopped. ``cancel`` must therefore record the request
    under the gate and **wait** for the spawn to publish, then stop the process it finds.

    The assertions are about the boundary: the stop does not return early, and the child that was
    created while it waited is gone.
    """
    implementer, _ = _pair(project_root)
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "stubborn"],
    )
    scratch = tmp_path / "stub-scratch"
    scratch.mkdir()
    reviewer.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    controller = _controller(store, implementer, reviewer, tmp_path / "data")

    entered, proceed, cancel_returned = threading.Event(), threading.Event(), threading.Event()
    original_popen = acpx_dsh_module.popen_in_boundary
    children: list[int] = []
    receipts: list[CancellationReceipt] = []
    errors: list[BaseException] = []

    def pause_inside_the_gate(*args, **kwargs):  # noqa: ANN002, ANN003
        assert reviewer._gate.locked(), "the spawn must be inside the driver's gate"
        entered.set()
        assert proceed.wait(timeout=30), "the spawn gate was never released"
        child = original_popen(*args, **kwargs)
        children.append(child.pid)
        return child

    monkeypatch.setattr(acpx_dsh_module, "popen_in_boundary", pause_inside_the_gate)
    try:
        with RunningRun(controller, run_request) as running:
            assert entered.wait(timeout=30), "the spawn gate was never held"
            run_id = running.run_id()
            assert reviewer._handles == {}, "the handle is published only after the child exists"

            def stop() -> None:
                try:
                    receipts.append(controller.cancel(run_id))
                except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                    errors.append(exc)
                finally:
                    cancel_returned.set()

            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            try:
                returned_early = cancel_returned.wait(timeout=1.0)
            finally:
                proceed.set()  # let the spawn finish, as a real one would
            stopper.join(timeout=30)

        assert not stopper.is_alive(), "the stop never finished"
        assert not errors, f"the stop raised: {errors!r}"
        assert running.error is None, f"the run raised instead of blocking: {running.error!r}"
        assert not returned_early, (
            "the stop returned while a child was being created; it must wait for the publication"
        )
        assert children, "the child must have been created for this window to be exercised"
        assert receipts and receipts[0].status == "confirmed_stopped"
        assert receipts[0].mechanism == "forced", (
            "the child created during the stop must be terminated, not merely forgotten"
        )
        assert receipts[0].local_process_stopped is True
        assert store.get_run(run_id)["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    finally:
        proceed.set()
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)

