"""Batch I1: ``hflow resume`` closes the ledger entries an ended run left open.

An entry left ``reserved``/``requested``/``started`` on a run that ended for some other reason than
``outcome_unknown``/``owner_lost`` used to block its root for good: ``resume`` reconciled only those
two block codes and ``hflow ledger settle`` refuses an open entry. This file pins the closure and
everything it must not do:

* the three producers the plan names - an implementer driver that raised without a spawn fact
  (``internal_error``), a reviewer driver that raised (``review_protocol_error``), a confirmed stop
  whose ledger write failed (``cancelled_by_operator``) - plus a settlement the store refused on a
  run that was then ``ACCEPTED``, each closed by ``resume`` and then settled by
  ``hflow ledger settle``;
* the owner rule is the one ``ledger settle`` applies: an owner that may be alive (this very
  controller, or another live one) refuses and writes nothing; a pre-v6 run with a recorded
  controller pid is refused (``resume`` takes no attestation); a pre-v6 run that recorded no
  controller passes;
* a confirmed stop that ended the run closes its entry from that stop fact, exactly as the stop
  would have (a still-``reserved`` target becomes ``not_started`` and releases the root); a stop
  that did not end the run - recorded after it ended, or on a run blocked for another reason - is
  never used; everything else becomes ``unknown`` / ``launch_unknown``;
* the run's task state, block code, receipt and outcome never change, no attempt or invocation
  row is added, no driver is called, nothing is refunded, and a second ``resume`` is a no-op;
* overlapping resumes over the same open snapshot: nothing is closed twice, a call that changed
  nothing writes nothing, and each note describes only its own call's closures;
* ``status`` says, in one line, what closes such entries: ``resume`` once the owner is gone,
  ``resume``'s reconcile for an ``outcome_unknown`` / ``owner_lost`` run, or nothing in this build
  for a pre-v6 run that recorded a controller pid.

Offline only: ``FakeDriver`` subclasses, ``FakeCheckRunner``, a real ``Store`` under ``tmp_path``.
A dead owner is simulated the way the owner-lease tests do it: the controller releases its lock
(``close``) and the run's recorded owner identity is replaced by that of a short Python child
that has exited - this test process cannot exit itself.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import (
    CancellationReceipt,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RunRequest,
    SpawnFact,
    SpawnKind,
    TaskSpec,
    TaskState,
)
from hflow.controller import NOTE_DISPATCH, Controller, assess_run_owner, inspect_run
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.report import status_text
from hflow.store import Store, StoreError

from .test_batch_e_dispatch import (
    FAKE_WRITE_PLAN,
    RunningTask,
    _authorization,
    _binding,
    _limits,
    _root_controller,
)
from .test_batch_e_ledger import (
    _binding as _ledger_binding,
)
from .test_batch_e_ledger import (
    _register_authorization,
    _reserve,
    _seed_ready_run,
)
from .test_owner_lease import _controller, _dead_identity, windows_only

ATTEST = "Checked the provider console: no request from this launch appears."


# --------------------------------------------------------------------------
# drivers
# --------------------------------------------------------------------------


class RaisingDriver(FakeDriver):
    """A driver whose ``start`` raises before it reports anything: no spawn fact, no result."""

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        self.started.append(request)
        raise RuntimeError("the transport broke before it reported a spawn fact")


class HeldDriver(FakeDriver):
    """An implementer held inside ``start`` until the test releases it, then stopped.

    ``report_launch`` decides what the ledger knows when the stop arrives: a reported launch
    (``started``) or only a launch request (``requested``). After release the driver answers like
    the fake does for a recorded stop, but silently (no second spawn report), so the late result
    leaves the entry as the stop left it.
    """

    def __init__(self, project_root: Path, *, report_launch: bool) -> None:
        super().__init__(project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, agent_turns=1))
        self.report_launch = report_launch
        self.entered = threading.Event()
        self.released = threading.Event()

    def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
        if self.report_launch and request.on_spawn is not None:
            request.on_spawn(
                SpawnFact(
                    invocation_id=request.invocation_id,
                    created=True,
                    pid=None,
                    spawn_kind=SpawnKind.NO_PROCESS,
                    detail="held test driver: launched, no child process",
                )
            )
        self.entered.set()
        assert self.released.wait(timeout=30), "the held start was never released"
        return super().start(request.model_copy(update={"on_spawn": None}))

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancelled.append(invocation_id)
        return CancellationReceipt(
            invocation_id=invocation_id,
            status="confirmed_stopped",
            mechanism="forced",
            local_process_stopped=True,
            detail="held test driver: the stop reached the invocation",
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _root_run_controller(
    store: Store,
    task_spec: TaskSpec,
    project_root: Path,
    tmp_path: Path,
    driver: FakeDriver,
    *,
    reviewer_driver: FakeDriver | None = None,
) -> tuple[Controller, str]:
    binding = _binding(store, task_spec, project_root)
    limits = _limits()
    controller = _root_controller(
        store,
        driver,
        reviewer_driver=reviewer_driver,
        binding=binding,
        limits=limits,
        authorization=_authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    return controller, binding.root_id


def _owner_exits(store: Store, controller: Controller, run_id: str) -> None:
    """The run's owning controller is gone: its lock released, its recorded identity exited."""
    controller.close()
    dead = _dead_identity()
    with store.transaction() as conn:
        updated = conn.execute(
            "UPDATE runs SET owner_pid = ?, owner_created = ?, owner_host = ?"
            " WHERE run_id = ? AND owner_token = ?",
            (dead.pid, dead.created, dead.host, run_id, controller.owner_token),
        ).rowcount
    assert updated == 1
    assert assess_run_owner(store, store.get_run(run_id)).gone


def _frozen(store: Store, run_id: str) -> dict[str, object]:
    """Everything ``resume`` must leave alone: the run's outcome and every row it owns."""
    row = store.get_run(run_id)
    return {
        "run": {
            key: row[key]
            for key in (
                "task_state", "phase", "delivery_state", "block_code", "block_reason",
                "receipt_json", "cancel_intent_at", "cancel_receipt_json", "turns_reserved",
                "turns_observed", "owner_token", "claim_generation",
            )
        },
        "attempts": [
            (a["attempt_id"], a["state"], a["outcome"], a["block_code"])
            for a in store.attempts_for(run_id)
        ],
        "invocation_ids": [entry.invocation_id for entry in store.invocations_for(run_id)],
    }


def _states(store: Store, run_id: str) -> dict[str, InvocationStartState]:
    return {entry.invocation_id: entry.state for entry in store.invocations_for(run_id)}


def _cli_settle(store: Store, invocation_id: str, settled_as: str = "consumed") -> int:
    return main([
        "--data-dir", str(store.path.parent), "ledger", "settle", invocation_id,
        "--as", settled_as, "--attest", ATTEST,
    ])


def _no_driver_call(controller: Controller) -> None:
    for driver in {id(controller.driver): controller.driver,
                   id(controller.reviewer_driver): controller.reviewer_driver}.values():
        assert driver.started == [], "resume never dispatches"  # type: ignore[attr-defined]
        assert driver.reconciled == [] and driver.cancelled == [], (  # type: ignore[attr-defined]
            "an ended run's owner is gone with its handles: no driver is asked anything"
        )


# --------------------------------------------------------------------------
# 1. the implementer driver raised without a spawn fact (internal_error)
# --------------------------------------------------------------------------


@windows_only
def test_a_driver_failure_entry_is_closed_by_resume_and_then_voided_by_ledger_settle(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, fake_script: FakeScript, capsys: pytest.CaptureFixture[str],
) -> None:
    owner, root_id = _root_run_controller(
        store, task_spec, project_root, tmp_path, RaisingDriver(project_root)
    )
    outcome = owner.run_task(run_request)
    run_id = outcome.run_id
    assert (outcome.task_state, outcome.block_code) == (
        TaskState.BLOCKED, RefusalCode.INTERNAL_ERROR,
    ), outcome.block_reason
    (entry,) = store.invocations_for(run_id)
    assert entry.state is InvocationStartState.REQUESTED and entry.started_at is None
    (failure,) = [n for n in store.notes_for(run_id) if "without reporting a spawn fact" in n]
    assert f"`hflow resume {run_id}`" in failure and "hflow ledger settle" in failure, failure
    assert "no command" not in failure, failure
    assert "open entries  1 ledger entry left open" in status_text(inspect_run(store, run_id))
    assert f"`hflow resume {run_id}` closes" in status_text(inspect_run(store, run_id))

    # ledger settle still refuses an open entry, pointing at resume.
    capsys.readouterr()
    assert _cli_settle(store, entry.invocation_id, "void") == EXIT_REFUSED
    assert "still open" in capsys.readouterr().err

    # The owner itself is alive: its own resume closes nothing and writes nothing.
    frozen, notes = _frozen(store, run_id), store.notes_for(run_id)
    alive = owner.resume(run_id)
    assert alive.block_code is RefusalCode.INTERNAL_ERROR
    assert any("this controller owns the run" in note and "Nothing was written" in note
               for note in alive.notes), alive.notes
    assert _frozen(store, run_id) == frozen and store.notes_for(run_id) == notes
    assert _states(store, run_id) == {entry.invocation_id: InvocationStartState.REQUESTED}

    _owner_exits(store, owner, run_id)
    successor = _controller(store, project_root, fake_script)
    resumed = successor.resume(run_id)

    assert (resumed.task_state, resumed.block_code) == (
        TaskState.BLOCKED, RefusalCode.INTERNAL_ERROR,
    ), "the run keeps the outcome it ended with"
    assert _frozen(store, run_id) == frozen, "state, block, receipt, attempts and rows unchanged"
    _no_driver_call(successor)
    closed = store.invocation(entry.invocation_id)
    assert closed is not None and closed.state is InvocationStartState.LAUNCH_UNKNOWN
    assert "closed by `hflow resume`" in closed.detail and "proven gone" in closed.detail
    assert closed.pending, "launch_unknown still blocks the root until an operator settles it"
    assert [e.invocation_id for e in store.pending_invocations(root_id)] == [entry.invocation_id]
    (note,) = [n for n in store.notes_for(run_id) if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    assert note in resumed.notes
    assert f"{entry.invocation_id} requested->launch_unknown" in note, note
    assert "Nothing was re-dispatched or refunded" in note and "unchanged" in note
    assert "`hflow ledger settle <invocation_id>`" in note
    assert "open entries" not in status_text(inspect_run(store, run_id))

    # A second resume finds nothing open: the old no-op, nothing written.
    notes = store.notes_for(run_id)
    again = _controller(store, project_root, fake_script).resume(run_id)
    assert any("no-op" in n for n in again.notes), again.notes
    assert store.notes_for(run_id) == notes and _frozen(store, run_id) == frozen
    assert _states(store, run_id) == {entry.invocation_id: InvocationStartState.LAUNCH_UNKNOWN}

    # ...and ledger settle now closes it; void returns exactly this dispatch's root charge.
    used = store.root_budget_view(root_id).used_top_level_submissions
    capsys.readouterr()
    assert _cli_settle(store, entry.invocation_id, "void") == EXIT_OK, capsys.readouterr().err
    assert store.invocation(entry.invocation_id).state is InvocationStartState.OPERATOR_SETTLED
    assert store.root_budget_view(root_id).used_top_level_submissions == used - 1
    assert store.pending_invocations(root_id) == []
    assert _frozen(store, run_id) == frozen


@windows_only
def test_a_live_second_owner_refuses_and_writes_nothing(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, fake_script: FakeScript,
) -> None:
    owner, _root = _root_run_controller(
        store, task_spec, project_root, tmp_path, RaisingDriver(project_root)
    )
    run_id = owner.run_task(run_request).run_id
    frozen, notes, states = _frozen(store, run_id), store.notes_for(run_id), _states(store, run_id)

    other = _controller(store, project_root, fake_script)
    refused = other.resume(run_id)

    note = " ".join(refused.notes)
    assert "may still be alive" in note and "lock=held" in note, note
    assert "Nothing was written" in note and f"`hflow resume {run_id}` again" in note
    assert _frozen(store, run_id) == frozen
    assert store.notes_for(run_id) == notes and _states(store, run_id) == states
    _no_driver_call(other)
    owner.close()


# --------------------------------------------------------------------------
# 2. the reviewer driver raised (review_protocol_error), through the CLI
# --------------------------------------------------------------------------


@windows_only
def test_a_reviewer_driver_failure_entry_is_closed_by_cli_resume(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, fake_script: FakeScript, capsys: pytest.CaptureFixture[str],
) -> None:
    owner, root_id = _root_run_controller(
        store, task_spec, project_root, tmp_path, FakeDriver(project_root, fake_script),
        reviewer_driver=RaisingDriver(project_root),
    )
    outcome = owner.run_task(run_request)
    run_id = outcome.run_id
    assert outcome.block_code is RefusalCode.REVIEW_PROTOCOL_ERROR, outcome.block_reason
    implementer, reviewer = store.invocations_for(run_id)
    assert implementer.state is InvocationStartState.SETTLED
    assert reviewer.role == "reviewer" and reviewer.state is InvocationStartState.REQUESTED
    frozen = _frozen(store, run_id)

    _owner_exits(store, owner, run_id)
    capsys.readouterr()
    code = main([
        "resume", run_id, "--project-root", str(project_root),
        "--data-dir", str(store.path.parent),
    ])
    out = capsys.readouterr().out

    assert code == EXIT_BLOCKED, out
    assert "resume closed the 1 ledger entry" in out, out
    assert _frozen(store, run_id) == frozen
    assert _states(store, run_id) == {
        implementer.invocation_id: InvocationStartState.SETTLED,
        reviewer.invocation_id: InvocationStartState.LAUNCH_UNKNOWN,
    }, "a settled entry is never reopened or relabelled"
    assert _cli_settle(store, reviewer.invocation_id) == EXIT_OK, capsys.readouterr().err
    assert store.pending_invocations(root_id) == []
    assert store.get_run(run_id)["block_code"] == RefusalCode.REVIEW_PROTOCOL_ERROR.value


# --------------------------------------------------------------------------
# 3. a confirmed stop whose ledger write failed (cancelled_by_operator)
# --------------------------------------------------------------------------


@windows_only
@pytest.mark.parametrize("launched", [False, True], ids=["requested", "started"])
def test_a_confirmed_stop_whose_ledger_write_failed_is_closed_from_the_stop_fact(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, fake_script: FakeScript, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], launched: bool,
) -> None:
    driver = HeldDriver(project_root, report_launch=launched)
    owner, root_id = _root_run_controller(store, task_spec, project_root, tmp_path, driver)
    # The write the confirmed stop makes for each shape: settle a launch, or record an
    # unresolved one. It fails once; every later call is the real one.
    failing_name = "settle_invocation" if launched else "mark_launch_unresolved"
    real = getattr(store, failing_name)
    failures: list[str] = []

    def fail_once(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if not failures:
            failures.append(failing_name)
            raise StoreError("injected ledger failure")
        return real(*args, **kwargs)

    with RunningTask(owner, run_request) as running:
        assert driver.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        monkeypatch.setattr(store, failing_name, fail_once)
        receipt = owner.cancel(run_id)
        monkeypatch.setattr(store, failing_name, real)
        driver.released.set()
    assert running.error is None, f"the run raised instead of stopping: {running.error!r}"
    assert failures == [failing_name]

    assert receipt.status == "confirmed_stopped" and not receipt.run_already_ended
    (entry,) = store.invocations_for(run_id)
    assert receipt.invocation_id == entry.invocation_id
    expected_open = InvocationStartState.STARTED if launched else InvocationStartState.REQUESTED
    assert entry.state is expected_open, "the failed write left the entry open"
    (failure,) = [n for n in store.notes_for(run_id) if "injected ledger failure" in n]
    assert f"`hflow resume {run_id}` closes it from this recorded stop" in failure, failure
    assert "no command" not in failure and "cancelled_by_operator" in failure
    frozen = _frozen(store, run_id)
    assert frozen["run"]["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value  # type: ignore[index]

    _owner_exits(store, owner, run_id)
    successor = _controller(store, project_root, fake_script)
    resumed = successor.resume(run_id)

    assert resumed.block_code is RefusalCode.CANCELLED_BY_OPERATOR
    assert _frozen(store, run_id) == frozen
    _no_driver_call(successor)
    closed = store.invocation(entry.invocation_id)
    assert closed is not None
    note = " ".join(resumed.notes)
    assert "closed from the run's recorded confirmed stop (forced)" in note, note
    if launched:
        # The stop fact says the launch happened and was stopped: settled as cancelled, exactly
        # what the confirmed stop records when its write succeeds. The root is released.
        assert closed.state is InvocationStartState.SETTLED
        assert closed.outcome is InvocationOutcome.CANCELLED
        assert closed.detail == f"stop confirmed (forced) for run {run_id}"
        assert store.pending_invocations(root_id) == []
        capsys.readouterr()
        assert _cli_settle(store, entry.invocation_id) == EXIT_REFUSED
        assert "already records its final fact" in capsys.readouterr().err
    else:
        # A launch was requested and never reported: the stop fact cannot say whether a process
        # existed, so the entry is launch_unknown - the stop's own wording, not the reconcile's.
        assert closed.state is InvocationStartState.LAUNCH_UNKNOWN
        assert closed.detail.startswith("a stop was confirmed (forced)"), closed.detail
        assert _cli_settle(store, entry.invocation_id, "void") == EXIT_OK, (
            capsys.readouterr().err
        )
        assert store.pending_invocations(root_id) == []


@pytest.mark.parametrize(
    "block",
    [RefusalCode.INTERNAL_ERROR, RefusalCode.CANCELLED_BY_OPERATOR],
    ids=["driver-failure", "cancelled-block"],
)
def test_a_stop_of_a_run_that_had_already_ended_is_not_used_as_the_entrys_fact(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    block: RefusalCode,
) -> None:
    """A stop recorded after the run ended says the child was stopped, not what it did before.

    So it does not settle the entry - whatever the run's block code - and the entry becomes
    ``unknown`` like any other unobserved launch, for an operator to decide. (A run with no owner
    and no controller recorded, so the owner half passes on any platform.)
    """
    binding = _ledger_binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    limits = _limits()
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    run_id = _seed_ready_run(store, run_id="R-late-stop", project_id=project.project_id,
                             spec=task_spec)
    assert _reserve(store, run_id=run_id, invocation_id="I-late-stop", binding=binding,
                    limits=limits).is_new
    assert store.mark_invocation_started("I-late-stop") is True
    store.set_blocked(run_id, block, "seeded: the run ended before the stop")
    store.record_cancel_receipt(
        run_id,
        CancellationReceipt(
            invocation_id="I-late-stop", status="confirmed_stopped", mechanism="forced",
            local_process_stopped=True, detail="stopped after the run ended",
            run_already_ended=True,
        ),
    )

    resumed = _controller(store, project_root, fake_script).resume(run_id)

    entry = store.invocation("I-late-stop")
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN, entry
    assert "recorded confirmed stop" not in " ".join(resumed.notes)
    assert resumed.block_code is block


def _seed_ended_entry(
    store: Store, project, task_spec: TaskSpec, project_root: Path, *, run_id: str,
    invocation_id: str, shape: str, block: RefusalCode,
    stop: CancellationReceipt | None = None,
) -> str:
    """A run that ended ``block`` with one ledger entry left ``shape``; returns the root id.

    ``shape`` is ``reserved`` (no driver was asked), ``requested`` (asked, nothing reported) or
    ``started`` (a launch recorded). ``stop`` is recorded as the run's cancel receipt. A
    label-only claim that recorded no controller pid, so the owner half passes on any platform.
    """
    binding = _ledger_binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    limits = _limits()
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    _seed_ready_run(store, run_id=run_id, project_id=project.project_id, spec=task_spec)
    assert _reserve(store, run_id=run_id, invocation_id=invocation_id, binding=binding,
                    limits=limits).is_new
    if shape in {"requested", "started"}:
        assert store.mark_invocation_launch_requested(invocation_id) is True
    if shape == "started":
        assert store.mark_invocation_started(invocation_id, process_created=False) is True
    store.set_blocked(run_id, block, f"seeded: the run ended {block.value}")
    if stop is not None:
        store.record_cancel_receipt(run_id, stop)
    entry = store.invocation(invocation_id)
    assert entry is not None and entry.state.value == shape, entry
    assert store.get_run(run_id)["owner_token"] is None
    return binding.root_id


def _confirmed_stop(invocation_id: str) -> CancellationReceipt:
    """The receipt a confirmed stop that ended a live run records (``run_already_ended`` unset)."""
    return CancellationReceipt(
        invocation_id=invocation_id, status="confirmed_stopped", mechanism="forced",
        local_process_stopped=True, detail="seeded: the stop that ended the run",
    )


def test_a_confirmed_stop_of_a_still_reserved_entry_closes_it_not_started_and_frees_the_root(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
) -> None:
    """No driver was asked to launch it, so the stop fact makes ``not_started`` a fact.

    That is exactly what the stop records when its own write succeeds - and, unlike
    ``launch_unknown``, it no longer blocks the root: nothing is left for an operator to settle.
    """
    root_id = _seed_ended_entry(
        store, project, task_spec, project_root, run_id="R-stop-reserved",
        invocation_id="I-stop-reserved", shape="reserved",
        block=RefusalCode.CANCELLED_BY_OPERATOR, stop=_confirmed_stop("I-stop-reserved"),
    )
    assert [e.invocation_id for e in store.pending_invocations(root_id)] == ["I-stop-reserved"]
    used = store.root_budget_view(root_id).used_top_level_submissions
    frozen = _frozen(store, "R-stop-reserved")
    successor = _controller(store, project_root, fake_script)

    resumed = successor.resume("R-stop-reserved")

    entry = store.invocation("I-stop-reserved")
    assert entry is not None and entry.state is InvocationStartState.NOT_STARTED, entry
    assert entry.detail.startswith("a confirmed stop (forced) ended run R-stop-reserved before"), (
        entry.detail
    )
    assert store.pending_invocations(root_id) == [], "a not_started entry releases the root"
    assert store.root_budget_view(root_id).used_top_level_submissions == used, "never refunded"
    assert _frozen(store, "R-stop-reserved") == frozen
    _no_driver_call(successor)
    (note,) = [n for n in store.notes_for("R-stop-reserved")
               if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    assert "resume closed the 1 ledger entry" in note, note
    assert "I-stop-reserved reserved->not_started" in note, note
    assert "I-stop-reserved was closed from the run's recorded confirmed stop (forced)" in note
    assert resumed.block_code is RefusalCode.CANCELLED_BY_OPERATOR


@pytest.mark.parametrize(
    ("shape", "expected", "stop_would_give"),
    [
        ("reserved", InvocationStartState.LAUNCH_UNKNOWN, InvocationStartState.NOT_STARTED),
        ("started", InvocationStartState.UNKNOWN, InvocationStartState.SETTLED),
    ],
    ids=["reserved", "started"],
)
def test_a_confirmed_stop_on_a_run_blocked_for_another_reason_is_not_used(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    shape: str, expected: InvocationStartState, stop_would_give: InvocationStartState,
) -> None:
    """The guard's block-code half: ``run_already_ended`` unset is not enough.

    A confirmed stop is the entry's fact only when it is the stop that ended the run - recorded
    with the ``cancelled_by_operator`` block it implies. On a run that ended ``internal_error``
    the receipt does not say what the invocation did, so the entry closes like any other
    unobserved one, and the root stays blocked for an operator.
    """
    root_id = _seed_ended_entry(
        store, project, task_spec, project_root, run_id="R-other-block",
        invocation_id="I-other-block", shape=shape, block=RefusalCode.INTERNAL_ERROR,
        stop=_confirmed_stop("I-other-block"),
    )
    _intent, receipt = store.cancel_state("R-other-block")
    assert receipt is not None and not receipt.run_already_ended
    assert receipt.status == "confirmed_stopped"

    resumed = _controller(store, project_root, fake_script).resume("R-other-block")

    entry = store.invocation("I-other-block")
    assert entry is not None and entry.state is expected, entry
    assert entry.state is not stop_would_give
    assert "closed by `hflow resume`" in entry.detail, entry.detail
    notes = " ".join(resumed.notes)
    assert "recorded confirmed stop" not in notes, notes
    assert f"I-other-block {shape}->{expected.value}" in notes, notes
    assert [e.invocation_id for e in store.pending_invocations(root_id)] == ["I-other-block"]
    assert resumed.block_code is RefusalCode.INTERNAL_ERROR


# --------------------------------------------------------------------------
# 3b. overlapping resumes over the same open snapshot
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "closed_as"),
    [
        ("reserved", InvocationStartState.NOT_STARTED),
        ("requested", InvocationStartState.LAUNCH_UNKNOWN),
        ("started", InvocationStartState.SETTLED),
    ],
    ids=["reserved", "requested", "started"],
)
def test_a_resume_over_a_stale_open_snapshot_writes_nothing_and_says_so(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    shape: str, closed_as: InvocationStartState,
) -> None:
    """Two resumes read the same open entry; one closes it, the other must not claim it.

    Interleaved deterministically: the open snapshot (and the run row) are read first, a whole
    ``resume`` then completes, and the closure step runs again with that stale snapshot - what a
    concurrent ``resume`` that read before the first one wrote would do. It used to write a
    second "resume closed ..." note for a closure it did not make, plus the stop's own
    self-contradictory "the confirmed stop ... left invocation ... settled" note.
    """
    run_id, invocation_id = "R-overlap", "I-overlap"
    _seed_ended_entry(
        store, project, task_spec, project_root, run_id=run_id, invocation_id=invocation_id,
        shape=shape, block=RefusalCode.CANCELLED_BY_OPERATOR,
        stop=_confirmed_stop(invocation_id),
    )
    stale_row = store.get_run(run_id)
    stale_open = store.invocations_for(run_id)
    assert [e.state.value for e in stale_open] == [shape]
    frozen = _frozen(store, run_id)

    first = _controller(store, project_root, fake_script).resume(run_id)
    after_first = store.invocation(invocation_id)
    assert after_first is not None and after_first.state is closed_as, after_first
    notes_after_first = store.notes_for(run_id)
    second_controller = _controller(store, project_root, fake_script)

    second = second_controller._close_open_entries_of_ended_run(run_id, stale_row, stale_open)

    closure_notes = [n for n in store.notes_for(run_id)
                     if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    assert len(closure_notes) == 1, closure_notes
    assert closure_notes[0] in first.notes
    assert f"{invocation_id} {shape}->{closed_as.value}" in closure_notes[0]
    assert store.notes_for(run_id) == notes_after_first, "the second call wrote nothing"
    assert not any("a stop closes only an open entry" in n for n in store.notes_for(run_id))
    (said,) = second.notes
    assert f"open when this resume read it ({invocation_id})" in said, said
    assert "it was already closed when it came to close it" in said, said
    assert "This resume closed nothing and wrote nothing" in said, said
    assert "resume closed the" not in said and "->" not in said, "no transition is claimed"
    final = store.invocation(invocation_id)
    assert final is not None and final.state is closed_as
    assert final.detail == after_first.detail, "the stop's closure is not rewritten"
    assert _frozen(store, run_id) == frozen
    _no_driver_call(second_controller)


@pytest.mark.parametrize("closer", ["concurrent", "none"], ids=["lost-race", "real-refusal"])
def test_a_stop_closure_refused_by_the_store_is_reported_only_while_the_entry_is_open(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    monkeypatch: pytest.MonkeyPatch, closer: str,
) -> None:
    """``mark_launch_unresolved`` refusing after this call's re-read has two different meanings.

    * ``lost-race``: another ``resume`` closed the entry (and an operator settled it) between
      this call's read and its write - the store refuses an entry that already records a final
      fact. That is not this call's failure: no "could not be recorded" note, no closure note;
    * ``real-refusal``: the write failed while the entry stayed open, so it is said, and the
      entry closes with the others as ``launch_unknown``.
    """
    run_id, invocation_id = "R-refused-stop", "I-refused-stop"
    _seed_ended_entry(
        store, project, task_spec, project_root, run_id=run_id, invocation_id=invocation_id,
        shape="requested", block=RefusalCode.CANCELLED_BY_OPERATOR,
        stop=_confirmed_stop(invocation_id),
    )
    real = store.mark_launch_unresolved
    calls: list[str] = []
    other: list[object] = []

    def interleave(invocation_id_: str, detail: str) -> None:
        calls.append(invocation_id_)
        if len(calls) > 1:
            return real(invocation_id_, detail)
        if closer == "concurrent":
            # The overlapping resume runs to completion here, then an operator settles the entry.
            other.append(_controller(store, project_root, fake_script).resume(run_id))
            store.settle_by_operator(
                invocation_id_, settled_as="consumed", attested_by="tester", attestation=ATTEST
            )
            return real(invocation_id_, detail)
        raise StoreError("injected refusal")

    monkeypatch.setattr(store, "mark_launch_unresolved", interleave)
    resumed = _controller(store, project_root, fake_script).resume(run_id)
    monkeypatch.setattr(store, "mark_launch_unresolved", real)

    said = " ".join(resumed.notes)
    closure_notes = [n for n in store.notes_for(run_id)
                     if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    entry = store.invocation(invocation_id)
    assert entry is not None
    if closer == "concurrent":
        assert entry.state is InvocationStartState.OPERATOR_SETTLED
        assert "could not be recorded" not in said, said
        assert "This resume closed nothing and wrote nothing" in said, said
        (only,) = closure_notes  # the overlapping resume's, describing its own closure
        assert only in other[0].notes  # type: ignore[attr-defined]
        assert f"{invocation_id} requested->launch_unknown" in only, only
        assert "closed from the run's recorded confirmed stop" in only
    else:
        assert entry.state is InvocationStartState.LAUNCH_UNKNOWN
        assert "closed by `hflow resume`" in entry.detail, "closed by step 3, not by the stop"
        assert "could not be recorded on its ledger entry (injected refusal)" in said, said
        assert "that entry is closed with the others instead" in said
        (only,) = closure_notes
        assert f"{invocation_id} requested->launch_unknown" in only, only
        assert "closed from the run's recorded confirmed stop" not in only
    assert not any("a stop closes only an open entry" in n for n in store.notes_for(run_id))


def test_overlapping_resumes_that_each_closed_part_describe_only_their_own_part(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two open entries: this call closes the stopped one, an overlapping call the other.

    The second entry is copied in by SQL - a dispatch cannot reserve it while the first blocks
    the root - which is the shape a stop target plus an unrelated unsettled entry would leave.
    The overlapping ``resume`` runs inside this call's step 3, after its stop closure: each call's
    note then claims exactly one closure, and neither presents the other's as its own.
    """
    run_id = "R-split"
    _seed_ended_entry(
        store, project, task_spec, project_root, run_id=run_id, invocation_id="I-split-stop",
        shape="requested", block=RefusalCode.CANCELLED_BY_OPERATOR,
        stop=_confirmed_stop("I-split-stop"),
    )
    with store.transaction() as conn:
        conn.execute(
            "CREATE TEMP TABLE copied AS SELECT * FROM invocations WHERE invocation_id = ?",
            ("I-split-stop",),
        )
        conn.execute(
            "UPDATE copied SET invocation_id = ?, state = ?, started_at = ?",
            ("I-split-other", InvocationStartState.STARTED.value, "2026-10-06T00:00:00Z"),
        )
        conn.execute("INSERT INTO invocations SELECT * FROM copied")
        conn.execute("DROP TABLE copied")
    assert _states(store, run_id) == {
        "I-split-stop": InvocationStartState.REQUESTED,
        "I-split-other": InvocationStartState.STARTED,
    }
    real = store.mark_unsettled_invocations_unknown
    entered: list[str] = []
    other: list[object] = []

    def overlap(run_id_: str, detail: str) -> int:
        if not entered:  # once: the overlapping resume's own step 3 is the real one
            entered.append(run_id_)
            other.append(_controller(store, project_root, fake_script).resume(run_id_))
        return real(run_id_, detail)

    monkeypatch.setattr(store, "mark_unsettled_invocations_unknown", overlap)
    mine = _controller(store, project_root, fake_script).resume(run_id)
    monkeypatch.setattr(store, "mark_unsettled_invocations_unknown", real)

    assert _states(store, run_id) == {
        "I-split-stop": InvocationStartState.LAUNCH_UNKNOWN,
        "I-split-other": InvocationStartState.UNKNOWN,
    }
    closure_notes = [n for n in store.notes_for(run_id)
                     if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    assert len(closure_notes) == 2, closure_notes
    (theirs,) = [n for n in closure_notes if n in other[0].notes]  # type: ignore[attr-defined]
    (ours,) = [n for n in closure_notes if n in mine.notes]
    # The overlapping call read only what was still open (the other entry) and closed it.
    assert "resume closed the 1 ledger entry" in theirs, theirs
    assert "I-split-other started->unknown" in theirs and "I-split-stop" not in theirs
    # This call closed the stopped entry and saw the other close under it: it claims one.
    assert "resume closed 1 of the 2 ledger entries it saw close" in ours, ours
    assert "the ledger does not record which call closed which" in ours
    assert "I-split-stop was closed from the run's recorded confirmed stop (forced)" in ours


# --------------------------------------------------------------------------
# 4. an ACCEPTED run whose reviewer settlement the store refused
# --------------------------------------------------------------------------


@windows_only
def test_an_accepted_runs_unsettled_entry_is_closed_without_touching_the_receipt(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, fake_script: FakeScript, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner, root_id = _root_run_controller(
        store, task_spec, project_root, tmp_path, FakeDriver(project_root, fake_script)
    )
    real = store.settle_invocation

    def refuse_reviewer(invocation_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
        entry = store.invocation(invocation_id)
        if entry is not None and entry.role == "reviewer":
            raise StoreError("injected settlement failure")
        return real(invocation_id, **kwargs)

    monkeypatch.setattr(store, "settle_invocation", refuse_reviewer)
    outcome = owner.run_task(run_request)
    monkeypatch.setattr(store, "settle_invocation", real)
    run_id = outcome.run_id
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    reviewer = next(e for e in store.invocations_for(run_id) if e.role == "reviewer")
    assert reviewer.state is InvocationStartState.STARTED and reviewer.started_at is not None
    (failure,) = [n for n in store.notes_for(run_id) if "injected settlement failure" in n]
    assert f"`hflow resume {run_id}`" in failure and "hflow ledger settle" in failure, failure
    frozen = _frozen(store, run_id)
    assert frozen["run"]["receipt_json"]  # type: ignore[index]

    _owner_exits(store, owner, run_id)
    capsys.readouterr()
    code = main([
        "resume", run_id, "--project-root", str(project_root),
        "--data-dir", str(store.path.parent),
    ])
    assert code == EXIT_OK, capsys.readouterr()
    assert _frozen(store, run_id) == frozen, "the receipt and the acceptance are untouched"
    closed = store.invocation(reviewer.invocation_id)
    assert closed is not None and closed.state is InvocationStartState.UNKNOWN, (
        "a recorded launch whose result never reached the ledger is unknown, never settled"
    )
    assert _cli_settle(store, reviewer.invocation_id, "void") == EXIT_REFUSED
    assert _cli_settle(store, reviewer.invocation_id) == EXIT_OK, capsys.readouterr().err
    assert store.pending_invocations(root_id) == []


# --------------------------------------------------------------------------
# 5. runs with no owner token (pre-v6, or a label-only claim)
# --------------------------------------------------------------------------


def _legacy_ended_run(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    block: RefusalCode = RefusalCode.INTERNAL_ERROR,
) -> str:
    """A label-only claimed run whose launched implementer was never settled; blocked ``block``."""
    binding = _ledger_binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    limits = _limits()
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    run_id = _seed_ready_run(store, run_id="R-legacy", project_id=project.project_id,
                             spec=task_spec)
    assert store.get_run(run_id)["owner_token"] is None
    assert _reserve(store, run_id=run_id, invocation_id="I-legacy", binding=binding,
                    limits=limits).is_new
    assert store.mark_invocation_started("I-legacy") is True
    store.set_blocked(run_id, block, f"seeded: the run ended {block.value}")
    return run_id


def _record_controller_pid(store: Store, run_id: str) -> None:
    """What a pre-v6 controller left on its attempts: a pid and a start time, never a host."""
    with store.transaction() as conn:
        conn.execute(
            "UPDATE attempts SET process_id = ?, process_started_at = ? WHERE run_id = ?",
            (4194300, "2000-01-01T00:00:00Z", run_id),
        )


def test_a_run_that_recorded_no_owner_and_no_controller_is_closed(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
) -> None:
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    frozen = _frozen(store, run_id)
    successor = _controller(store, project_root, fake_script)
    status = status_text(inspect_run(store, run_id))
    assert (
        f"`hflow resume {run_id}` closes it once the run's owner is gone, then "
        "`hflow ledger settle <invocation_id>`"
    ) in status, status

    resumed = successor.resume(run_id)

    entry = store.invocation("I-legacy")
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN
    assert _frozen(store, run_id) == frozen
    _no_driver_call(successor)
    note = " ".join(resumed.notes)
    assert "no owner and no controller process were recorded" in note, note
    assert "an inference, not an observation" in note
    assert "I-legacy started->unknown" in note


def test_a_pre_v6_run_with_a_recorded_controller_pid_is_refused_and_unchanged(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
) -> None:
    """``resume`` has no attestation flag, so a controller pid with no host is never judged.

    ``status`` says so instead of naming ``resume`` as the way out: this build has none.
    """
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)
    frozen, notes = _frozen(store, run_id), store.notes_for(run_id)
    status = status_text(inspect_run(store, run_id))
    (line,) = [item for item in status.splitlines() if item.startswith("open entries")]
    assert "1 ledger entry left open on this ended run (I-legacy)" in line, line
    assert "this build cannot close it" in line and "recorded a controller pid with no host" in line
    assert f"`hflow resume {run_id}` refuses (it takes no attestation)" in line, line
    assert "`hflow ledger settle` refuses an open entry, so it keeps blocking the root" in line
    assert "closes it once" not in line and "reconciles" not in line, line

    refused = _controller(store, project_root, fake_script).resume(run_id)

    note = " ".join(refused.notes)
    assert "carries no host" in note and "takes no attestation" in note, note
    assert "Nothing was written" in note
    assert _frozen(store, run_id) == frozen and store.notes_for(run_id) == notes
    entry = store.invocation("I-legacy")
    assert entry is not None and entry.state is InvocationStartState.STARTED
    assert status_text(inspect_run(store, run_id)) == status, "the line still holds after resume"


@pytest.mark.parametrize("legacy_pid", [False, True], ids=["no-pid", "pre-v6-pid"])
@pytest.mark.parametrize(
    "block", [RefusalCode.OUTCOME_UNKNOWN, RefusalCode.OWNER_LOST],
    ids=["outcome_unknown", "owner_lost"],
)
def test_status_names_resumes_reconcile_for_an_outcome_unknown_or_owner_lost_run(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
    block: RefusalCode, legacy_pid: bool,
) -> None:
    """Such a run is reconciled by ``resume``'s existing path, which waits on no owner.

    That path comes first in ``resume``, so it holds even for a pre-v6 run that recorded a
    controller pid - the case whose ``open entries`` line otherwise says this build cannot close it.
    """
    run_id = _legacy_ended_run(store, project, task_spec, project_root, block)
    if legacy_pid:
        _record_controller_pid(store, run_id)
    status = status_text(inspect_run(store, run_id))
    (line,) = [item for item in status.splitlines() if item.startswith("open entries")]
    assert (
        f"`hflow resume {run_id}` reconciles it - a run blocked {block.value} is reconciled "
        "without waiting on its owner - recording each as unknown or launch_unknown, then "
        "`hflow ledger settle <invocation_id>`"
    ) in line, line
    assert "cannot close" not in line and "once the run's owner is gone" not in line, line

    successor = _controller(store, project_root, fake_script)
    resumed = successor.resume(run_id)

    assert any("reconciled an interrupted attempt" in n for n in resumed.notes), resumed.notes
    entry = store.invocation("I-legacy")
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN, entry
    assert successor.driver.started == []  # type: ignore[attr-defined]
    assert "open entries" not in status_text(inspect_run(store, run_id))


def test_a_live_run_with_an_open_entry_gets_no_open_entry_line(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
) -> None:
    """The status line is for an *ended* run: a live run's open entry is in flight."""
    binding = _ledger_binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    limits = _limits()
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    run_id = _seed_ready_run(store, run_id="R-live", project_id=project.project_id,
                             spec=task_spec)
    assert _reserve(store, run_id=run_id, invocation_id="I-live", binding=binding,
                    limits=limits).is_new
    assert store.get_run(run_id)["task_state"] == TaskState.RUNNING.value
    assert "open entries" not in status_text(inspect_run(store, run_id))
    store.set_blocked(run_id, RefusalCode.INTERNAL_ERROR, "seeded")
    assert "open entries  1 ledger entry left open on this ended run (I-live)" in status_text(
        inspect_run(store, run_id)
    )
