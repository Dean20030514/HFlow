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

The last group (7) is about what a stop may do to that ledger afterwards: it closes only an
*open* entry, never an ``unknown``, ``launch_unknown``, ``settled`` or ``not_started`` one, and it
always leaves the run in a terminal state - even when the ledger bookkeeping itself fails.

Offline only: ``FakeDriver``, ``FakeCheckRunner``, ``tmp_path``, and in two cases the production
``AcpxDshDriver`` over the checked-in client and agent stand-ins (no model). Concurrency is
coordinated with ``threading.Event``, by wrapping one store operation at a chosen point (the
``RaceSeam`` pattern already used in ``tests/test_cancel_routing.py``) and by polling for a
recorded fact - never with a fixed sleep.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import hflow.controller as controller_module
from hflow.authorization import AuthorizationBinding, AuthorizationRecord
from hflow.contracts import (
    AttemptState,
    CancellationReceipt,
    CheckPhase,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RefusedError,
    ReviewOutput,
    RootBudgetBinding,
    RootBudgetLimits,
    RunRequest,
    SpawnKind,
    TaskSpec,
    TaskState,
)
from hflow.controller import Controller, inspect_run
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.report import report_json, status_text
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

from .test_driver_acpx_dsh import FAKE_CLIENT, STUB_AGENT

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


class SilentSpawnDriver(FakeDriver):
    """A driver that predates the spawn report: it never calls ``on_spawn``.

    Everything else is the offline fake, so the run reaches the same verdict a reporting driver
    does; only the launch facts have to come from its results instead of from a report.
    """

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        return super().start(request.model_copy(update={"on_spawn": None}))


def test_a_silent_driver_that_produced_work_is_recorded_as_launched(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """No spawn report arrived, but a candidate and a verdict did: both invocations launched.

    The controller records a launch request before every ``start``, so "a request exists" is not
    "a driver answered". Completed work is the answer a silent driver gives: each invocation is a
    launch, none is an unaccounted allowance, and no process is claimed - the driver never said
    whether its launch creates one.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    controller = _root_controller(
        store,
        SilentSpawnDriver(project_root),
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    intents = store.invocations_for(outcome.run_id)
    assert [entry.role for entry in intents] == ["implementer", "reviewer"]
    for intent in intents:
        assert intent.launched, f"{intent.role} produced work, so it launched: {intent}"
        assert intent.state is InvocationStartState.SETTLED, intent.state
        assert intent.spawn_kind is SpawnKind.UNKNOWN, (
            "a silent driver never said what its launch creates; the inference must not say it"
        )
        assert not intent.process_created, "an inferred launch is not an observed process"
    assert store.unstarted_invocations(outcome.run_id) == [], (
        "a run that produced a candidate and a verdict has no consumed allowance without a call"
    )
    counts = store.invocation_state_counts(outcome.run_id)
    assert counts.settled == 2
    assert counts.processes == 0 and counts.childless_launches == 0, counts


def test_a_silent_driver_cancelled_before_any_work_is_recorded_as_not_started(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No spawn report, a cancelled result and no work: the launch never happened.

    The stop commits after the reservation, so the controller still asks the driver; the driver
    sees the stop in its gate and returns without doing anything - and without saying so through
    ``on_spawn``. The ledger must read that as ``not_started``, keeping the consumption, and not
    leave it ``requested`` as an unconfirmed launch.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver = SilentSpawnDriver(project_root)
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
            controller.cancel(kwargs["run_id"])
        return reservation

    monkeypatch.setattr(store, "reserve_dispatch", reserve_then_stop)
    outcome = controller.run_task(run_request)

    intents = store.invocations_for(outcome.run_id)
    assert [entry.role for entry in intents] == ["implementer"]
    intent = intents[0]
    assert driver.stopped_before_start == [intent.invocation_id]
    assert intent.state is InvocationStartState.NOT_STARTED, intent.state
    assert intent.started_at is None
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 1, (
        "a launch that never happened is still not a refund"
    )
    # The stop reached the driver before it had started anything, so the driver could not
    # confirm it - the same ``unknown`` the production driver gives for an id with no handle and
    # no spawn in flight. The run therefore blocks unconfirmed; the ledger entry above is what
    # records that no launch happened.
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason


class SilentRoleStoppedMidRun(SilentSpawnDriver):
    """A driver without ``on_spawn`` whose role runs, then honours a stop recorded meanwhile.

    The stop is recorded by ``hflow cancel`` from outside (receipt ``unknown``) while the role is
    already working; the role then returns ``cancelled`` with no candidate and no agent turns.
    "No work" is therefore *not* "never launched" here: the implementer even changed the
    workspace before it stopped.
    """

    def __init__(self, project_root: Path, store: Store, *, role: str) -> None:
        super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
        self.store = store
        self.role = role
        self.receipts: list[CancellationReceipt] = []

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        from hflow.contracts import InvocationResult

        if request.role != self.role:
            return super().start(request)
        self.started.append(request)
        if request.role == "implementer":
            (Path(request.workspace) / "src" / "parser.py").write_text(
                "changed by a launch that was never reported\n", encoding="utf-8"
            )
        self.receipts.append(_cli_observer(self.store, self.project_root).cancel(request.run_id))
        assert request.stop_requested(), "the stop is recorded while the role runs"
        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=InvocationOutcome.CANCELLED,
            agent_turns=None,
            error_code="cancelled",
            error_message="stopped after the operator's stop was observed mid-run",
        )


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_silent_role_stopped_mid_run_is_not_recorded_as_never_launched(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    role: str,
) -> None:
    """A cancelled, workless result after a stop recorded *during* the run proves no non-launch.

    Only a stop recorded before the driver was asked lets the controller infer ``not_started``
    (the driver's gate must then have refused, see the test above). Here the stop came later and
    could not be confirmed, so the entry stays an open, unconfirmed launch that keeps blocking
    the root, and ``resume`` records it as ``launch_unknown``. The inference used to run ahead of
    the stop-conditional apply and record ``not_started``, which freed the root.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver = SilentRoleStoppedMidRun(project_root, store, role=role)
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

    outcome = controller.run_task(run_request)

    assert [receipt.status for receipt in driver.receipts] == ["unknown"]
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    (entry,) = [e for e in store.invocations_for(outcome.run_id) if e.role == role]
    assert entry.state is InvocationStartState.REQUESTED, (
        f"a launch that ran and was stopped is not a launch that never happened: {entry}"
    )
    assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
        entry.invocation_id
    ]

    controller.resume(outcome.run_id)

    reconciled = store.invocation(entry.invocation_id)
    assert reconciled is not None
    assert reconciled.state is InvocationStartState.LAUNCH_UNKNOWN, reconciled.state


def test_a_confirmed_stop_of_a_silent_implementer_that_ran_records_launch_unknown(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-process order: the run thread reads the driver's return before ``cancel`` books it.

    The driver ran (it changed the workspace), reported nothing, and was stopped by a confirmed
    stop. Whichever thread writes first, the ledger must say what the confirmed stop knows - a
    launch was requested and no report came back, so ``launch_unknown`` - and never infer
    ``not_started`` from the run thread's side of the race.
    """

    class SilentImplementerKilledInProcess(SilentSpawnDriver):
        def __init__(self, project_root: Path) -> None:
            super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
            self.killed = threading.Event()
            self.running = threading.Event()

        def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
            from hflow.contracts import InvocationResult

            self.started.append(request)
            (Path(request.workspace) / "src" / "parser.py").write_text(
                "changed by a launch that was never reported\n", encoding="utf-8"
            )
            self.running.set()
            assert self.killed.wait(30), "the stop never reached the driver"
            return InvocationResult(
                invocation_id=request.invocation_id,
                outcome=InvocationOutcome.CANCELLED,
                agent_turns=None,
                error_code="cancelled",
                error_message="killed by the confirmed stop",
            )

        def cancel(self, invocation_id: str) -> CancellationReceipt:
            receipt = super().cancel(invocation_id)
            self.killed.set()
            return receipt

    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver = SilentImplementerKilledInProcess(project_root)
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
    returned = threading.Event()
    original_confirm = Controller._confirm_driver_ran

    def confirm_then_signal(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        try:
            return original_confirm(self, *args, **kwargs)
        finally:
            returned.set()

    monkeypatch.setattr(Controller, "_confirm_driver_ran", confirm_then_signal)
    original_receipt = store.record_cancel_receipt

    def receipt_after_the_run_thread(run_id, receipt):  # noqa: ANN001, ANN202
        assert returned.wait(30), "the run thread never read the driver's return"
        return original_receipt(run_id, receipt)

    monkeypatch.setattr(store, "record_cancel_receipt", receipt_after_the_run_thread)
    receipts: list[CancellationReceipt] = []

    def stop() -> None:
        assert driver.running.wait(30)
        receipts.append(controller.cancel(str(store.list_runs(1)[0]["run_id"])))

    stopper = threading.Thread(target=stop, daemon=True)
    stopper.start()
    outcome = controller.run_task(run_request)
    stopper.join(30)
    assert not stopper.is_alive()

    assert [receipt.status for receipt in receipts] == ["confirmed_stopped"]
    row = store.get_run(outcome.run_id)
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value, row["block_reason"]
    (entry,) = store.invocations_for(outcome.run_id)
    assert entry.state is InvocationStartState.LAUNCH_UNKNOWN, (
        f"a stopped launch nobody reported is not a launch that never happened: {entry}"
    )
    assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
        entry.invocation_id
    ]


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


def test_a_controller_interrupted_during_the_review_blocks_the_root_for_resume(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """``SystemExit`` escaping the reviewer's ``start`` is recorded before it propagates.

    The controller cannot observe the reviewer's result any more, so the ledger entry becomes
    ``unknown`` (a process was reported), the run blocks ``outcome_unknown`` and the root stays
    blocked - and ``resume`` reconciles it instead of answering "no-op for a RUNNING run".
    """

    class ExitingReviewer(GatedDriver):
        def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
            self.started.append(request)
            self._report_spawn(request, created=True, detail="the review began")
            raise SystemExit(2)

    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles=set())
    reviewer = ExitingReviewer(project_root, label="reviewer", gated_roles=set())
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
        reviewer_driver=reviewer,
    )

    with pytest.raises(SystemExit):
        controller.run_task(run_request)

    run_id = str(store.list_runs()[0]["run_id"])
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value
    assert "controller interrupted during reviewer invocation" in row["block_reason"]
    states = {entry.role: entry.state for entry in store.invocations_for(run_id)}
    assert states == {
        "implementer": InvocationStartState.SETTLED,
        "reviewer": InvocationStartState.UNKNOWN,
    }, states
    assert [entry.role for entry in store.pending_invocations(binding.root_id)] == ["reviewer"]

    controller.resume(run_id)

    assert implementer.cancel_calls == [] and reviewer.cancel_calls == []
    assert len(implementer.started) == 1 and len(reviewer.started) == 1, "resume never re-dispatches"
    assert reviewer.reconciled == [reviewer.started[0].invocation_id]
    assert store.open_attempt(run_id)["reconcile_json"] is not None
    assert store.get_run(run_id)["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value


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


def test_a_rootless_revision_cannot_dispatch_a_task_that_already_has_a_root(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    tmp_path: Path,
) -> None:
    """Leaving out --root-budget-file does not step around the task's root and its open rows.

    The root identity is derived from the project, the repository and the task, so the legacy
    branch of the dispatch transaction can see that this task already has one. Spending outside
    it would bypass the unresolved invocation, the live-run gate and every cumulative ceiling.
    """
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

    revision_two = task_spec.model_copy(
        update={"revision": 2, "goal": f"{task_spec.goal} (revision 2)"}
    )
    driver = GatedDriver(project_root, label="implementer", gated_roles=set())
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        driver,
        controller_build="dispatch-ledger-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=tmp_path / "data",
        production=False,
    )
    request = RunRequest(
        task=revision_two,
        project=project,
        project_root=project_root,
        workspace_root=project_root,
    )

    outcome, refusal = _run_or_refusal(controller, request)

    # A refusal before the run row exists: a blocked row would make the refusal's own advice
    # (resubmit with --root-budget-file) return that blocked run instead of dispatching.
    assert outcome is None and refusal is not None, (
        outcome and (outcome.task_state, outcome.block_code, outcome.block_reason)
    )
    assert refusal.code is RefusalCode.BUDGET_EXHAUSTED, refusal
    reason = str(refusal)
    assert binding.root_id in reason and "--root-budget-file" in reason, reason

    assert driver.started == [], "no driver call may happen outside the task's root"
    assert store.find_run_by_spec_digest(project.project_id, revision_two.spec_digest()) is None
    assert _counters(
        store, run_id=run_id, binding=binding, authorization_id=AUTHORIZATION_ID
    ) == before, "the refused revision moved the root's counters"
    assert [entry.invocation_id for entry in store.pending_invocations(binding.root_id)] == [
        "I-unresolved"
    ]


def test_after_a_rootless_refusal_the_same_revision_runs_against_its_root(
    store: Store, project, task_spec: TaskSpec, project_root: Path, tmp_path: Path
) -> None:
    """Following the rootless refusal's advice dispatches the same spec against the root.

    The refusal records no run row, so the identical TaskSpec resubmitted with the root and an
    authorization covering it is a new dispatch, not a history query returning a blocked run -
    the approval issued for that revision is not wasted.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    store.register_root_budget(binding, limits)  # the task already has a root (revision 1)

    revision_two = task_spec.model_copy(
        update={"revision": 2, "goal": f"{task_spec.goal} (revision 2)"}
    )
    request = RunRequest(
        task=revision_two, project=project, project_root=project_root, workspace_root=project_root
    )
    runner = FakeCheckRunner()
    rootless = Controller(
        store,
        GatedDriver(project_root, label="implementer", gated_roles=set()),
        controller_build="dispatch-ledger-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=tmp_path / "rootless",
        production=False,
    )
    with pytest.raises(RefusedError) as excinfo:
        rootless.run_task(request)
    assert excinfo.value.code is RefusalCode.BUDGET_EXHAUSTED, excinfo.value
    assert store.find_run_by_spec_digest(project.project_id, revision_two.spec_digest()) is None

    driver = GatedDriver(project_root, label="implementer", gated_roles=set())
    rooted = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=revision_two, binding=binding, limits=limits, project_root=project_root,
            authorization_id="AUTH-after-rootless",
        ),
        data_dir=tmp_path / "rooted",
    )
    outcome = rooted.run_task(request)

    assert driver.started, "the identical spec with its root must dispatch"
    assert not any("identical TaskSpec" in note for note in outcome.notes), outcome.notes


def test_the_rootless_dispatch_transaction_refuses_a_task_that_has_a_root(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The refusal is the ledger's own, inside the transaction, not only a pre-flight read.

    The controller refuses this before the run row exists; reaching the transaction means the
    root was registered after that read, so the message says the run row is spent.
    """
    binding = _binding(store, task_spec, project_root)
    store.register_root_budget(binding, _limits())
    store.create_run(
        run_id="R-rootless",
        project_id=project.project_id,
        spec=task_spec,
        spec_digest=task_spec.spec_digest(),
        controller_build="dispatch-ledger-test",
        checks_digest=project.checks_digest(),
        turn_limit=task_spec.budget.max_agent_turns,
        repair_limit=0,
    )
    store.claim_run("R-rootless", CONTROLLER_ID)

    with pytest.raises(StoreError, match="new revision with --root-budget-file"):
        store.reserve_dispatch(
            run_id="R-rootless",
            controller_id=CONTROLLER_ID,
            invocation_id="I-rootless",
            role="implementer",
            reservation_id="B-rootless",
            reserved_turns=1,
            reservation_expires_at=EXPIRES_AT,
            repo_path=str(project_root),
        )
    assert store.attempts_for("R-rootless") == []
    assert int(store.get_run("R-rootless")["turns_reserved"]) == 0


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


# --------------------------------------------------------------------------
# 7. a stop closes only an open ledger entry, and always ends the run
#
# A confirmed stop closes the entry of the invocation it stopped while that entry is still open -
# ``reserved``, ``requested`` or ``started`` - and nothing else. Every other state already records
# what is known: ``not_started`` (no launch happened), ``settled`` (a result was applied),
# ``unknown`` and ``launch_unknown`` (only an operator reconcile closes them). A local stop is not
# evidence against any of those, and rewriting one either invents a fact or unblocks a root that
# must stay blocked. Separately, the run's terminal state is written *before* the ledger
# bookkeeping, so a ledger failure is a note on a stopped run - not an exception that leaves the
# run RUNNING and its root owned by it forever.
# --------------------------------------------------------------------------


def _wait_for(predicate: Callable[[], object], what: str, timeout: float = 30.0) -> None:
    """Poll for a recorded fact - never a fixed sleep - and fail naming the fact."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"never observed: {what}"
        time.sleep(0.01)


def _reserve_revision_two(
    store: Store,
    *,
    project,
    spec: TaskSpec,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    project_root: Path,
):
    """Seed revision 2 of the same task on the same root and try its implementer dispatch."""
    revision_two = spec.model_copy(update={"revision": 2, "goal": f"{spec.goal} (revision 2)"})
    run_two = _seed(
        store, project=project, spec=revision_two, binding=binding, limits=limits,
        project_root=project_root, run_id="R-revision-2", authorization_id="AUTH-dispatch-r2",
    )
    return _reserve(
        store, run_id=run_two, binding=binding, limits=limits, role="implementer",
        invocation_id="I-revision-2", authorization_id="AUTH-dispatch-r2",
    )


@pytest.mark.parametrize(
    "recorded",
    [
        InvocationStartState.NOT_STARTED,
        InvocationStartState.SETTLED,
        InvocationStartState.UNKNOWN,
        InvocationStartState.LAUNCH_UNKNOWN,
    ],
)
def test_settling_never_moves_an_entry_that_is_no_longer_open(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    recorded: InvocationStartState,
) -> None:
    """``settle_invocation`` is a compare-and-set from the open states, and says when it refused.

    Each of these states records a fact that a later settlement - a confirmed stop, a late driver
    result - is not evidence against. The row keeps its state, its blocking effect on the root,
    its settlement time and what it recorded. ``not_started`` additionally keeps the reported
    outcome beside the fact, which still does not make it a launch.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-closed",
    )
    store.mark_invocation_launch_requested("I-closed")
    if recorded is InvocationStartState.NOT_STARTED:
        store.mark_invocation_not_started("I-closed", "a recorded stop won the spawn gate")
    elif recorded is InvocationStartState.LAUNCH_UNKNOWN:
        store.mark_launch_unresolved("I-closed", "a stop was confirmed before any spawn report")
    else:
        store.mark_invocation_started("I-closed", process_created=False)
        store.settle_invocation(
            "I-closed",
            outcome=InvocationOutcome.COMPLETED
            if recorded is InvocationStartState.SETTLED
            else None,
            detail="the first observation",
        )
    before = store.invocation("I-closed")
    assert before is not None and before.state is recorded, before

    moved = store.settle_invocation(
        "I-closed", outcome=InvocationOutcome.CANCELLED, detail="a later stop"
    )

    after = store.invocation("I-closed")
    assert moved is False, "a settlement that changed nothing must say so"
    assert after is not None and after.state is recorded, (
        f"a {recorded.value} entry was moved to {after.state.value if after else None}"
    )
    assert after.pending is before.pending, "the settlement changed whether the root is blocked"
    assert (after.settled_at, after.started_at) == (before.settled_at, before.started_at)
    if recorded is InvocationStartState.NOT_STARTED:
        assert after.outcome is InvocationOutcome.CANCELLED, "the report is kept beside the fact"
    else:
        assert (after.outcome, after.detail) == (before.outcome, before.detail)
    assert [entry.invocation_id for entry in store.pending_invocations(binding.root_id)] == (
        ["I-closed"] if before.pending else []
    )


@pytest.mark.parametrize("launched", [True, False], ids=["started", "requested"])
def test_settling_an_open_entry_closes_it_and_says_so(
    store: Store, project, task_spec: TaskSpec, project_root: Path, launched: bool
) -> None:
    """The other half of the compare-and-set: an open entry is closed and the call reports it."""
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(
        store, run_id=run_id, binding=binding, limits=limits,
        role="implementer", invocation_id="I-open",
    )
    store.mark_invocation_launch_requested("I-open")
    if launched:
        store.mark_invocation_started("I-open", process_created=False)

    moved = store.settle_invocation(
        "I-open", outcome=InvocationOutcome.COMPLETED, detail="the driver's result"
    )

    after = store.invocation("I-open")
    assert moved is True
    assert after is not None
    assert (after.state, after.outcome) == (
        InvocationStartState.SETTLED,
        InvocationOutcome.COMPLETED,
    )
    assert after.pending is False and after.settled_at is not None


class StopWinsTheSpawnGate(GatedDriver):
    """A stop committed after the launch request wins this driver's spawn gate.

    ``start`` waits until the run's stop is durable and only then asks ``stop_requested`` - the
    question the production driver asks inside its gate - so the entry ends up as the finding
    found it: ``not_started`` with ``launch_requested_at`` set. ``cancel`` gives the offline
    driver's own answer (confirmed) only once ``answer_after`` returns, which is how a test
    decides whether the run thread or the stop finishes the attempt first.
    """

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root, label="implementer", gated_roles=set())
        #: Set once the gate has decided, and reported, that no launch happened.
        self.gate_decided = threading.Event()
        #: Called inside ``cancel`` before it answers.
        self.answer_after: Callable[[], object] = lambda: None

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        self.entered.set()
        _wait_for(request.stop_requested, "the stop was never recorded")
        result = FakeDriver.start(self, request)
        self.gate_decided.set()
        assert self.released.wait(timeout=30), "the run thread was never released"
        return result

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancel_calls.append(invocation_id)
        self.answer_after()
        return FakeDriver.cancel(self, invocation_id)


@pytest.mark.parametrize("finishes_first", ["run_thread", "stop"])
def test_a_stop_that_wins_the_spawn_gate_after_the_launch_request_still_ends_the_run(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    finishes_first: str,
) -> None:
    """The stop lands between the launch request and the driver's gate, and is confirmed.

    The gate reports that no launch happened, so the entry is ``not_started`` with its launch
    request recorded. The confirmed stop used to hand that entry to ``mark_launch_unresolved``,
    which refuses a ``not_started`` row - raising after the receipt was written and before the
    run was blocked. The run then stayed ``RUNNING`` for good: ``resume`` is a no-op for it, a
    second ``cancel`` returned the stored receipt, and the root refused every later revision as
    owned by this run. Both orders of the two ``finish_attempt`` calls reached that state.
    """
    binding = _binding(store, task_spec, project_root)
    # One repair is allowed, so revision 2 below is decided by the root's ownership and pending
    # rules, not by the repair ceiling.
    limits = _limits(max_repairs=1)
    driver = StopWinsTheSpawnGate(project_root)
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

    with RunningTask(controller, run_request) as running:
        assert driver.entered.wait(timeout=30), "the implementer never reached its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        if finishes_first == "run_thread":
            driver.released.set()
            driver.answer_after = lambda: running.thread.join(timeout=30)
        else:
            driver.answer_after = lambda: driver.gate_decided.wait(timeout=30)
        receipt = controller.cancel(run_id)
        driver.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    assert receipt.status == "confirmed_stopped"
    (entry,) = store.invocations_for(run_id)
    assert driver.stopped_before_start == [entry.invocation_id], "the gate must have seen the stop"
    assert driver.cancel_calls == [entry.invocation_id]
    assert entry.state is InvocationStartState.NOT_STARTED, entry
    assert entry.launch_requested_at is not None and entry.started_at is None, entry
    assert store.pending_invocations(binding.root_id) == []
    row = store.get_run(run_id)
    assert row["task_state"] == TaskState.BLOCKED.value, (row["task_state"], row["block_code"])
    assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
    assert controller.cancel(run_id) == receipt, "a repeated stop returns the recorded receipt"

    second = _reserve_revision_two(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    assert second.is_new, "a stopped run with nothing unresolved no longer owns its root"


def test_a_stop_that_wins_the_production_spawn_gate_after_the_launch_request_still_ends_the_run(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same window through the production driver over the checked-in client stand-in.

    The stop is durable before ``start_handle`` takes the spawn gate, so the gate creates no
    client and publishes a ``start_cancelled`` handle. The controller's stop is let through only
    after that, and ``cancel_handle`` confirms it from the handle. No process exists at any point
    and no model is contacted.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits(max_repairs=1)
    implementer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "cooperative"],
    )
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    entered, gate_decided = threading.Event(), threading.Event()
    launches: list[tuple[int | None, bool]] = []
    original_handle = implementer.start_handle
    original_driver_cancel = controller._driver_cancel

    def launch_after_the_stop(request):  # noqa: ANN001, ANN201 - the driver's own shape
        entered.set()
        _wait_for(request.stop_requested, "the stop was never recorded")
        handle = original_handle(request)
        launches.append((handle.pid, handle.start_cancelled))
        gate_decided.set()
        return handle

    def stop_after_the_gate(driver, invocation_id):  # noqa: ANN001, ANN201
        assert gate_decided.wait(timeout=30), "the spawn gate never decided"
        return original_driver_cancel(driver, invocation_id)

    implementer.start_handle = launch_after_the_stop  # type: ignore[method-assign]
    monkeypatch.setattr(controller, "_driver_cancel", stop_after_the_gate)
    try:
        with RunningTask(controller, run_request) as running:
            assert entered.wait(timeout=30), "the implementer never reached its driver"
            run_id = str(store.list_runs()[0]["run_id"])
            receipt = controller.cancel(run_id)

        assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
        assert launches == [(None, True)], "the production driver must have created no client"
        assert (receipt.status, receipt.mechanism) == ("confirmed_stopped", "none"), receipt
        (entry,) = store.invocations_for(run_id)
        assert entry.state is InvocationStartState.NOT_STARTED, entry
        assert entry.launch_requested_at is not None and entry.started_at is None, entry
        row = store.get_run(run_id)
        assert row["task_state"] == TaskState.BLOCKED.value, (row["task_state"], row["block_code"])
        assert row["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value
        assert _reserve_revision_two(
            store, project=project, spec=task_spec, binding=binding, limits=limits,
            project_root=project_root,
        ).is_new, "a stopped run with nothing unresolved no longer owns its root"
    finally:
        for invocation_id in list(implementer._handles):
            implementer.release(invocation_id)


def test_a_stop_after_an_unknown_outcome_keeps_the_entry_unknown_and_the_run_reconcilable(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """A confirmed *local* stop says nothing about a result nobody observed.

    The implementer's result is unknown, so the run is ``BLOCKED/outcome_unknown`` and its entry
    is ``unknown``: the root stays blocked until an operator reconciles it. A stop requested now
    is still sent to the driver - a child can outlive an unknown result - and its receipt is
    recorded. It used to do more: settle the ``unknown`` entry as ``cancelled``, which unblocked
    the root for a new revision, and relabel the block as ``cancelled_by_operator``, which made
    ``resume`` a no-op instead of a reconcile.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits(max_repairs=1)
    driver = FakeDriver(
        project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, unknown_invocations=1)
    )
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
    outcome = controller.run_task(run_request)
    run_id = outcome.run_id
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    (before,) = store.invocations_for(run_id)
    assert before.state is InvocationStartState.UNKNOWN, before

    receipt = controller.cancel(run_id)

    assert receipt.status == "confirmed_stopped"
    assert driver.cancelled == [before.invocation_id], "a possibly-alive child is still stopped"
    assert store.cancel_state(run_id)[1] == receipt, "the stop's receipt is recorded"
    row = store.get_run(run_id)
    assert (row["task_state"], row["block_code"]) == (
        TaskState.BLOCKED.value,
        RefusalCode.OUTCOME_UNKNOWN.value,
    ), "a stop must not relabel an unknown outcome"
    assert store.invocation(before.invocation_id) == before, "a stop rewrote the unknown entry"
    assert [entry.invocation_id for entry in store.pending_invocations(binding.root_id)] == [
        before.invocation_id
    ]

    with pytest.raises(StoreError) as refused:
        _reserve_revision_two(
            store, project=project, spec=task_spec, binding=binding, limits=limits,
            project_root=project_root,
        )
    assert "unresolved invocation" in str(refused.value), str(refused.value)

    resumed = controller.resume(run_id)
    assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN
    assert driver.reconciled == [before.invocation_id], "resume still reconciles the run"
    assert len(driver.started) == 1, "resume never re-dispatches"


def test_a_late_implementer_result_does_not_settle_a_launch_a_forced_stop_left_unresolved(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The implementer's half of the reviewer case in ``tests/test_batch_e_review.py`` (8).

    A real managed child exists, the controller's record of its spawn is made to fail, and the
    stop force-terminates it: the confirmed stop records ``launch_unknown``, because no launch is
    recorded and yet a process demonstrably existed. The driver's ``cancelled`` result arrives
    afterwards. The reviewer path never settled over that state; the implementer's non-completed
    path did, and turned ``launch_unknown`` into ``settled`` - which unblocked the root.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits(max_repairs=1)
    implementer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "stubborn"],
    )
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original_record = store.record_invocation_spawn
    facts: list[object] = []
    after_stop: list[InvocationStartState] = []

    def fail_implementer_record(fact):  # noqa: ANN001, ANN201 - the store method's own shape
        recorded = store.invocation(fact.invocation_id)
        if recorded is not None and recorded.role == "implementer":
            facts.append(fact)
            raise StoreError("injected failure to persist the spawn observation")
        return original_record(fact)

    def start_then_stop(request):  # noqa: ANN001, ANN201 - the driver's own shape
        handle = implementer.start_handle(request)
        assert handle.pid is not None, "the real managed client must exist"
        receipt = controller.cancel(request.run_id)
        assert (receipt.status, receipt.mechanism) == ("confirmed_stopped", "forced"), receipt
        after_stop.append(store.invocation(request.invocation_id).state)
        return implementer.collect(handle)

    monkeypatch.setattr(store, "record_invocation_spawn", fail_implementer_record)
    monkeypatch.setattr(implementer, "start", start_then_stop)
    try:
        outcome = controller.run_task(run_request)

        assert facts and facts[0].created and facts[0].pid is not None, (
            "the driver did report a real child; only the record of it was made to fail"
        )
        assert after_stop == [InvocationStartState.LAUNCH_UNKNOWN]
        (entry,) = store.invocations_for(outcome.run_id)
        assert entry.state is InvocationStartState.LAUNCH_UNKNOWN, (
            f"the late cancelled result moved the entry: {entry.model_dump(mode='json')}"
        )
        assert entry.pending is True
        assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
            entry.invocation_id
        ], "an unrecorded launch keeps blocking the root"
        assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR, outcome.block_reason
        refused = [
            note
            for note in store.notes_for(outcome.run_id)
            if note.startswith("dispatch") and entry.invocation_id in note and "launch_unknown" in note
        ]
        assert refused, "the refused settlement must be recorded, not silently dropped"
    finally:
        for invocation_id in list(implementer._handles):
            implementer.release(invocation_id)


def _cli_observer(store: Store, project_root: Path) -> Controller:
    """What ``hflow cancel`` builds for an offline run: a second controller holding no handle."""
    fresh = FakeDriver(project_root)
    return Controller(store, fresh, reviewer_driver=fresh, controller_build="cli-observer")


class StoppedFromAnotherProcess(FakeDriver):
    """A role that is stopped by ``hflow cancel`` while it runs, and then returns a result.

    The work happens and the offline spawn is reported first; then the stop is recorded the way
    the CLI records it - through a second controller over a fresh driver that owns no handle, so
    its receipt is ``unknown`` - and only after that does ``start`` return ``late``.
    """

    def __init__(
        self, project_root: Path, store: Store, *, role: str, late: InvocationOutcome
    ) -> None:
        super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
        self.store = store
        self.role = role
        self.late = late
        self.receipts: list[CancellationReceipt] = []

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        from hflow.contracts import InvocationResult

        result = super().start(request)
        if request.role != self.role:
            return result
        self.receipts.append(_cli_observer(self.store, self.project_root).cancel(request.run_id))
        if self.late is InvocationOutcome.COMPLETED:
            return result
        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=self.late,
            agent_turns=1,
            error_code="failed",
            error_message="the role failed after the operator's stop was recorded",
        )


@pytest.mark.parametrize(
    ("role", "late"),
    [
        ("implementer", InvocationOutcome.COMPLETED),
        ("implementer", InvocationOutcome.FAILED),
        ("reviewer", InvocationOutcome.COMPLETED),
        ("reviewer", InvocationOutcome.FAILED),
    ],
)
def test_a_late_result_after_an_unconfirmed_stop_changes_nothing_for_either_role(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    role: str,
    late: InvocationOutcome,
) -> None:
    """AGENTS rules 4 and 8, with one rule for both roles.

    A cross-process ``hflow cancel`` cannot confirm the stop, so the run blocks
    ``outcome_unknown`` with the role's ledger entry open. The owner's late result is then only a
    ``late_result`` note: the entry stays ``started`` and keeps blocking the root, no review
    verdict or review evidence is recorded, a new revision is refused, and ``resume`` turns the
    entry into ``unknown`` without re-dispatching. The reviewer used to settle the entry (and so
    free the root) and record its evidence, while the implementer did not.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits(max_repairs=1)
    driver = StoppedFromAnotherProcess(project_root, store, role=role, late=late)
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

    outcome = controller.run_task(run_request)
    run_id = outcome.run_id

    assert [receipt.status for receipt in driver.receipts] == ["unknown"]
    assert outcome.task_state is TaskState.BLOCKED, outcome
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    (entry,) = [e for e in store.invocations_for(run_id) if e.role == role]
    assert entry.state is InvocationStartState.STARTED, (
        f"a late {late.value} result closed an entry an unconfirmed stop left open: "
        f"{entry.model_dump(mode='json')}"
    )
    assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
        entry.invocation_id
    ], "work may still be running, so the root stays blocked"
    late_notes = [note for note in store.notes_for(run_id) if note.startswith("late_result")]
    assert len(late_notes) == 1, store.notes_for(run_id)
    assert entry.invocation_id in late_notes[0] and late.value in late_notes[0], late_notes
    assert store.evidence_for(run_id, kind="review") == [], "no verdict is recorded after a stop"
    attempts = store.attempts_for(run_id)
    assert all(row["review_json"] is None for row in attempts), "the late verdict was attached"
    if role == "implementer":
        assert attempts[-1]["state"] == AttemptState.ACTIVE.value, attempts[-1]["state"]

    with pytest.raises(StoreError) as refused:
        _reserve_revision_two(
            store, project=project, spec=task_spec, binding=binding, limits=limits,
            project_root=project_root,
        )
    assert "unresolved invocation" in str(refused.value), str(refused.value)

    started = len(driver.started)
    resumed = controller.resume(run_id)
    assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN
    assert len(driver.started) == started, "resume never re-dispatches"
    reconciled = store.invocation(entry.invocation_id)
    assert reconciled is not None and reconciled.state is InvocationStartState.UNKNOWN
    assert "did not observe a result" not in reconciled.detail, (
        "the controller did observe a result - it is the late_result note - so the reconcile "
        f"detail must not deny it: {reconciled.detail}"
    )


def test_a_stop_landing_inside_the_acceptance_is_the_recorded_state_not_an_exception(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop committed after ``_accept`` read the run and before its acceptance write.

    ``finalize_acceptance`` refuses the late success in its own transaction, which is right; the
    refusal used to escape ``run_task`` as ``StoreError`` (and ``hflow run`` with a traceback).
    The run must instead report the state the stop recorded, with no receipt.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    controller = _root_controller(
        store,
        FakeDriver(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1)),
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original = controller_module.candidate_fingerprint
    receipts: list[CancellationReceipt] = []

    def fingerprint_then_stop(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        value = original(*args, **kwargs)
        if sys._getframe(1).f_code.co_name == "_accept" and not receipts:
            run_id = str(store.list_runs(1)[0]["run_id"])
            receipts.append(_cli_observer(store, project_root).cancel(run_id))
        return value

    monkeypatch.setattr(controller_module, "candidate_fingerprint", fingerprint_then_stop)

    outcome = controller.run_task(run_request)

    assert [receipt.status for receipt in receipts] == ["unknown"]
    assert outcome.task_state is TaskState.BLOCKED, outcome
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    row = store.get_run(outcome.run_id)
    assert not row["receipt_json"], "no receipt may be written for a stopped run"
    assert row["task_state"] == TaskState.BLOCKED.value


def test_a_stop_after_a_rejected_review_rewrites_neither_the_entries_nor_the_block(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """A run that already ended keeps the outcome it ended with, and so do its entries.

    The run is ``BLOCKED/review_rejected`` and both entries are ``settled/completed``. A stop
    requested afterwards used to settle the reviewer's entry a second time, as ``cancelled``, and
    relabel the block ``cancelled_by_operator`` - rewriting two recorded facts about work that had
    finished before anybody asked to stop it.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver = FakeDriver(
        project_root,
        FakeScript(
            write_plan=FAKE_WRITE_PLAN,
            review=ReviewOutput(
                verdict="changes_requested",
                findings=[{"statement": "the empty input still crashes"}],
            ),
        ),
    )
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
    outcome = controller.run_task(run_request)
    run_id = outcome.run_id
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED, outcome.block_reason
    before = store.invocations_for(run_id)
    assert [(entry.role, entry.state, entry.outcome) for entry in before] == [
        ("implementer", InvocationStartState.SETTLED, InvocationOutcome.COMPLETED),
        ("reviewer", InvocationStartState.SETTLED, InvocationOutcome.COMPLETED),
    ]
    reason_before = store.get_run(run_id)["block_reason"]

    receipt = controller.cancel(run_id)

    assert store.invocations_for(run_id) == before, "a stop of a finished run rewrote its ledger"
    row = store.get_run(run_id)
    assert (row["task_state"], row["block_code"], row["block_reason"]) == (
        TaskState.BLOCKED.value,
        RefusalCode.REVIEW_REJECTED.value,
        reason_before,
    ), "a stop must not relabel a run that already ended"
    assert store.cancel_state(run_id)[1] == receipt, "the stop itself is still recorded"
    assert any("already BLOCKED" in note for note in store.notes_for(run_id)), (
        store.notes_for(run_id)
    )


@pytest.mark.parametrize("reports_spawn", [True, False], ids=["reporting", "silent"])
def test_a_stop_during_the_checks_leaves_the_settled_implementer_entry_as_it_was(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reports_spawn: bool,
) -> None:
    """The implementer's entry was settled ``completed`` before the stop; the stop cannot reopen it.

    With a driver that reports its spawn the entry carries ``started_at``, and the stop used to
    settle it again as ``cancelled``. With a driver that reports nothing the entry is ``settled``
    with a launch request and no ``started_at``, and the stop used to hand it to
    ``mark_launch_unresolved`` - which raised out of ``cancel`` in the middle of the checks.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    driver_type = FakeDriver if reports_spawn else SilentSpawnDriver
    driver = driver_type(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
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
    settled_before_the_stop: list[list] = []
    original = controller_module.verify_candidate

    def verify_then_stop(**kwargs):  # noqa: ANN003, ANN201 - the verifier's own shape
        verification = original(**kwargs)
        settled_before_the_stop.append(store.invocations_for(kwargs["run_id"]))
        controller.cancel(kwargs["run_id"])
        return verification

    monkeypatch.setattr(controller_module, "verify_candidate", verify_then_stop)
    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.CANCELLED_BY_OPERATOR, outcome.block_reason
    (before,) = settled_before_the_stop
    assert [(entry.role, entry.state, entry.outcome) for entry in before] == [
        ("implementer", InvocationStartState.SETTLED, InvocationOutcome.COMPLETED)
    ]
    assert store.invocations_for(outcome.run_id) == before, "the stop rewrote a settled entry"
    assert [request.role for request in driver.started] == ["implementer"], (
        "no review may be bought after the stop"
    )


def test_a_ledger_failure_during_a_confirmed_stop_is_a_note_on_a_stopped_run(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's terminal state does not depend on the ledger write that follows it.

    The stop is confirmed while the implementer is inside its driver - launch requested, no spawn
    report yet - and recording that entry as an unresolved launch is made to fail. That failure
    used to escape ``cancel`` before the run was blocked. The block is now written first and the
    failure becomes a note; the entry stays open, so the root stays blocked, which is the safe
    reading of a ledger write that did not happen.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles={"implementer"})
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    def failing_ledger_write(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise StoreError("injected ledger failure")

    with RunningTask(controller, run_request) as running:
        assert implementer.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        monkeypatch.setattr(store, "mark_launch_unresolved", failing_ledger_write)
        receipt = controller.cancel(run_id)
        row = store.get_run(run_id)
        (entry,) = store.invocations_for(run_id)
        implementer.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    assert receipt.status == "confirmed_stopped"
    assert (row["task_state"], row["block_code"]) == (
        TaskState.BLOCKED.value,
        RefusalCode.CANCELLED_BY_OPERATOR.value,
    ), "the stop must end the run whatever the ledger bookkeeping does"
    assert entry.state is InvocationStartState.REQUESTED and entry.pending, entry
    assert any("injected ledger failure" in note for note in store.notes_for(run_id)), (
        store.notes_for(run_id)
    )


def test_the_ledger_failure_note_of_a_stop_promises_no_reconcile_that_cannot_happen(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note left by a failed ledger write must describe a way out that exists.

    The run is ``BLOCKED/cancelled_by_operator``, and ``resume`` reconciles only an
    ``outcome_unknown`` run, so a note saying the open entry blocks the root "until an operator
    reconciles it" sent an operator to a command that does nothing for this run.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles={"implementer"})
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    def failing_ledger_write(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise StoreError("injected ledger failure")

    with RunningTask(controller, run_request) as running:
        assert implementer.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        monkeypatch.setattr(store, "mark_launch_unresolved", failing_ledger_write)
        controller.cancel(run_id)
        implementer.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    (note,) = [n for n in store.notes_for(run_id) if "injected ledger failure" in n]
    resumed = controller.resume(run_id)
    assert resumed.block_code is RefusalCode.CANCELLED_BY_OPERATOR, "resume reconciles nothing here"
    assert "operator reconciles" not in note, note
    assert "no command" in note and "cancelled_by_operator" in note, note


@pytest.mark.parametrize("stopper", ["owner", "observer"])
def test_a_cancel_that_fails_before_the_block_is_finished_by_the_next_cancel(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stopper: str,
) -> None:
    """The receipt is a stop's idempotency key, so it must never exist without the block.

    The first ``cancel`` fails on its terminal write (a cross-process ``database is locked``).
    The receipt used to be committed already, so every later ``cancel`` returned it without
    writing a block: the run stayed ``RUNNING`` for good, its owner could no longer block it (the
    intent refuses that), ``resume`` was a no-op for it, and its root was owned for ever. A retry
    of ``cancel`` must still end the run - confirmed by the owner, unknown from an observer that
    holds no handle, which ``resume`` then reconciles.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles={"implementer"})
    controller = _root_controller(
        store,
        implementer,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    observer_driver = FakeDriver(project_root)
    stopping = (
        controller
        if stopper == "owner"
        else Controller(store, observer_driver, reviewer_driver=observer_driver,
                        controller_build="observer", controller_id="observer-process")
    )
    failures: list[str] = []

    def fail_first_terminal_write(name: str) -> None:
        original = getattr(store, name, None)

        def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - the store's own shape
            if not failures:
                failures.append(name)
                raise sqlite3.OperationalError("database is locked")
            return original(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapper, raising=False)

    with RunningTask(controller, run_request) as running:
        assert implementer.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        # Whichever write records the stop's terminal state fails once: the separate block of
        # the old order, or the one statement that records the receipt and the block together.
        fail_first_terminal_write("set_blocked")
        fail_first_terminal_write("record_cancel_outcome")
        with pytest.raises(sqlite3.OperationalError):
            stopping.cancel(run_id)
        assert failures, "the injected failure never fired"
        receipt = stopping.cancel(run_id)
        row = store.get_run(run_id)
        implementer.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    expected = (
        RefusalCode.CANCELLED_BY_OPERATOR if stopper == "owner" else RefusalCode.OUTCOME_UNKNOWN
    )
    assert receipt.status == ("confirmed_stopped" if stopper == "owner" else "unknown"), receipt
    assert (row["task_state"], row["block_code"]) == (TaskState.BLOCKED.value, expected.value), (
        "a retried cancel must end the run"
    )
    final = store.get_run(run_id)
    assert (final["task_state"], final["block_code"]) == (TaskState.BLOCKED.value, expected.value)
    assert store.cancel_state(run_id)[1] == receipt
    if stopper == "observer":
        (entry,) = store.invocations_for(run_id)
        resumed = stopping.resume(run_id)
        assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN
        assert observer_driver.reconciled == [entry.invocation_id], "resume must reconcile it"


@pytest.mark.parametrize("status", ["confirmed_stopped", "unknown"])
def test_a_receipt_recorded_without_its_block_is_completed_by_the_next_cancel(
    store: Store,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    status: str,
) -> None:
    """A database written by the old order can hold a receipt on a run that is still live.

    The next ``cancel`` must not return that receipt as if the stop were finished: it re-applies
    the block the receipt implies (and, for a confirmed stop, the attempt and ledger bookkeeping
    of one) instead of asking the driver again.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = GatedDriver(project_root, label="implementer", gated_roles={"implementer"})
    controller = _root_controller(
        store,
        implementer,
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
        attempt = store.open_attempt(run_id)
        stale = CancellationReceipt(
            invocation_id=str(attempt["invocation_id"]),
            status=status,  # type: ignore[arg-type]
            mechanism="forced" if status == "confirmed_stopped" else "none",
            local_process_stopped=True if status == "confirmed_stopped" else None,
            detail="recorded by an earlier cancel that never wrote its block",
        )
        store.record_cancel_intent(run_id)
        store.record_cancel_receipt(run_id, stale)
        assert store.get_run(run_id)["task_state"] == TaskState.RUNNING.value

        receipt = controller.cancel(run_id)
        row = store.get_run(run_id)
        attempt_after = store.open_attempt(run_id)
        implementer.released.set()

    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    assert receipt == stale, "the recorded receipt is the stop's answer"
    assert implementer.cancel_calls == [], "the driver is not asked again"
    expected = (
        RefusalCode.CANCELLED_BY_OPERATOR
        if status == "confirmed_stopped"
        else RefusalCode.OUTCOME_UNKNOWN
    )
    assert (row["task_state"], row["block_code"]) == (TaskState.BLOCKED.value, expected.value)
    if status == "confirmed_stopped":
        assert attempt_after["state"] == AttemptState.CANCELLED.value
    else:
        assert attempt_after["state"] == AttemptState.ACTIVE.value, "work may still be running"
    assert any("re-applied" in note for note in store.notes_for(run_id)), store.notes_for(run_id)


@pytest.mark.parametrize("created", [True, False], ids=["launched", "not-launched"])
def test_a_late_spawn_report_never_reopens_a_launch_closed_as_unknown(
    store: Store, project, task_spec: TaskSpec, project_root: Path, created: bool
) -> None:
    """A spawn report that arrives after ``launch_unknown`` must not make the entry settleable.

    ``launch_unknown`` is closed only by an operator's reconcile (or a confirmed stop): a late
    ``created=True`` used to turn it back into ``started`` - an *open* state - so the next
    settlement closed it and released the root that the reconcile had kept blocked. The launch
    it reports is still a fact, so it is recorded, and the entry becomes ``unknown``: exactly
    what the reconcile would have recorded had the report come first. A late ``created=False``
    is the same commutation in the other direction - the reconcile leaves a ``not_started``
    entry alone - so the driver's own word that nothing was launched stands.
    """
    from hflow.contracts import SpawnFact

    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    run_id = _seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    _reserve(store, run_id=run_id, binding=binding, limits=limits, role="implementer",
             invocation_id="I-late")
    store.mark_invocation_launch_requested("I-late")
    assert store.mark_unsettled_invocations_unknown(run_id, "reconciled by an operator") == 1
    assert store.invocation("I-late").state is InvocationStartState.LAUNCH_UNKNOWN

    store.record_invocation_spawn(
        SpawnFact(
            invocation_id="I-late",
            created=created,
            pid=4321 if created else None,
            spawn_kind=SpawnKind.PROCESS if created else SpawnKind.UNKNOWN,
            detail="late spawn report",
        )
    )
    entry = store.invocation("I-late")
    if not created:
        assert entry.state is InvocationStartState.NOT_STARTED, entry
        return
    assert entry.state is InvocationStartState.UNKNOWN, entry
    assert entry.started_at is not None and entry.process_pid == 4321, "the launch is recorded"
    assert entry.pending is True
    assert store.settle_invocation("I-late", outcome=InvocationOutcome.COMPLETED) is False
    assert store.invocation("I-late").state is InvocationStartState.UNKNOWN
    assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == ["I-late"]


class RefusingDriver(FakeDriver):
    """A role driver that refuses deterministically before it creates anything.

    It reports no spawn fact on purpose, so what the ledger says afterwards is the controller's
    mapping of the refusal and nothing the driver wrote itself.
    """

    def __init__(self, project_root: Path, *, refused_roles: set[str]) -> None:
        super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
        self.refused_roles = refused_roles

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        if request.role in self.refused_roles:
            raise RefusedError(
                RefusalCode.WORKSPACE_CLIENT_CONFIG,
                ".acpxrc.json exists in the workspace; remove the file and submit again",
            )
        return super().start(request)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_driver_refusal_before_launch_blocks_with_its_own_code_and_starts_nothing(
    store: Store,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    role: str,
) -> None:
    """A deterministic driver refusal (a workspace client config) is not a review protocol error.

    For either role the run blocks with the driver's code, and the refused reservation is
    recorded as never started - its allowance stays consumed, but it does not sit in the ledger
    as an unconfirmed launch that blocks the root.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    implementer = RefusingDriver(project_root, refused_roles={role} & {"implementer"})
    reviewer = RefusingDriver(project_root, refused_roles={role} & {"reviewer"})
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

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG, outcome.block_reason
    assert outcome.receipt is None
    states = {entry.role: entry.state for entry in store.invocations_for(outcome.run_id)}
    assert states[role] is InvocationStartState.NOT_STARTED, states


@pytest.mark.parametrize("max_repairs", [0, 1])
def test_after_a_launch_time_refusal_only_a_new_revision_with_a_repair_left_runs_again(
    store: Store,
    task_spec: TaskSpec,
    project_root: Path,
    run_request: RunRequest,
    tmp_path: Path,
    max_repairs: int,
) -> None:
    """What the driver's refusal message tells the operator is the step that actually works.

    The dispatch was reserved before the driver refused, so: the identical TaskSpec is a history
    lookup that returns the blocked run; a new revision is a new run, and on the same root its
    first implementer is charged as a repair - it runs only while the root has one left.
    """
    binding = _binding(store, task_spec, project_root)
    limits = _limits(max_repairs=max_repairs)
    driver = RefusingDriver(project_root, refused_roles={"implementer"})
    first = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    ).run_task(run_request)
    assert first.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG, first.block_reason

    driver.refused_roles = set()  # the operator removed the file
    again = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    ).run_task(run_request)
    assert again.run_id == first.run_id
    assert again.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG
    assert any("identical TaskSpec" in note for note in again.notes), again.notes

    revised = task_spec.model_copy(update={"revision": 2})
    second = _root_controller(
        store,
        driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=revised,
            binding=binding,
            limits=limits,
            project_root=project_root,
            authorization_id="AUTH-revision-2",
        ),
        data_dir=tmp_path / "data",
    ).run_task(run_request.model_copy(update={"task": revised}))

    assert second.run_id != first.run_id
    if max_repairs == 0:
        assert second.block_code is RefusalCode.BUDGET_EXHAUSTED, second.block_reason
        assert "repair" in (second.block_reason or "")
    else:
        assert second.block_code is not RefusalCode.BUDGET_EXHAUSTED, second.block_reason
        implementers = [
            entry for entry in store.invocations_for(second.run_id) if entry.role == "implementer"
        ]
        assert implementers and implementers[0].is_repair is True, implementers
        assert implementers[0].state is not InvocationStartState.NOT_STARTED
