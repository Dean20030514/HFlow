"""Batch E1 review findings: the counterexamples from both review rounds, as regression tests.

These are the review's reproductions, kept in the formal suite rather than in a scratch probe,
because each one is a property E1 claims and a reader deserves to see asserted.

Round one:

1. a root is owned exclusively by its active run, including while the run is ``CHECKING`` - a
   settled invocation is not a finished run;
2. a reviewer cannot be bought twice for one candidate by presenting a new invocation id, whether
   or not the first review is still unresolved;
3. a root that cannot afford the run's *whole remaining loop* refuses the implementer instead of
   buying half a loop and blocking at the review turn;
4. "a launch was requested" and "a launch happened" are different recorded facts, and the ledger
   takes the second from the driver rather than assuming it from the first;
5. a ``reserved`` invocation that was closed as unresolved is not thereby counted as started.

Round two:

6. a record's ``origin`` survives registration, and re-registering the same id with a different
   origin is refused;
7. an unknown result with no spawn report does not become a process;
8. a failed spawn *record* followed by a real forced stop of a real child never yields
   ``not_started`` - an empty timestamp is not evidence that nothing was launched;
9. the offline driver's two settled invocations are not two processes, and a v2 terminal row does
   not promote its request timestamp to a launch fact.

Round three:

10. reconciling first and then confirming a stop is idempotent: the second settlement of the same
    unresolved launch must not raise, because the run really was stopped;
11. a database already stamped v3 still gets its terminal rows repaired, since the corrected v3
    step can never run again for that file.

The production-driver cases (4 and 8) drive the real ``AcpxDshDriver`` against the checked-in
client and agent stand-ins - a production driver with a Python stand-in client, **not** the
installed pinned acpx - and inject the stop exactly inside the driver's own handoff. Nothing here
reaches a model.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from hflow.contracts import (
    InvocationOutcome,
    InvocationStartState,
    ProjectConfig,
    RefusalCode,
    RunRequest,
    SpawnFact,
    SpawnKind,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.drivers.fake import FakeDriver
from hflow.migrate import migrate
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner
from tests import test_batch_e_dispatch as helpers
from tests import test_batch_e_migration as migration_helpers
from tests.test_driver_acpx_dsh import FAKE_CLIENT, STUB_AGENT


def _run_request(project, spec, project_root: Path) -> RunRequest:
    return RunRequest(
        task=spec,
        project=project,
        project_root=str(project_root),
        workspace_root=str(project_root),
    )


def _offline_controller(store: Store, project_root: Path, spec, tmp_path: Path):
    """A root controller whose two roles are both the offline fake driver."""
    binding = helpers._binding(store, spec, project_root)
    limits = helpers._limits()
    return helpers._root_controller(
        store,
        FakeDriver(project_root),
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )


# --------------------------------------------------------------------------
# 1. the root belongs to its active run, not only to its unresolved invocations
# --------------------------------------------------------------------------


def test_a_checking_run_keeps_exclusive_ownership_of_its_root(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """While the first run is in CHECKING, a second revision cannot take the same root.

    The window this covers is the one a pending-invocation check misses: the implementer has
    settled, so nothing is unresolved, and the approved checks are still running. Allowing a
    second revision in would mean one root executing two tasks at once.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits(max_repairs=1)
    first = helpers._root_controller(
        store,
        FakeDriver(project_root),
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    entered, release = threading.Event(), threading.Event()

    class HeldCheck(FakeCheckRunner):
        def run(self, check, cwd, timeout_seconds):
            entered.set()
            assert release.wait(15), "the review probe did not release the check"
            return super().run(check, cwd, timeout_seconds)

    first.runners = CheckRunners({"fake": HeldCheck()})
    next_spec = task_spec.model_copy(update={"revision": 2})
    second_driver = FakeDriver(project_root)
    second = helpers._root_controller(
        store,
        second_driver,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=next_spec,
            binding=binding,
            limits=limits,
            project_root=project_root,
            authorization_id="AUTH-second",
        ),
        data_dir=tmp_path / "data",
    )

    with helpers.RunningTask(first, _run_request(project, task_spec, project_root)) as running:
        try:
            assert entered.wait(15), "the first run never reached verification"
            first_run = store.list_runs()[0]["run_id"]
            assert store.get_run(first_run)["task_state"] == TaskState.CHECKING.value
            assert store.pending_invocations(binding.root_id) == [], (
                "the probe is only meaningful while nothing is unresolved"
            )

            result, refusal = helpers._run_or_refusal(
                second, _run_request(project, next_spec, project_root)
            )
            assert second_driver.started == [], (
                f"first run={store.get_run(first_run)['task_state']}; "
                f"second roles={[r.role for r in second_driver.started]}; result={result}"
            )
            reason = str(refusal) if refusal is not None else str(result.block_reason)
            assert "is owned by run" in reason or "One root runs one task" in reason, reason
        finally:
            release.set()
        running.thread.join(timeout=30)
        assert running.error is None, repr(running.error)

    # The first run finished its loop on the root it owns; the second never charged anything.
    root = store.root_budget_view(binding.root_id)
    assert root is not None
    assert root.used_top_level_submissions == 2, "the first run's own two dispatches"
    assert first_run in root.run_ids
    assert len(root.run_ids) == 1, "the refused revision never joined the root"


# --------------------------------------------------------------------------
# 2. one reviewer per candidate, whatever the invocation id says
# --------------------------------------------------------------------------


def test_a_settled_reviewer_cannot_be_reserved_again_with_a_new_id(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """The first review settles, and a second reviewer id is still refused.

    This is the shape a pending-only rule misses: after the verdict is recorded nothing is
    unresolved, so only a per-attempt rule stops the same candidate from being reviewed twice.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._review_phase_run(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="reviewer", invocation_id="I-review-first",
    )
    store.mark_invocation_started("I-review-first")
    store.settle_invocation("I-review-first", outcome=InvocationOutcome.COMPLETED)

    with pytest.raises(StoreError) as refused:
        helpers._reserve(
            store, run_id="R-dispatch", binding=binding, limits=limits,
            role="reviewer", invocation_id="I-review-second",
        )
    assert "already dispatched a reviewer invocation" in str(refused.value)
    assert store.attempts_for("R-dispatch")[0]["review_invocation_id"] == "I-review-first", (
        "the attempt keeps naming the review that produced its verdict"
    )


# --------------------------------------------------------------------------
# 3. the whole remaining loop has to fit before the implementer is bought
# --------------------------------------------------------------------------


def test_remaining_root_allowance_must_cover_the_required_review_before_implementation(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """A root with one submission left refuses the implementer, because the loop costs two.

    Buying the implementation and then discovering the review is unaffordable is the half-loop
    the admission gate exists to prevent. The refusal happens before any driver is built.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits(max_top_level_submissions=3, max_repairs=1)
    first_driver = FakeDriver(project_root)
    first = helpers._root_controller(
        store,
        first_driver,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    result = first.run_task(_run_request(project, task_spec, project_root))
    assert result.task_state is TaskState.ACCEPTED
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 2

    next_spec = task_spec.model_copy(update={"revision": 2})
    second_driver = FakeDriver(project_root)
    second = helpers._root_controller(
        store,
        second_driver,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=next_spec,
            binding=binding,
            limits=limits,
            project_root=project_root,
            authorization_id="AUTH-second",
        ),
        data_dir=tmp_path / "data",
    )
    result, refusal = helpers._run_or_refusal(
        second, _run_request(project, next_spec, project_root)
    )
    assert second_driver.started == [], (
        "required loop costs 2 but the root has 1; purchased roles="
        f"{[request.role for request in second_driver.started]}; result={result} refusal={refusal}"
    )
    reason = str(refusal) if refusal is not None else f"{result.block_code}: {result.block_reason}"
    assert "cannot complete this run's loop" in reason or "still needs" in reason, reason

    root = store.root_budget_view(binding.root_id)
    assert root is not None
    assert root.used_top_level_submissions == 2, "the refused revision bought nothing"
    assert len(root.run_ids) == 1


# --------------------------------------------------------------------------
# 4. a requested launch is not a created process
# --------------------------------------------------------------------------


def test_production_driver_suppression_after_the_launch_request_is_not_started(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """The real driver suppresses the spawn, and the ledger says the process was never created.

    The stop is injected inside ``start`` - after the controller recorded the launch request and
    before the driver reaches its spawn gate - which is exactly the window where "we asked" and
    "it exists" differ. The driver reports ``created=False`` from inside that gate, so the ledger
    records ``not_started`` with no start timestamp rather than a started invocation.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "cooperative"],
    )
    controller = helpers._root_controller(
        store,
        FakeDriver(project_root),
        reviewer_driver=reviewer,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original_start, original_handle = reviewer.start, reviewer.start_handle
    launches: list[tuple[int | None, bool]] = []

    def observe(request):
        handle = original_handle(request)
        launches.append((handle.pid, handle.start_cancelled))
        return handle

    def stop_before_driver_start(request):
        controller.cancel(request.run_id)
        return original_start(request)

    reviewer.start_handle = observe
    reviewer.start = stop_before_driver_start
    try:
        result = controller.run_task(_run_request(project, task_spec, project_root))
        assert launches == [(None, True)], "the production driver created no child"
        intent = [i for i in store.invocations_for(result.run_id) if i.role == "reviewer"][0]
        assert (intent.state, intent.started_at) == (InvocationStartState.NOT_STARTED, None), (
            f"driver launches={launches}, ledger={intent.model_dump(mode='json')}"
        )
        assert intent.launch_requested_at is not None, (
            "the controller did ask for the launch; the ledger records that separately"
        )
        counts = store.invocation_state_counts(result.run_id)
        # The implementer's driver is the offline fake: its launch happened and created no child,
        # so the *process* count is zero while the state count shows one launch. Only a driver
        # that reports a pid can move ``processes``.
        assert counts.processes == 0
        assert counts.ever_started == 0
        assert counts.not_started == 1
        assert counts.childless_launches == 1
        assert store.root_budget_view(binding.root_id).used_top_level_submissions == 2, (
            "the suppressed launch keeps its consumption: a reservation is never refunded"
        )
    finally:
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)


def test_the_offline_driver_reports_both_spawn_outcomes_honestly(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """The offline driver's own two reports reach the ledger as the two different facts they are.

    The fake driver starts no process, so it reports the truth it can: a stop that won its
    handoff created nothing, and a launch that went through is a start it can stand behind. This
    test drives both through the real controller and reads the ledger, which is the seam the
    earlier version of this code got wrong in both directions - it recorded the stop case as
    started, and it counted a never-launched reservation as one.

    The second scenario is deliberately an accepted run (``COMPLETED`` in the limit sense): the
    offline driver has no separate launch-result contract, so the honest comparison is between
    "a stop won before any launch" and "a launch happened".
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()

    # (a) A stop that lands inside the driver's handoff: reported as no process, kept as spend.
    stopped_driver = FakeDriver(project_root)
    stopped = helpers._root_controller(
        store,
        stopped_driver,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    real_start = stopped_driver.start

    def stop_then_start(request):
        if request.role == "reviewer":
            stopped.cancel(request.run_id)
        return real_start(request)

    stopped_driver.start = stop_then_start
    first = stopped.run_task(_run_request(project, task_spec, project_root))

    reviewer = [i for i in store.invocations_for(first.run_id) if i.role == "reviewer"][0]
    assert reviewer.state is InvocationStartState.NOT_STARTED, (
        f"the stop won the handoff, so nothing launched; ledger={reviewer.model_dump(mode='json')}"
    )
    assert reviewer.started_at is None
    assert reviewer.launch_requested_at is not None, "the controller did ask for the launch"
    counts = store.invocation_state_counts(first.run_id)
    # The offline driver creates no child process, so nothing here is a process - and the
    # suppressed reviewer launch is not even a launch. The two facts are counted separately.
    assert counts.processes == 0
    assert counts.ever_started == 0
    assert counts.not_started == 1
    assert counts.childless_launches == 1, "only the implementer's launch actually happened"
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 2, (
        "the suppressed launch keeps its consumption: a reservation is never refunded"
    )

    # (b) A launch that went through: the driver reports creating a process, and it counts.
    # A different task id on purpose: an identical spec is a history query that returns the
    # blocked run above, which would measure nothing.
    clean_spec = task_spec.model_copy(update={"task_id": "T-002"})
    clean_binding = helpers._binding(store, clean_spec, project_root)
    clean_driver = FakeDriver(project_root)
    clean = helpers._root_controller(
        store,
        clean_driver,
        binding=clean_binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=clean_spec,
            binding=clean_binding,
            limits=limits,
            project_root=project_root,
            authorization_id="AUTH-clean",
        ),
        data_dir=tmp_path / "data",
    )
    second = clean.run_task(_run_request(project, clean_spec, project_root))
    assert second.task_state is TaskState.ACCEPTED
    implementer = [i for i in store.invocations_for(second.run_id) if i.role == "implementer"][0]
    assert implementer.state is InvocationStartState.SETTLED
    assert implementer.launched, "the driver reported that its launch happened"
    clean_counts = store.invocation_state_counts(second.run_id)
    assert clean_counts.settled == 2, "both roles settled"
    assert clean_counts.childless_launches == 2
    assert clean_counts.processes == 0, (
        "the offline driver creates no operating-system child, so a completed run is not two "
        "processes - the process count reads the recorded fact, not the result state"
    )
    assert clean_counts.ever_started == 0


# --------------------------------------------------------------------------
# 5. an unresolved reservation is not a started process
# --------------------------------------------------------------------------


def test_reconciling_reserved_to_unknown_does_not_invent_a_started_process(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """Closing a never-launched reservation leaves ``ever_started`` at zero.

    ``ever_started`` means "a process existed". A launch that was never requested cannot become
    one by being reconciled, or the report would claim work nobody observed.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="implementer", invocation_id="I-never-started",
    )
    store.mark_unsettled_invocations_unknown("R-dispatch", "the controller died before any launch")

    intent = store.invocation("I-never-started")
    assert intent is not None
    assert intent.started_at is None
    assert intent.launch_requested_at is None
    counts = store.invocation_state_counts("R-dispatch")
    assert counts.ever_started == 0, (
        f"no driver was called; counts={counts.model_dump()}, ever_started={counts.ever_started}"
    )
    assert counts.launch_unknown == 1
    assert intent.pending is True, "it still blocks the root: the spend is unresolved either way"


def test_a_requested_but_unconfirmed_launch_is_not_counted_as_started(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """A launch the controller asked for and never got a report about is ``launch_unknown``.

    The mirror of the previous case, and the one a crash between the request and the spawn report
    produces: a transport call may have been made, so the root must stay blocked, but no process
    was reported, so nothing may be counted as started.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="implementer", invocation_id="I-requested",
    )
    assert store.mark_invocation_launch_requested("I-requested") is True
    store.mark_unsettled_invocations_unknown("R-dispatch", "the controller died after the request")

    intent = store.invocation("I-requested")
    assert intent is not None
    assert intent.state is InvocationStartState.LAUNCH_UNKNOWN
    assert intent.launch_requested_at is not None
    assert intent.started_at is None
    counts = store.invocation_state_counts("R-dispatch")
    assert counts.ever_started == 0
    assert counts.launch_unknown == 1
    assert intent.pending is True


def test_a_driver_report_turns_a_requested_launch_into_a_started_process(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """The spawn report is what decides, and it can only ever move the fact forward.

    A report of ``created=False`` after a start does not rewrite history either: once a process is
    known to exist, a late "nothing was created" would be the same class of mistake in the other
    direction.
    """
    from hflow.contracts import SpawnFact

    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="implementer", invocation_id="I-reported",
    )
    store.mark_invocation_launch_requested("I-reported")
    assert store.record_invocation_spawn(
        SpawnFact(invocation_id="I-reported", created=True, pid=4242, detail="child created")
    ) is True

    started = store.invocation("I-reported")
    assert started is not None
    assert started.state is InvocationStartState.STARTED
    assert started.started_at is not None
    assert store.invocation_state_counts("R-dispatch").ever_started == 1

    # A later "nothing was created" report cannot undo a recorded process.
    assert store.record_invocation_spawn(
        SpawnFact(invocation_id="I-reported", created=False, pid=None, detail="late denial")
    ) is False
    assert store.invocation("I-reported").state is InvocationStartState.STARTED


def test_the_contract_state_vocabulary_and_the_ledger_filter_agree() -> None:
    """``InvocationIntent.pending`` and the store's SQL filter must stay the same set.

    The store builds its "unresolved" query from a module-level constant; a state added to one
    side and not the other would let an invocation block the root in Python while the SQL says
    the root is free.
    """
    from hflow.contracts import InvocationIntent
    from hflow.store import INVOCATION_UNRESOLVED_STATES

    for state in InvocationStartState:
        intent = InvocationIntent(
            invocation_id="I-x",
            root_id="root-x",
            run_id="R-x",
            attempt_id="A-x",
            role="implementer",
            reserved_at="2026-01-01T00:00:00Z",
            state=state,
        )
        assert intent.pending is (state.value in INVOCATION_UNRESOLVED_STATES), (
            f"{state.value}: pending={intent.pending} but the ledger filter says "
            f"{state.value in INVOCATION_UNRESOLVED_STATES}"
        )


# --------------------------------------------------------------------------
# 6. an artifact's origin is a persisted fact, not a field that dies with the process
# --------------------------------------------------------------------------


def test_offline_synthetic_origin_survives_authorization_registration(
    store: Store, task_spec, project_root: Path
) -> None:
    """The structured statement "nobody approved this record" has to reach the database.

    Without it, the ledger reads back ``user_artifact`` for a record the CLI synthesized, which is
    the one claim the field exists to prevent. This is a record-keeping defect: it was never a
    way to authorize a real dispatch, because ``verify_authorization`` reads the in-memory record.
    """
    from hflow.cli import offline_root_authorization

    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    user_record = helpers._authorization(
        spec=task_spec, binding=binding, limits=limits, project_root=project_root
    )
    record = offline_root_authorization(binding=user_record.binding, limits=limits)
    assert record.origin == "cli_offline_synthetic"
    store.register_authorization(record.as_store_record())

    persisted = store.authorization_state(record.authorization_id)
    assert persisted is not None
    assert persisted["origin"] == "cli_offline_synthetic", persisted

    # The same id cannot be re-registered as a user artifact: the origin is immutable too.
    forged = dict(record.as_store_record())
    forged["origin"] = "user_artifact"
    with pytest.raises(StoreError) as refused:
        store.register_authorization(forged)
    assert "different immutable field(s)" in str(refused.value)
    assert store.authorization_state(record.authorization_id)["origin"] == "cli_offline_synthetic"


# --------------------------------------------------------------------------
# 7. a process count reads process facts, never result states
# --------------------------------------------------------------------------


def test_fake_completed_result_does_not_mean_two_processes_existed(
    store: Store, project: ProjectConfig, task_spec, project_root: Path, tmp_path: Path
) -> None:
    """A completed offline run creates no operating-system child, so it is not two processes."""
    controller = _offline_controller(store, project_root, task_spec, tmp_path)
    result = controller.run_task(_run_request(project, task_spec, project_root))
    assert result.task_state is TaskState.ACCEPTED

    intents = store.invocations_for(result.run_id)
    assert len(intents) == 2
    counts = store.invocation_state_counts(result.run_id)
    assert counts.ever_started == 0, (
        f"the offline driver created no children; ever_started={counts.ever_started}; "
        f"facts={[(i.state.value, i.spawn_kind.value, i.process_created) for i in intents]}"
    )
    assert counts.processes == 0
    assert counts.childless_launches == 2
    assert counts.settled == 2, "the dispatches and their outcomes are still counted"


def test_unknown_return_without_spawn_fact_does_not_invent_a_process(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """A requested launch that went unknown is still not evidence that a process existed."""
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="implementer", invocation_id="I-no-report",
    )
    store.mark_invocation_launch_requested("I-no-report")
    store.settle_invocation("I-no-report", outcome=InvocationOutcome.OUTCOME_UNKNOWN)

    entry = store.invocation("I-no-report")
    assert entry is not None
    assert entry.started_at is None and entry.pending
    counts = store.invocation_state_counts("R-dispatch")
    assert counts.ever_started == 0, entry.model_dump()
    assert counts.processes == 0
    assert counts.unknown == 1, "the unresolved state is still recorded"


# --------------------------------------------------------------------------
# 8. a failed spawn *record* is not proof that nothing launched
# --------------------------------------------------------------------------


def test_failed_spawn_record_then_real_forced_stop_must_not_claim_never_started(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A real child existed and was force-stopped; the ledger may not say "never started".

    The controller's spawn bookkeeping is made to fail, so no launch is recorded even though the
    production driver really created a managed child and the stop really terminated it. An empty
    ``started_at`` then means "nobody recorded a launch", not "nothing launched" - and the ledger
    must keep the second reading out.
    """
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "stubborn"],
    )
    controller = helpers._root_controller(
        store,
        FakeDriver(project_root),
        reviewer_driver=reviewer,
        binding=binding,
        limits=limits,
        authorization=helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )
    original_record = store.record_invocation_spawn
    facts, receipts = [], []

    def fail_reviewer_record(fact):
        recorded = store.invocation(fact.invocation_id)
        if recorded is not None and recorded.role == "reviewer":
            facts.append(fact)
            raise StoreError("injected failure to persist the spawn observation")
        return original_record(fact)

    def start_then_stop(request):
        handle = reviewer.start_handle(request)
        assert handle.pid is not None, "the real managed client must exist"
        receipt = controller.cancel(request.run_id)
        receipts.append(receipt)
        assert receipt.status == "confirmed_stopped" and receipt.mechanism == "forced"
        return reviewer.collect(handle)

    monkeypatch.setattr(store, "record_invocation_spawn", fail_reviewer_record)
    monkeypatch.setattr(reviewer, "start", start_then_stop)
    try:
        result = controller.run_task(_run_request(project, task_spec, project_root))
        assert facts and facts[0].created and facts[0].pid is not None, (
            "the driver did report a real child; only the *record* of it was made to fail"
        )
        intent = [i for i in store.invocations_for(result.run_id) if i.role == "reviewer"][0]
        assert intent.state is not InvocationStartState.NOT_STARTED, (
            f"real child pid={facts[0].pid}, stop={receipts[0].model_dump()}, "
            f"ledger={intent.model_dump()}"
        )
        assert intent.state is InvocationStartState.LAUNCH_UNKNOWN
        assert intent.pending is True, "an unrecorded launch keeps blocking the root"
        assert intent.launch_requested_at is not None
        counts = store.invocation_state_counts(result.run_id)
        assert counts.processes == 0, "no process was recorded, because the record failed"
    finally:
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)


# --------------------------------------------------------------------------
# 9. a v2 terminal row keeps its outcome and loses its invented start time
# --------------------------------------------------------------------------


@pytest.mark.parametrize("old_state", ["settled", "unknown"])
def test_v2_terminal_rows_do_not_promote_request_timestamps_to_process_facts(
    tmp_path: Path, task_spec, old_state: str
) -> None:
    """v2 wrote its timestamp before asking the driver, in every state past ``reserved``.

    A ``settled`` or ``unknown`` row therefore carries a *request* time, and leaving it in
    ``started_at`` would promote it to a launch fact - and from there into the process count. The
    row keeps its own state and outcome; only the misnamed timestamp moves.
    """
    path = tmp_path / "v2.sqlite"
    migration_helpers._build_v1_database(path, task_spec)
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        migrate(connection, path, supported=2)
        connection.execute(
            """
            INSERT INTO invocations
                (invocation_id, root_id, run_id, attempt_id, role, state, reserved_at, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "I-v2",
                "root-v2",
                migration_helpers.RUN_ID,
                migration_helpers.ATTEMPT_ID,
                "implementer",
                old_state,
                "2026-09-25T00:00:00Z",
                "2026-09-25T00:00:01Z",
            ),
        )
        migrate(connection, path)
        row = connection.execute(
            "SELECT state, launch_requested_at, started_at, process_started_at, spawn_kind "
            "FROM invocations WHERE invocation_id = 'I-v2'"
        ).fetchone()
        state, launch_requested_at, started_at, process_started_at, spawn_kind = row
        assert state == old_state, "the old outcome is preserved, not rewritten"
        assert launch_requested_at == "2026-09-25T00:00:01Z", (
            "the v2 timestamp is a launch request, and v3 records it there"
        )
        assert started_at is None, "v2 never recorded a driver report, so no launch is claimed"
        assert process_started_at is None
        assert spawn_kind == "unknown", (
            "v3 recorded no process fact either, so the kind of launch stays unknown"
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 10. settling the same unresolved launch twice must not raise
# --------------------------------------------------------------------------


def test_reconcile_then_confirmed_cancel_does_not_raise(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """A reconciled launch that is then stopped is settled twice, and the second must be a no-op.

    The ordering is ordinary: ``reconcile`` closes an interrupted launch as ``launch_unknown``
    and keeps the root blocked, then the real driver confirms the forced stop and the same
    invocation is settled again. A public ``cancel`` that raises *after* recording its receipt
    reports a failure that did not happen - the run really was stopped, and its boundary is
    unchanged either way. Nothing new is recorded: the state and the "never re-dispatched" rule
    stay exactly as they were.
    """
    original_cancel = Controller.cancel
    errors: list[BaseException] = []

    def reconcile_then_cancel(self, run_id):
        self.reconcile(run_id)
        try:
            return original_cancel(self, run_id)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)
            # Preserve the receipt the cancel already wrote, so the probe can still collect and
            # release its real child; the exception itself is what this test forbids.
            return self.store.cancel_state(run_id)[1]

    monkeypatch.setattr(Controller, "cancel", reconcile_then_cancel)
    test_failed_spawn_record_then_real_forced_stop_must_not_claim_never_started(
        store, project, task_spec, project_root, tmp_path, monkeypatch
    )
    assert errors == [], f"public cancel raised after reconcile: {errors!r}"


def test_marking_an_already_unresolved_launch_again_keeps_its_state(
    store: Store, project, task_spec, project_root: Path
) -> None:
    """The idempotent half, at the store level and without a driver in the way."""
    binding = helpers._binding(store, task_spec, project_root)
    limits = helpers._limits()
    helpers._seed(
        store, project=project, spec=task_spec, binding=binding, limits=limits,
        project_root=project_root,
    )
    helpers._reserve(
        store, run_id="R-dispatch", binding=binding, limits=limits,
        role="implementer", invocation_id="I-twice",
    )
    store.mark_invocation_launch_requested("I-twice")
    store.mark_launch_unresolved("I-twice", "first closure: reconciled")
    store.mark_launch_unresolved("I-twice", "second closure: the stop was confirmed")

    entry = store.invocation("I-twice")
    assert entry is not None
    assert entry.state is InvocationStartState.LAUNCH_UNKNOWN, "no new state, as required"
    assert entry.pending is True
    assert entry.started_at is None
    assert "second closure" in entry.detail, "the later reason is what a reader should see"
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 1, (
        "settling twice never refunds or double-charges"
    )

    # A row that did record a launch is refused instead: a known launch is not an unknown one.
    # The late report records the launch without reopening the entry: it becomes ``unknown``
    # (a launch nobody observed the result of), never the open ``started`` a settlement closes.
    store.record_invocation_spawn(
        SpawnFact(invocation_id="I-twice", created=True, pid=4242, spawn_kind=SpawnKind.PROCESS)
    )
    with pytest.raises(StoreError) as refused:
        store.mark_launch_unresolved("I-twice", "too late")
    assert "recorded a launch" in str(refused.value)
    recorded = store.invocation("I-twice")
    assert recorded.state is InvocationStartState.UNKNOWN and recorded.started_at is not None


# --------------------------------------------------------------------------
# 11. a database already stamped v3 still gets repaired
# --------------------------------------------------------------------------


@pytest.mark.parametrize("old_state", ["settled", "unknown"])
def test_already_v3_database_repairs_the_legacy_request_timestamp(
    tmp_path: Path, task_spec, old_state: str
) -> None:
    """The corrected v3 step cannot run again for a file that already stamped v3.

    The first v3 migration backfilled only ``state = 'started'``, so a v2 row that was already
    ``settled`` or ``unknown`` kept its request time in ``started_at`` and never got a
    ``launch_requested_at``. Fixing that step helps only databases that have not been upgraded
    yet; the repair therefore lives in v4, which every such file will run, and leaves the state,
    the outcome and the consumption alone.
    """
    path = tmp_path / "already-v3.sqlite"
    migration_helpers._build_v1_database(path, task_spec)
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        migrate(connection, path, supported=3)
        # The exact shape the old v3 left: no launch_requested_at, the request time in started_at.
        connection.execute(
            """
            INSERT INTO invocations
                (invocation_id, root_id, run_id, attempt_id, role, state, reserved_at, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "I-old-v3",
                "root-v3",
                migration_helpers.RUN_ID,
                migration_helpers.ATTEMPT_ID,
                "implementer",
                old_state,
                "2026-09-25T00:00:00Z",
                "2026-09-25T00:00:01Z",
            ),
        )
        migrate(connection, path)
        row = connection.execute(
            "SELECT state, launch_requested_at, started_at, process_started_at, spawn_kind "
            "FROM invocations WHERE invocation_id = 'I-old-v3'"
        ).fetchone()
        state, launch_requested_at, started_at, process_started_at, spawn_kind = row
        assert state == old_state, "the outcome is preserved"
        assert launch_requested_at == "2026-09-25T00:00:01Z", (
            "the misnamed timestamp becomes the launch request it always was"
        )
        assert started_at is None, "and no launch is claimed on its behalf"
        assert process_started_at is None
        assert spawn_kind == "unknown"
    finally:
        connection.close()
