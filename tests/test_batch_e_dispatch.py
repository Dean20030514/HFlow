"""Batch E1 acceptance item 3: the one dispatch transaction, a stop, and the crash window.

``docs/batch-e-plan.md`` §7 E1 item 3 asks for two observable facts, and this file pins both -
for the implementer *and* the reviewer, at the store level and through the real ``Controller``:

* **a stop recorded first** refuses the dispatch transaction and moves *no* counter: the root's
  used count, the authorization's used count, the run's ``turns_reserved``, the attempt count and
  the invocation count are all read before and after and must be identical;
* **a reservation committed first** whose spawn never happened keeps the consumption and is
  recorded as exactly that - ``not_started`` with a NULL ``started_at``, which is not the same
  number as "an invocation was started".

Four facts are kept apart throughout, because they are not one number: an allowance *reserved*,
a process *started*, a launch that provably never happened, and a reservation nobody can account
for (``unknown``). The crash case - a controller that reserved and died before it launched
anything - must be closed as ``unknown`` without buying a second invocation or a second attempt,
and an unresolved invocation must keep blocking the whole root, including under a new revision.

Offline only: ``FakeDriver``, ``FakeCheckRunner``, ``tmp_path``. Concurrency is coordinated with
``threading.Event`` and by wrapping one store operation at a chosen point (the ``RaceSeam``
pattern already used in ``tests/test_cancel_routing.py``), never with a sleep.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hflow.authorization import AuthorizationBinding, AuthorizationRecord
from hflow.contracts import (
    AttemptState,
    CancellationReceipt,
    CheckPhase,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RefusedError,
    RootBudgetBinding,
    RootBudgetLimits,
    RunRequest,
    TaskSpec,
    TaskState,
)
from hflow.controller import Controller, inspect_run
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.report import report_json, status_text
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

PROJECT_ID = "demo-project"
CONTROLLER_ID = "dispatch-ledger-test"
AUTHORIZATION_ID = "AUTH-dispatch-test"
EXPIRES_AT = "2999-01-01T00:00:00Z"
FAKE_WRITE_PLAN = {"src/parser.py": "def parse(text):\n    return text\n"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _limits(
    *,
    max_top_level_submissions: int = 4,
    max_repairs: int = 0,
    deadline_seconds: int = 3600,
) -> RootBudgetLimits:
    return RootBudgetLimits(
        max_top_level_submissions=max_top_level_submissions,
        max_repairs=max_repairs,
        deadline_seconds=deadline_seconds,
    )


def _binding(store: Store, spec: TaskSpec, project_root: Path) -> RootBudgetBinding:
    """The root this task resolves: derived mechanically, ledger path included."""
    return RootBudgetBinding.derive(
        project_id=PROJECT_ID,
        repo_path=str(project_root),
        task_id=spec.task_id,
        ledger_path=store.path,
    )


def _authorization(
    *,
    spec: TaskSpec,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    project_root: Path,
    authorization_id: str = AUTHORIZATION_ID,
    max_submissions: int = 4,
) -> AuthorizationRecord:
    """A user-provenance artifact for this spec, as the CLI would have verified it."""
    return AuthorizationRecord(
        authorization_id=authorization_id,
        user_text="I approve one bounded run of this exact task against this root.",
        authorized_at="2026-09-25T00:00:00Z",
        max_top_level_submissions=max_submissions,
        binding=AuthorizationBinding(
            mode="m2-live-change",
            driver="fake",
            project_id=PROJECT_ID,
            repo_path=str(project_root),
            base_commit="0" * 40,
            spec_digest=spec.spec_digest(),
            spec_path=str(project_root / "task.json"),
            root_budget=binding,
        ),
        root_limits=limits,
    )


def _seed(
    store: Store,
    *,
    project,
    spec: TaskSpec,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    project_root: Path,
    run_id: str = "R-dispatch",
    authorization_id: str = AUTHORIZATION_ID,
    max_submissions: int = 4,
) -> str:
    """A claimed run plus its registered root and authorization: no dispatch yet."""
    store.register_root_budget(binding, limits)
    store.register_authorization(
        _authorization(
            spec=spec,
            binding=binding,
            limits=limits,
            project_root=project_root,
            authorization_id=authorization_id,
            max_submissions=max_submissions,
        ).as_store_record()
    )
    store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="dispatch-ledger-test",
        checks_digest=project.checks_digest(),
        turn_limit=spec.budget.max_agent_turns,
        repair_limit=0,
    )
    store.claim_run(run_id, CONTROLLER_ID)
    return run_id


def _reserve(
    store: Store,
    *,
    run_id: str,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    role: str,
    invocation_id: str,
    authorization_id: str = AUTHORIZATION_ID,
    authorization_max: int = 4,
    attempt_id: str | None = None,
):
    return store.reserve_dispatch(
        run_id=run_id,
        controller_id=CONTROLLER_ID,
        invocation_id=invocation_id,
        role=role,
        reservation_id=f"B-{invocation_id}",
        reserved_turns=1,
        reservation_expires_at=EXPIRES_AT,
        attempt_id=attempt_id,
        root_binding=binding,
        root_limits=limits,
        authorization_id=authorization_id,
        authorization_max=authorization_max,
    )


def _counters(
    store: Store, *, run_id: str, binding: RootBudgetBinding, authorization_id: str
) -> dict[str, int]:
    """The five counters a refused dispatch must not move, as one comparable mapping.

    Read through the ``Store`` accessors on purpose: a *refusal* has already rolled back, so
    there is no writer in flight to skew these reads. The agreement of counters committed by a
    *successful* dispatch is asserted separately, in one SQL snapshot (``_snapshot``).
    """
    view = store.root_budget_view(binding.root_id)
    state = store.authorization_state(authorization_id)
    row = store.get_run(run_id)
    return {
        "root_used": view.used_top_level_submissions if view is not None else -1,
        "root_repairs": view.used_repairs if view is not None else -1,
        "authorization_used": int(state["used_top_level_submissions"]) if state else -1,
        "run_turns_reserved": int(row["turns_reserved"]),
        "attempt_rows": len(store.attempts_for(run_id)),
        "invocation_rows": len(store.invocations_for(run_id)),
    }


def _snapshot(
    path: Path, *, run_id: str, root_id: str, authorization_id: str, invocation_id: str
) -> dict[str, object]:
    """Root usage, authorization usage, run turns, attempt count and the invocation row.

    One statement, on a separate connection: three separate reads are three snapshots, and a
    dispatch committing between them would show a skew that is an artifact of the observation.
    """
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT rb.used_top_level_submissions AS root_used,
                   rb.used_repairs               AS root_repairs,
                   (SELECT used_top_level_submissions FROM authorizations
                     WHERE authorization_id = ?) AS auth_used,
                   (SELECT turns_reserved FROM runs WHERE run_id = ?) AS run_turns,
                   (SELECT COUNT(*) FROM attempts WHERE run_id = ?) AS attempt_rows,
                   i.role                        AS invocation_role,
                   i.state                       AS invocation_state,
                   i.root_used_at_reservation    AS invocation_root_used,
                   i.authorization_used_at_reservation AS invocation_auth_used
              FROM root_budgets rb
              JOIN invocations i ON i.invocation_id = ?
             WHERE rb.root_id = ?
            """,
            (authorization_id, run_id, run_id, invocation_id, root_id),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the snapshot found no root row / invocation row"
    return dict(row)


def _review_phase_run(
    store: Store,
    *,
    project,
    spec: TaskSpec,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    project_root: Path,
    authorization_id: str = AUTHORIZATION_ID,
    run_id: str = "R-dispatch",
) -> None:
    """Seed the state a review is dispatched from: one settled implementer dispatch, phase=review."""
    reservation = _reserve(
        store,
        run_id=run_id,
        binding=binding,
        limits=limits,
        role="implementer",
        invocation_id="I-implementer",
        authorization_id=authorization_id,
    )
    store.mark_invocation_started("I-implementer")
    store.settle_invocation(
        "I-implementer", outcome=InvocationOutcome.COMPLETED, detail="seeded implementer result"
    )
    store.finish_attempt(
        run_id=run_id,
        attempt_id=reservation.attempt_id,
        state=AttemptState.SUCCEEDED,
        outcome=InvocationOutcome.COMPLETED,
        result={"invocation_id": "I-implementer"},
    )
    store.advance_to_checking(
        run_id=run_id, attempt_id=reservation.attempt_id, phase=CheckPhase.REVIEW
    )


def _root_controller(
    store: Store,
    driver: FakeDriver,
    *,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    authorization: AuthorizationRecord,
    data_dir: Path,
    reviewer_driver: FakeDriver | None = None,
) -> Controller:
    """The controller shape a root run has: a root binding, its limits and a real artifact.

    The pre-flight is wired because an authorized run requires one; it is not what these tests
    measure, and it makes no call.
    """
    runner = FakeCheckRunner()
    return Controller(
        store,
        driver,
        reviewer_driver=reviewer_driver,
        controller_build="dispatch-ledger-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=data_dir,
        authorization=authorization,
        preflight=lambda: (True, "the zero-model pre-flight is not what this test measures"),
        root_binding=binding,
        root_limits=limits,
        production=False,
    )


class GatedDriver(FakeDriver):
    """A fake role driver that announces ``start`` and can be held inside it.

    ``FakeDriver.start`` records the request; the gate is entered *before* that, so when a test
    observes ``entered`` the invocation is running and no result exists yet - which is exactly
    the window a stop must be able to reach. The driver's own ``stop_requested`` question is the
    one the production driver asks inside its spawn gate.
    """

    def __init__(self, project_root: Path, *, label: str, gated_roles: set[str]) -> None:
        super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
        self.label = label
        self.gated_roles = gated_roles
        #: Set when this driver is inside ``start`` for a gated role.
        self.entered = threading.Event()
        #: Open to let every gated role through; the test sets it, never a timeout.
        self.released = threading.Event()
        #: Invocation ids this driver was asked to stop, in order.
        self.cancel_calls: list[str] = []

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        if request.role in self.gated_roles and not self.released.is_set():
            self.entered.set()
            assert self.released.wait(timeout=30), "the run gate was never released"
        return super().start(request)

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancel_calls.append(invocation_id)
        return CancellationReceipt(
            invocation_id=invocation_id,
            status="confirmed_stopped",
            mechanism="forced",
            local_process_stopped=True,
            detail=f"{self.label} driver: the stop reached the invocation",
        )


class RunningTask:
    """One ``run_task`` driven on a background thread, so a real stop can race it."""

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

    def __enter__(self) -> RunningTask:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release_all()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "the run thread is stuck; the gate was never released"

    def release_all(self) -> None:
        for driver in (self.controller.driver, self.controller.reviewer_driver):
            if isinstance(driver, GatedDriver):
                driver.released.set()


def _run_or_refusal(controller: Controller, request: RunRequest):
    """``(outcome, refusal)``: a refusal is a ``RunOutcome`` or a raised ``RefusedError``.

    Both shapes are refusals of the same dispatch; which one a caller sees depends only on
    whether the run row existed when the dispatch transaction was reached.
    """
    try:
        return controller.run_task(request), None
    except RefusedError as exc:
        return None, exc


# --------------------------------------------------------------------------
# 1. a stop recorded first: the dispatch is refused and no counter moves
# --------------------------------------------------------------------------


def test_a_recorded_stop_refuses_the_implementer_dispatch_and_moves_no_counter(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The stop commits first: nothing is reserved, charged, attempted or recorded."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)
    assert before == {
        "root_used": 0,
        "root_repairs": 0,
        "authorization_used": 0,
        "run_turns_reserved": 0,
        "attempt_rows": 0,
        "invocation_rows": 0,
    }, f"the baseline is not a fresh run: {before}"

    store.record_cancel_intent(run_id)

    with pytest.raises(StoreError) as excinfo:
        _reserve(
            store, run_id=run_id, binding=binding, limits=limits,
            role="implementer", invocation_id="I-cancelled",
        )

    assert "cancellation intent" in str(excinfo.value), str(excinfo.value)
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "a refused dispatch moved a counter"
    assert store.get_run(run_id)["cancel_intent_at"], "the stop itself stays recorded"
    assert store.invocations_for(run_id) == [], "no invocation may be recorded for a stopped run"
    assert store.attempts_for(run_id) == [], "no attempt may be created for a stopped run"


def test_a_recorded_stop_refuses_the_reviewer_dispatch_and_moves_no_counter(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The reviewer's reservation is the same transaction: a stop first refuses it too.

    The run is already in ``phase=review``, so the refusal can only come from the recorded stop -
    not from a phase rule that would have refused the dispatch anyway.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _review_phase_run(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root, run_id=run_id,
    )
    assert store.get_run(run_id)["phase"] == CheckPhase.REVIEW.value
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)
    assert before == {
        "root_used": 1,
        "root_repairs": 0,
        "authorization_used": 1,
        "run_turns_reserved": 1,
        "attempt_rows": 1,
        "invocation_rows": 1,
    }, f"the seeded review handoff is not what this test expects: {before}"

    store.record_cancel_intent(run_id)

    with pytest.raises(StoreError) as excinfo:
        _reserve(
            store, run_id=run_id, binding=binding, limits=limits,
            role="reviewer", invocation_id="I-review-cancelled",
        )

    assert "cancellation intent" in str(excinfo.value), str(excinfo.value)
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "the refused review reservation moved a counter"
    assert [entry.invocation_id for entry in store.invocations_for(run_id)] == ["I-implementer"], (
        "no reviewer invocation may be recorded once a stop has been"
    )


# --------------------------------------------------------------------------
# 2. a reservation committed first, whose spawn never happened
# --------------------------------------------------------------------------


def test_a_suppressed_spawn_keeps_the_spend_and_is_not_a_started_call(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The reservation is kept, the invocation is ``not_started``, and no start is claimed."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    reservation = _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-suppressed",
    )
    assert reservation.is_new and reservation.invocation is not None
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)

    # What a stop that won the handoff records: the reservation committed, the launch did not.
    store.mark_invocation_not_started(
        "I-suppressed", "the stop won the handoff before any process existed"
    )

    intent = store.invocation("I-suppressed")
    assert intent is not None
    assert intent.state is InvocationStartState.NOT_STARTED
    assert intent.started_at is None, "a suppressed launch must not carry a start time"
    assert intent.pending is False, "a closed reservation no longer blocks its root"

    counts = store.invocation_state_counts(run_id)
    assert counts.started == 0, "a reserved-then-suppressed invocation is not a started one"
    assert counts.ever_started == 0, "the report must not present it as a call to the model"
    assert counts.not_started == 1
    assert [entry.invocation_id for entry in store.unstarted_invocations(run_id)] == [
        "I-suppressed"
    ], "the report reads its 'consumed but never launched' set from exactly this record"

    # The consumption is kept: this is a fact about what happened, not a refund.
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "recording a suppressed launch changed the accounting"
    view = store.root_budget_view(binding.root_id)
    assert view is not None and view.used_top_level_submissions == 1
    assert store.get_run(run_id)["turns_reserved"] == 1
    assert store.mark_invocation_started("I-suppressed") is False, (
        "a suppressed reservation can never be rounded up to a started call"
    )


def test_the_report_does_not_count_a_suppressed_launch_as_a_started_call(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The reader's own view of the same row: reserved/started/model request stay three numbers."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-suppressed",
    )
    store.mark_invocation_not_started("I-suppressed", "the stop won the handoff")

    inspection = inspect_run(store, run_id)

    counts = inspection.invocation_counts
    assert counts.not_started == 1
    assert counts.started == 0 and counts.ever_started == 0, (
        "a suppressed launch is not a started invocation"
    )
    payload = report_json(inspection)
    reported = payload["invocation_counts"]
    assert reported["not_started"] == 1, reported
    assert reported["started"] == 0, reported
    # ``ever_started`` is derived (started + settled + unknown), so the serialised form carries
    # the three facts it is derived from; all three must be zero for a suppressed launch.
    assert reported["settled"] == 0 and reported["unknown"] == 0, reported
    assert (
        reported["started"] + reported["settled"] + reported["unknown"] == 0
    ), f"the report counts a call to the model that never happened: {reported}"

    text = status_text(inspection)
    # The count line carries every state, including the two the E1 review added: a launch that
    # was requested and never confirmed, and a launch that produced no process.
    assert (
        "reserved=0 requested=0 started=0 not_started=1 settled=0 unknown=0 launch_unknown=0"
        in text
    ), text
    assert "ever started 0" in text, text
    assert "state=not_started" in text, text
    assert "started_at=" not in text, "no invocation in this run was ever started"
    assert "provider model requests: unknown" in text, (
        "a reservation is never reported as a provider model request"
    )


def test_the_controller_records_a_reservation_whose_spawn_a_stop_suppressed(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production path: a real stop commits between the reserve and the launch.

    The seam wraps the *dispatch transaction itself*, so the stop is recorded strictly after the
    reservation committed and strictly before the controller could hand anything to a driver.
    What must be observable afterwards is the acceptance's own wording: the consumption stays,
    the invocation is ``not_started`` with a NULL ``started_at``, and the ledger does not count
    it as a started invocation.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver = GatedDriver(project_root, label="implementer", gated_roles=set())
    controller = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original = store.reserve_dispatch

    def reserve_then_stop(**kwargs):  # noqa: ANN003, ANN201 - the store method's own shape
        reservation = original(**kwargs)
        if reservation.is_new:
            controller.cancel(kwargs["run_id"])  # a real stop, committed after the reservation
        return reservation

    monkeypatch.setattr(store, "reserve_dispatch", reserve_then_stop)
    outcome = controller.run_task(run_request)

    intents = store.invocations_for(outcome.run_id)
    assert [entry.role for entry in intents] == ["implementer"], [e.role for e in intents]
    intent = intents[0]
    assert intent.state is InvocationStartState.NOT_STARTED, (
        f"a reservation whose launch a stop suppressed is not_started, not {intent.state}"
    )
    assert intent.started_at is None, "the suppressed launch must not carry a start time"
    counts = store.invocation_state_counts(outcome.run_id)
    assert counts.ever_started == 0, "a start that was suppressed is not a call to the model"
    assert driver.stopped_before_start == [intent.invocation_id], (
        "the driver must see the stop inside its own gate, before it does any work"
    )
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 1, (
        "the reservation is kept: a suppressed launch is not a refund"
    )
    assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR, outcome.block_reason
    row = store.get_run(outcome.run_id)
    assert row["turns_reserved"] == 1
    assert int(row["turns_observed"] or 0) == 0, "a suppressed launch observed no agent turn"


# --------------------------------------------------------------------------
# 3. the crash window: reserved, never launched, then resumed
# --------------------------------------------------------------------------


def test_resume_closes_a_reservation_that_never_launched_as_unknown(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    tmp_path: Path,
) -> None:
    """A controller that died between the commit and the launch leaves a blocked root, not a retry.

    The invocation was reserved and *no driver was ever asked*, so the ledger says exactly that:
    ``launch_unknown`` - a launch that was never requested and never confirmed, which still blocks
    the root and keeps the consumption. It is deliberately not ``unknown``: ``unknown`` means a
    process existed and its result was never observed, and counting this as one would report work
    that never happened. Recovery must not mint a second invocation id or a second attempt row.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    reservation = _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-crashed",
    )
    assert store.invocation("I-crashed").state is InvocationStartState.RESERVED
    # What the operator sees after a controller died mid-dispatch: the run is unresolved.
    store.set_blocked(
        run_id, RefusalCode.OUTCOME_UNKNOWN, "the controller died between the reserve and the launch"
    )
    driver = GatedDriver(project_root, label="implementer", gated_roles=set())
    controller = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    controller.resume(run_id)

    intent = store.invocation("I-crashed")
    assert intent is not None
    assert intent.state is InvocationStartState.LAUNCH_UNKNOWN, (
        "a reservation no driver was ever asked about is an unconfirmed launch - not started, "
        "not settled, not refunded"
    )
    assert intent.launch_requested_at is None, "no driver was asked, and the ledger says so"
    assert intent.started_at is None
    assert store.invocation_state_counts(run_id).ever_started == 0, (
        "no process was reported, so nothing may be counted as started"
    )
    assert driver.started == [], "resume never re-dispatches"
    assert [entry.invocation_id for entry in store.invocations_for(run_id)] == ["I-crashed"], (
        "recovery must not mint a second invocation"
    )
    assert len(store.attempts_for(run_id)) == 1, "recovery must not create a second attempt"
    assert store.get_run(run_id)["task_state"] == TaskState.BLOCKED.value
    view = store.root_budget_view(binding.root_id)
    assert view is not None and view.used_top_level_submissions == 1, "unknown is not a refund"
    assert [entry.invocation_id for entry in store.pending_invocations(binding.root_id)] == [
        "I-crashed"
    ], "an unresolved invocation keeps blocking its root"


# --------------------------------------------------------------------------
# 4. an unresolved invocation blocks the root, including a new revision
# --------------------------------------------------------------------------


def test_a_new_revision_cannot_dispatch_while_the_root_has_an_unresolved_invocation(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    tmp_path: Path,
) -> None:
    """A different revision is a different spec digest, not a different allowance."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-unresolved",
    )
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)

    # A new revision is a new task revision, so it needs its own approval - but the same root.
    revision_two = task_spec.model_copy(
        update={"revision": 2, "goal": f"{task_spec.goal} (revision 2)"}
    )
    second_authorization = _authorization(
        spec=revision_two,
        binding=binding,
        limits=limits,
        project_root=project_root,
        authorization_id="AUTH-dispatch-test-r2",
    )
    store.register_authorization(second_authorization.as_store_record())
    driver = GatedDriver(project_root, label="implementer", gated_roles=set())
    controller = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=second_authorization,
        data_dir=tmp_path / "data",
    )
    request = RunRequest(
        task=revision_two,
        project=project,
        project_root=project_root,
        workspace_root=project_root,
    )

    outcome, refusal = _run_or_refusal(controller, request)

    if refusal is not None:
        assert refusal.code is RefusalCode.BUDGET_EXHAUSTED, refusal
        reason = str(refusal)
    else:
        assert outcome is not None
        assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED, outcome.block_reason
        reason = outcome.block_reason or ""
    assert "unresolved invocation" in reason, reason

    assert driver.started == [], "no driver call may happen while the root is unresolved"
    second = store.find_run_by_spec_digest(project.project_id, revision_two.spec_digest())
    if second is not None:
        second_run = str(second["run_id"])
        assert store.attempts_for(second_run) == [], "the refused revision must have no attempt"
        assert store.invocations_for(second_run) == [], "and no invocation record"
        assert int(store.get_run(second_run)["turns_reserved"]) == 0, "and no reserved turn"
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "the refused revision moved the first run's counters"
    assert store.authorization_state("AUTH-dispatch-test-r2")["used_top_level_submissions"] == 0, (
        "the second approval must not be charged for a dispatch that never happened"
    )
    root = store.root_budget_view(binding.root_id)
    assert root is not None and root.used_top_level_submissions == 1


# --------------------------------------------------------------------------
# 5. the reviewer goes through the same one transaction
# --------------------------------------------------------------------------


def test_the_review_dispatch_charges_root_run_and_authorization_in_one_snapshot(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """After the review is bought, all three counters and the invocation row agree at once."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _review_phase_run(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root, run_id=run_id,
    )

    reservation = _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="reviewer", invocation_id="I-review",
    )

    assert reservation.is_new and reservation.invocation is not None
    assert reservation.invocation.role == "reviewer"
    assert reservation.invocation.state is InvocationStartState.RESERVED
    # The reservation itself carries the counters as committed by that one transaction.
    assert reservation.invocation.root_used_at_reservation == 2
    assert reservation.authorization_used == 2
    assert reservation.run_turns_reserved == 2

    snapshot = _snapshot(
        store.path,
        run_id=run_id,
        root_id=binding.root_id,
        authorization_id=AUTHORIZATION_ID,
        invocation_id="I-review",
    )
    assert snapshot["root_used"] == 2, snapshot
    assert snapshot["auth_used"] == 2, snapshot
    assert snapshot["run_turns"] == 2, snapshot
    assert snapshot["attempt_rows"] == 1, (
        "a review is not a second attempt: it reviews the implementer's own attempt row"
    )
    assert snapshot["invocation_role"] == "reviewer", snapshot
    assert snapshot["invocation_state"] == InvocationStartState.RESERVED.value, snapshot
    assert snapshot["invocation_root_used"] == 2, snapshot
    assert snapshot["invocation_auth_used"] == 2, snapshot

    attempt = store.attempts_for(run_id)[0]
    assert attempt["review_invocation_id"] == "I-review", (
        "the attempt's review column is written by the same transaction as the invocation row"
    )


def test_a_reviewer_dispatch_before_the_review_phase_is_refused_with_nothing_charged(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A review is only bought once the run is in review; before that, nothing is charged."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-implementer",
    )
    assert store.get_run(run_id)["phase"] is None
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)

    with pytest.raises(StoreError) as excinfo:
        _reserve(
            store, run_id=run_id, binding=binding, limits=limits,
            role="reviewer", invocation_id="I-review-too-early",
        )

    assert CheckPhase.REVIEW.value in str(excinfo.value), str(excinfo.value)
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "a refused review reservation must not charge the review turn"
    assert store.get_run(run_id)["turns_reserved"] == 1
    assert [entry.invocation_id for entry in store.invocations_for(run_id)] == ["I-implementer"]
    assert store.attempts_for(run_id)[0]["review_invocation_id"] is None


def test_a_second_invocation_id_for_the_same_role_cannot_dispatch_twice(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A fresh invocation id is not a way around the one-dispatch-per-role rule.

    Two shapes, both refused with nothing charged: a second reviewer id while the first review is
    still unresolved, and - the case that used to slip through - a second reviewer id *after* the
    first one has settled. The second shape is why this is a per-attempt rule and not a
    "is anything pending" rule: a settled review is finished, and buying another one would buy a
    second verdict on the same candidate and overwrite the attempt's record of the first.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _review_phase_run(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root, run_id=run_id,
    )
    first = _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="reviewer", invocation_id="I-review",
    )
    assert first.is_new
    before = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)

    with pytest.raises(StoreError) as excinfo:
        _reserve(
            store, run_id=run_id, binding=binding, limits=limits,
            role="reviewer", invocation_id="I-review-second",
        )

    assert "already dispatched a reviewer invocation" in str(excinfo.value), str(excinfo.value)
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "the second review reservation charged something"
    assert [entry.invocation_id for entry in store.invocations_for(run_id)] == [
        "I-implementer",
        "I-review",
    ]
    assert store.attempts_for(run_id)[0]["review_invocation_id"] == "I-review"

    # Now the first review is finished. The rule still holds: one review per candidate, and the
    # attempt keeps naming the invocation that actually produced the verdict.
    store.mark_invocation_started("I-review")
    store.settle_invocation("I-review", outcome=InvocationOutcome.COMPLETED, detail="verdict recorded")
    assert store.pending_invocations(binding.root_id) == [], "nothing is pending any more"
    settled_counters = _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    )
    with pytest.raises(StoreError) as second_after:
        _reserve(
            store, run_id=run_id, binding=binding, limits=limits,
            role="reviewer", invocation_id="I-review-third",
        )
    assert "already dispatched a reviewer invocation" in str(second_after.value)
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == settled_counters, "a refused second review moved a counter"
    assert [entry.invocation_id for entry in store.invocations_for(run_id)] == [
        "I-implementer",
        "I-review",
    ]
    assert store.attempts_for(run_id)[0]["review_invocation_id"] == "I-review", (
        "the attempt must keep naming the review that produced its verdict"
    )


# --------------------------------------------------------------------------
# 6. the same rules through the production controller path
# --------------------------------------------------------------------------


def test_a_stop_racing_the_running_implementer_refuses_the_review_dispatch(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """A real stop on another thread, while the implementer is inside the driver.

    The stop is recorded while the invocation runs, and the controller then has to finish the
    handoff without buying anything: no reviewer process, no review submission, no new
    reservation. The coordination is the driver's own ``start`` event - not a sleep.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles={"implementer"})
    reviewer = GatedDriver(project_root, label="reviewer", gated_roles=set())
    controller = _root_controller(
        store,
        implementer,
        reviewer_driver=reviewer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    with RunningTask(controller, run_request) as running:
        assert implementer.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        assert store.invocations_for(run_id), "the reservation must be committed before the launch"

        receipt = controller.cancel(run_id)
        accounted = _counters(
            store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
        )
        implementer.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    assert receipt.status == "confirmed_stopped"
    intents = store.invocations_for(run_id)
    assert implementer.cancel_calls == [intents[0].invocation_id], (
        "the stop must reach the invocation that was actually running"
    )
    assert reviewer.started == [], "no review may be dispatched after a recorded stop"
    assert [entry.role for entry in store.invocations_for(run_id)] == ["implementer"], (
        "the refused review reservation must not leave an invocation row"
    )
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == accounted, "a dispatch happened after the stop was recorded"
    assert accounted["root_used"] == 1 and accounted["authorization_used"] == 1
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert row["turns_reserved"] == 1, "the review turn was never bought"


def test_a_stop_that_wins_the_review_handoff_refuses_the_review_reservation(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tightest window of the review handoff: the stop commits, then the reservation is tried.

    The seam records a real stop immediately *before* the review's dispatch transaction runs, so
    the transaction itself is the thing being refused - rather than the controller deciding not
    to reach it. A refusal by cancellation is a stop, not a crash: the run keeps the operator's
    decision and exactly one submission stays charged.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles=set())
    reviewer = GatedDriver(project_root, label="reviewer", gated_roles=set())
    controller = _root_controller(
        store,
        implementer,
        reviewer_driver=reviewer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original = store.reserve_dispatch

    def stop_then_reserve(**kwargs):  # noqa: ANN003, ANN201 - the store method's own shape
        if kwargs["role"] == "reviewer":
            controller.cancel(kwargs["run_id"])  # the stop commits first
        return original(**kwargs)

    monkeypatch.setattr(store, "reserve_dispatch", stop_then_reserve)
    outcome = controller.run_task(run_request)

    assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR, outcome.block_reason
    assert reviewer.started == [], "the reviewer must not start for a stopped run"
    assert implementer.started and [request.role for request in implementer.started] == [
        "implementer"
    ]
    run_id = outcome.run_id
    assert [entry.role for entry in store.invocations_for(run_id)] == ["implementer"]
    counters = _counters(store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID)
    assert counters == {
        "root_used": 1,
        "root_repairs": 0,
        "authorization_used": 1,
        "run_turns_reserved": 1,
        "attempt_rows": 1,
        "invocation_rows": 1,
    }, f"the refused review reservation charged something: {counters}"
