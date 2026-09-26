"""Batch E1 acceptance: the root ledger and the one atomic dispatch transaction.

What ``docs/batch-e-plan.md`` §7 E1 claims, as this file checks it:

1. two independent ``Store`` connections racing for the last unit of allowance commit at most
   one dispatch, and the root / run / authorization / invocation counters agree when they are
   read in **one** SQL snapshot statement;
2. a failure at any write inside the dispatch transaction rolls *all* of it back, and replaying
   the same ``invocation_id`` afterwards charges nothing and reports ``is_new=False``;
3. a second run of the same task, under a new revision, shares the same root row, pays for a
   repair, and cannot re-register a fresh allowance;
4. an unknown outcome keeps the consumption and blocks the root for every later revision.

Every test drives the real ``Store`` on a real file under ``tmp_path`` - never ``:memory:``,
because the race needs two connections and a snapshot reader needs a third. Nothing here calls a
model or the network, and no test waits on a sleep: the race is forced by a ``Barrier`` and the
rest are single-threaded.

Counters are asserted through a separate ``sqlite3`` connection with one statement, not through
the ``Store`` accessors. Four queries would let a reader observe a state no writer ever
committed (query two running after a dispatch that query one predates), which is exactly the
skew this batch exists to remove.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from hflow.contracts import (
    DispatchReservation,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RootBudgetBinding,
    RootBudgetLimits,
    TaskSpec,
    TaskState,
    root_id_for,
)
from hflow.store import Store, StoreError

CONTROLLER_ID = "ledger-test-controller"
CONTROLLER_BUILD = "ledger-test"
AUTHORIZATION_ID = "AUTH-ledger-test"
#: Not a real approval: a synthetic binding digest, recorded so the test can prove the ledger
#: never recomputes or rewrites it.
BINDING_DIGEST = "sha256:" + "d" * 64


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _binding(
    store: Store, *, project_id: str, repo_path: Path, task_id: str, root_id: str = ""
) -> RootBudgetBinding:
    """The binding a run resolves for one task, optionally with a *forged* root id."""
    derived = RootBudgetBinding.derive(
        project_id=project_id,
        repo_path=str(repo_path),
        task_id=task_id,
        ledger_path=store.path,
    )
    if not root_id:
        return derived
    return derived.model_copy(update={"root_id": root_id})


def _register_authorization(
    store: Store, *, max_submissions: int = 2, authorization_id: str = AUTHORIZATION_ID
) -> None:
    store.register_authorization(
        {
            "authorization_id": authorization_id,
            "mode": "m2-live-change",
            "binding_digest": BINDING_DIGEST,
            "user_text": "I approve one bounded run against this root ledger.",
            "provided_by": "user",
            "authorized_at": "2026-09-25T00:00:00Z",
            "max_top_level_submissions": max_submissions,
        }
    )


def _seed_ready_run(
    store: Store,
    *,
    run_id: str,
    project_id: str,
    spec: TaskSpec,
    turn_limit: int = 4,
    repair_limit: int = 0,
) -> str:
    """A claimed, READY run with no attempt: the state a dispatch starts from."""
    store.create_run(
        run_id=run_id,
        project_id=project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build=CONTROLLER_BUILD,
        checks_digest="checks-ledger-test",
        turn_limit=turn_limit,
        repair_limit=repair_limit,
    )
    store.claim_run(run_id, CONTROLLER_ID)
    store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)
    return run_id


def _next_revision(spec: TaskSpec) -> TaskSpec:
    """The same task at revision 2: a different spec digest, the same task id."""
    return spec.model_copy(
        update={
            "revision": spec.revision + 1,
            "goal": f"{spec.goal} (revision {spec.revision + 1})",
        }
    )


def _reserve(
    store: Store,
    *,
    run_id: str,
    invocation_id: str,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    role: str = "implementer",
    authorization_id: str = AUTHORIZATION_ID,
    authorization_max: int | None = None,
    reserved_turns: int = 1,
) -> DispatchReservation:
    return store.reserve_dispatch(
        run_id=run_id,
        controller_id=CONTROLLER_ID,
        invocation_id=invocation_id,
        role=role,
        reservation_id=f"B-{invocation_id}",
        reserved_turns=reserved_turns,
        reservation_expires_at="2999-01-01T00:00:00Z",
        root_binding=binding,
        root_limits=limits,
        authorization_id=authorization_id,
        authorization_max=authorization_max,
    )


def _snapshot(
    path: Path, *, root_id: str, authorization_id: str, run_ids: Sequence[str]
) -> dict[str, object]:
    """Every counter this file asserts on, in ONE statement, from a separate connection.

    ``run_ids`` adds per-run attempt / turn / invocation counts as scalar subqueries, so
    "the refused run has no attempt and no reserved turn" is read from the same snapshot as
    "the root charged one submission" instead of from a second query that a writer could have
    invalidated in between.
    """
    parameters: list[object] = [authorization_id, authorization_id, root_id, root_id]
    per_run: list[str] = []
    for index, run_id in enumerate(run_ids):
        per_run.append(
            f"(SELECT COUNT(*) FROM attempts WHERE run_id = ?) AS run{index}_attempt_rows"
        )
        parameters.append(run_id)
        per_run.append(f"(SELECT turns_reserved FROM runs WHERE run_id = ?) AS run{index}_turns")
        parameters.append(run_id)
        per_run.append(
            f"(SELECT COUNT(*) FROM invocations WHERE run_id = ?) AS run{index}_invocations"
        )
        parameters.append(run_id)
    parameters.append(root_id)

    sql = f"""
        SELECT
            rb.used_top_level_submissions AS root_used,
            rb.used_repairs               AS root_repairs,
            rb.max_top_level_submissions  AS root_max,
            rb.max_repairs                AS root_max_repairs,
            (SELECT COUNT(*) FROM root_budgets) AS root_rows,
            (SELECT used_top_level_submissions FROM authorizations
              WHERE authorization_id = ?) AS auth_used,
            (SELECT max_top_level_submissions FROM authorizations
              WHERE authorization_id = ?) AS auth_max,
            (SELECT COUNT(*) FROM invocations) AS invocation_rows_total,
            (SELECT COUNT(*) FROM invocations WHERE root_id = ?) AS invocation_rows_root,
            (SELECT COUNT(*) FROM attempts) AS attempt_rows_total,
            (SELECT COUNT(*) FROM attempts WHERE root_id = ?) AS attempt_rows_root,
            {", ".join(per_run)}
          FROM root_budgets rb
         WHERE rb.root_id = ?
    """
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()
    assert row is not None, f"no root row for {root_id}: the ledger was never registered"
    return dict(row)


# --------------------------------------------------------------------------
# 1. two connections, the last unit of allowance
# --------------------------------------------------------------------------


def test_two_connections_racing_for_the_last_unit_commit_at_most_one_dispatch(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """Both connections enter the dispatch transaction together; only one may commit.

    The root allows exactly one top-level submission. The loser's refusal is read from its own
    error message, and the ledger is then checked in one snapshot: if the rollback were partial
    (an authorization claimed without an attempt, a turn reserved for a run with no dispatch),
    one of the counters below would disagree with the others.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    registry = Store(path)
    racer = Store(path)
    try:
        limits = RootBudgetLimits(max_top_level_submissions=1, max_repairs=0)
        binding = _binding(
            registry,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
        )
        registry.register_root_budget(binding, limits)
        # Two submissions of room, so the authorization ceiling cannot be the first refusal:
        # this test is about the root's last unit, not about the approval's.
        _register_authorization(registry, max_submissions=2)
        run_ids = [
            _seed_ready_run(
                registry,
                run_id="R-race-1",
                project_id=project.project_id,
                spec=task_spec,
            ),
            _seed_ready_run(
                registry,
                run_id="R-race-2",
                project_id=project.project_id,
                spec=_next_revision(task_spec),
            ),
        ]

        together = threading.Barrier(2)
        invocation_ids = ["I-race-1", "I-race-2"]
        outcomes: dict[str, object] = {}

        def dispatch(store: Store, run_id: str, invocation_id: str) -> None:
            together.wait(timeout=30)  # both connections arrive at the transaction together
            try:
                outcomes[invocation_id] = _reserve(
                    store,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    binding=binding,
                    limits=limits,
                )
            except BaseException as exc:  # noqa: BLE001 - classified by the assertions below
                # A non-StoreError failure (a lock timeout, say) is not "a dispatch committed":
                # it is recorded here so the assertions report it instead of losing it in a
                # thread that died.
                outcomes[invocation_id] = exc

        threads = [
            threading.Thread(target=dispatch, args=(registry, run_ids[0], invocation_ids[0])),
            threading.Thread(target=dispatch, args=(racer, run_ids[1], invocation_ids[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "a dispatch thread deadlocked on the file lock"

        assert len(outcomes) == 2, f"both connections must report an outcome: {outcomes}"
        unexpected = [
            value
            for value in outcomes.values()
            if not isinstance(value, (DispatchReservation, StoreError))
        ]
        assert not unexpected, f"a dispatch failed with an unexpected error: {unexpected}"
        winners = [
            value
            for value in outcomes.values()
            if isinstance(value, DispatchReservation) and value.is_new
        ]
        refusals = [value for value in outcomes.values() if isinstance(value, StoreError)]
        assert len(winners) == 1, f"exactly one dispatch may commit: {outcomes}"
        assert len(refusals) == 1, f"the losing connection must be refused: {outcomes}"

        winner = winners[0]
        assert winner.invocation is not None
        intent = winner.invocation
        assert intent.root_id == binding.root_id
        assert intent.state is InvocationStartState.RESERVED
        assert winner.run_turns_reserved == 1, "the winner's own run moved by exactly one turn"
        assert winner.authorization_used == 1

        winner_index = run_ids.index(intent.run_id)
        loser_index = 1 - winner_index
        snapshot = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=run_ids,
        )
        assert snapshot["root_used"] == 1, "one dispatch, one consumed submission"
        assert snapshot["root_max"] == 1
        assert snapshot["root_repairs"] == 0
        assert snapshot["auth_used"] == 1, "the approval moved with the root, not before it"
        assert snapshot["invocation_rows_root"] == 1
        assert snapshot["invocation_rows_total"] == 1
        assert snapshot["attempt_rows_root"] == 1
        assert snapshot["attempt_rows_total"] == 1
        assert snapshot[f"run{winner_index}_turns"] == 1
        assert snapshot[f"run{winner_index}_attempt_rows"] == 1
        assert snapshot[f"run{winner_index}_invocations"] == 1
        assert snapshot[f"run{loser_index}_turns"] == 0, "the refusal must not have reserved a turn"
        assert snapshot[f"run{loser_index}_attempt_rows"] == 0, "the refusal wrote no attempt"
        assert snapshot[f"run{loser_index}_invocations"] == 0, "the refusal wrote no invocation"

        refusal = str(refusals[0])
        assert intent.invocation_id in refusal, "the refusal names the dispatch that won"
        assert "unresolved invocation" in refusal

        # The race consumed the root's only unit. Settling the winner does not give it back, so
        # the next dispatch is refused by the ceiling itself, not by the pending-invocation rule.
        assert registry.mark_invocation_started(intent.invocation_id) is True
        registry.settle_invocation(
            intent.invocation_id, outcome=InvocationOutcome.COMPLETED, detail="race winner settled"
        )
        settled = registry.invocation(intent.invocation_id)
        assert settled is not None
        assert settled.state is InvocationStartState.SETTLED
        assert settled.pending is False

        # The winner's run is still RUNNING, so the root's exclusive-ownership rule refuses the
        # losing run before the ceiling is even consulted: one root runs one task at a time. The
        # ceiling is checked on the run that owns the root, which is the same statement's other
        # branch, so both refusals are exercised rather than one standing in for the other.
        with pytest.raises(StoreError) as owned:
            _reserve(
                registry,
                run_id=run_ids[loser_index],
                invocation_id="I-race-late",
                binding=binding,
                limits=limits,
            )
        assert "is owned by run" in str(owned.value)
        assert run_ids[1 - loser_index] in str(owned.value)

        registry.set_blocked(
            intent.run_id, RefusalCode.INTERNAL_ERROR, "the race test ends this run"
        )
        with pytest.raises(StoreError) as exhausted:
            _reserve(
                registry,
                run_id=run_ids[loser_index],
                invocation_id="I-race-late-2",
                binding=binding,
                limits=limits,
            )
        assert "cannot complete this run's loop" in str(exhausted.value)
        assert "used and this run still needs" in str(exhausted.value)

        assert (
            _snapshot(
                path,
                root_id=binding.root_id,
                authorization_id=AUTHORIZATION_ID,
                run_ids=run_ids,
            )
            == snapshot
        ), "a refused dispatch may not move any counter"

        view = registry.root_budget_view(binding.root_id)
        assert view is not None
        assert view.used_top_level_submissions == 1
        assert view.remaining == 0
        assert view.run_ids == [intent.run_id], "the losing run was never charged"
        assert view.first_dispatch_at and view.deadline_at, (
            "the root's clock starts at its first win"
        )
    finally:
        racer.close()
        registry.close()


# --------------------------------------------------------------------------
# 2. failure injection inside the dispatch transaction
# --------------------------------------------------------------------------

#: The writes the dispatch transaction is made of. Patching the store's own helpers is the
#: injection point; every *assertion* below is on committed rows, so the test does not depend on
#: where in the sequence the failure was raised.
INJECTED_STEPS = (
    "_claim_authorization_locked",
    "_reserve_turn_locked",
    "_attempt_for_dispatch_locked",
    "_charge_root_locked",
    "_insert_invocation_locked",
)


@pytest.mark.parametrize("step", INJECTED_STEPS)
def test_a_failure_at_any_write_rolls_the_whole_dispatch_back(
    tmp_path: Path,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    """Inject a failure at each write, then replay the same invocation id.

    The first half proves the rollback: no root consumption, no authorization consumption, no
    reserved turn, no attempt row, no invocation row. The second half proves idempotency: the
    *same* ``invocation_id`` then commits once (the failed attempt left no record), and a second
    call for it returns the recorded intent with ``is_new=False`` and charges nothing.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        limits = RootBudgetLimits(max_top_level_submissions=2, max_repairs=1)
        binding = _binding(
            store,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
        )
        store.register_root_budget(binding, limits)
        _register_authorization(store, max_submissions=2)
        run_id = _seed_ready_run(
            store, run_id="R-inject", project_id=project.project_id, spec=task_spec
        )
        run_ids = [run_id]

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError(f"injected failure at {step}")

        monkeypatch.setattr(Store, step, explode)
        with pytest.raises(RuntimeError) as raised:
            _reserve(
                store, run_id=run_id, invocation_id="I-inject", binding=binding, limits=limits
            )
        assert f"injected failure at {step}" in str(raised.value)

        rolled_back = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=run_ids,
        )
        assert rolled_back["root_used"] == 0, "no root consumption survived the failure"
        assert rolled_back["root_repairs"] == 0
        assert rolled_back["auth_used"] == 0, "no authorization consumption survived"
        assert rolled_back["invocation_rows_total"] == 0
        assert rolled_back["attempt_rows_total"] == 0
        assert rolled_back["run0_turns"] == 0, "no turn stayed reserved"
        assert rolled_back["run0_attempt_rows"] == 0
        assert rolled_back["run0_invocations"] == 0
        assert store.get_run(run_id)["current_attempt_id"] is None

        monkeypatch.undo()  # the injection is over; the identical call must now commit once
        reservation = _reserve(
            store, run_id=run_id, invocation_id="I-inject", binding=binding, limits=limits
        )
        assert reservation.is_new is True
        committed = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=run_ids,
        )
        assert committed["root_used"] == 1
        assert committed["auth_used"] == 1
        assert committed["invocation_rows_total"] == 1
        assert committed["attempt_rows_total"] == 1
        assert committed["run0_turns"] == 1

        replay = _reserve(
            store, run_id=run_id, invocation_id="I-inject", binding=binding, limits=limits
        )
        assert replay.is_new is False, "a replay coordinates the recorded intent, it never reserves"
        assert replay.invocation is not None
        assert replay.invocation.invocation_id == "I-inject"
        assert replay.attempt_id == reservation.attempt_id
        assert "already recorded" in replay.detail
        assert (
            _snapshot(
                path,
                root_id=binding.root_id,
                authorization_id=AUTHORIZATION_ID,
                run_ids=run_ids,
            )
            == committed
        ), "a replay charges nothing and starts nothing"
    finally:
        store.close()


# --------------------------------------------------------------------------
# 3. cross-revision consumption
# --------------------------------------------------------------------------


def test_a_second_revision_shares_the_root_and_pays_for_a_repair(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A new revision of the same task keeps the root's counters; the second implementer pays.

    ``used_top_level_submissions`` and ``used_repairs`` are the root's, not a run's: the second
    run increments the *same* row that the first one spent, and there is exactly one root row
    afterwards. Re-registering the identical limits returns that row untouched, and a different
    ceiling is refused without changing anything.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        limits = RootBudgetLimits(max_top_level_submissions=2, max_repairs=1)
        binding = _binding(
            store,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
        )
        store.register_root_budget(binding, limits)
        _register_authorization(store, max_submissions=2)

        assert binding.root_id == root_id_for(
            project_id=project.project_id,
            repo_path=str(project_root),
            task_id=task_spec.task_id,
        ), "the root is derived from the task's identity, not chosen by a run"

        run_one = _seed_ready_run(
            store, run_id="R-root-1", project_id=project.project_id, spec=task_spec
        )
        first = _reserve(
            store, run_id=run_one, invocation_id="I-root-1", binding=binding, limits=limits
        )
        assert first.is_new is True
        assert first.invocation is not None
        assert first.invocation.root_id == binding.root_id
        assert first.invocation.is_repair is False, (
            "a root's first implementer dispatch is not a repair"
        )
        assert first.invocation.round == 1
        after_first = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=[run_one],
        )
        assert after_first["root_used"] == 1
        assert after_first["root_repairs"] == 0
        store.mark_invocation_started("I-root-1")
        store.settle_invocation(
            "I-root-1", outcome=InvocationOutcome.COMPLETED, detail="first revision delivered"
        )

        second_spec = _next_revision(task_spec)
        assert second_spec.task_id == task_spec.task_id
        assert second_spec.spec_digest() != task_spec.spec_digest(), (
            "the second run is a new revision with a new spec digest, not a resubmission"
        )
        run_two = _seed_ready_run(
            store,
            run_id="R-root-2",
            project_id=project.project_id,
            spec=second_spec,
        )
        # The first run is still RUNNING: a settled invocation is not a finished run. The root
        # belongs to that run until it reaches a terminal state, so the second revision is
        # refused *here* rather than being allowed to run concurrently against the same ledger.
        with pytest.raises(StoreError) as busy:
            _reserve(
                store, run_id=run_two, invocation_id="I-root-2-too-early", binding=binding, limits=limits
            )
        assert "is owned by run" in str(busy.value)
        assert run_one in str(busy.value)
        assert "One root runs one task at a time" in str(busy.value)

        # Sequential is fine: once the first run has ended, the same root takes the next revision.
        store.set_blocked(run_one, RefusalCode.INTERNAL_ERROR, "first revision ends before revision 2")
        second = _reserve(
            store, run_id=run_two, invocation_id="I-root-2", binding=binding, limits=limits
        )
        assert second.is_new is True
        assert second.invocation is not None
        assert second.invocation.root_id == binding.root_id, (
            "the new revision resolved the same root"
        )
        assert second.invocation.is_repair is True, (
            "a second implementer dispatch of the same root is a repair, even under a new revision"
        )
        assert second.invocation.round == 2, "rounds are counted across runs of the root"

        run_ids = [run_one, run_two]
        snapshot = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=run_ids,
        )
        assert snapshot["root_rows"] == 1, "a new revision must not fork the ledger"
        assert snapshot["root_used"] == 2
        assert snapshot["root_repairs"] == 1
        assert snapshot["auth_used"] == 2
        assert snapshot["invocation_rows_root"] == 2
        assert snapshot["run0_turns"] == 1
        assert snapshot["run1_turns"] == 1

        view = store.root_budget_view(binding.root_id)
        assert view is not None
        assert view.used_top_level_submissions == 2
        assert view.used_repairs == 1
        assert view.remaining == 0
        assert view.run_ids == run_ids
        assert view.authorization_ids == [AUTHORIZATION_ID]

        # The ceiling belongs to the approval that opened the root: a later run cannot raise it.
        with pytest.raises(StoreError) as changed:
            store.register_root_budget(
                binding, RootBudgetLimits(max_top_level_submissions=3, max_repairs=1)
            )
        assert "is recorded with limits" in str(changed.value)
        assert "no top-up path" in str(changed.value)

        # Re-registering the identical ceiling returns the recorded row: it never re-initialises.
        again = store.register_root_budget(binding, limits)
        assert (again["used_top_level_submissions"], again["used_repairs"]) == (2, 1)
        assert (
            _snapshot(
                path,
                root_id=binding.root_id,
                authorization_id=AUTHORIZATION_ID,
                run_ids=run_ids,
            )
            == snapshot
        ), "re-registration must not reset a counter"
    finally:
        store.close()


def test_a_forged_second_root_id_cannot_open_a_fresh_allowance(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """The same task under a different root id is refused: one task, one ledger row.

    A worker that could name its own root id could declare an unused one and spend the same task
    twice. Registration refuses it by name, and the dispatch transaction refuses it again - the
    unique index is the backstop - with no counter moved either way.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        limits = RootBudgetLimits(max_top_level_submissions=2, max_repairs=1)
        binding = _binding(
            store,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
        )
        forged = _binding(
            store,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
            root_id="root-" + "f" * 32,
        )
        assert forged.root_id != binding.root_id
        assert (forged.project_id, forged.repo_path, forged.task_id) == (
            binding.project_id,
            binding.repo_path,
            binding.task_id,
        )

        store.register_root_budget(binding, limits)
        _register_authorization(store, max_submissions=2)
        run_one = _seed_ready_run(
            store, run_id="R-forge-1", project_id=project.project_id, spec=task_spec
        )
        assert (
            _reserve(
                store, run_id=run_one, invocation_id="I-forge-1", binding=binding, limits=limits
            ).is_new
            is True
        )
        store.mark_invocation_started("I-forge-1")
        store.settle_invocation(
            "I-forge-1", outcome=InvocationOutcome.COMPLETED, detail="the recorded root's dispatch"
        )
        before = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=[run_one],
        )
        assert before["root_rows"] == 1
        assert before["root_used"] == 1

        with pytest.raises(StoreError) as clash:
            store.register_root_budget(forged, limits)
        assert "already has root" in str(clash.value)
        assert binding.root_id in str(clash.value), "the refusal names the root that stands"

        run_two = _seed_ready_run(
            store, run_id="R-forge-2", project_id=project.project_id, spec=_next_revision(task_spec)
        )
        # The dispatch transaction refuses the forged id as well, with the store's own error
        # rather than the unique index's bare failure, and rolls back everything it touched.
        with pytest.raises(StoreError) as forged_refusal:
            _reserve(
                store, run_id=run_two, invocation_id="I-forge-2", binding=forged, limits=limits
            )
        forged_message = str(forged_refusal.value)
        assert "already has root" in forged_message
        assert binding.root_id in forged_message
        assert "a second allowance" in forged_message

        after = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=[run_one, run_two],
        )
        assert after["root_rows"] == 1, "the forged root id created no second ledger row"
        assert after["root_used"] == 1, "the forged root id bought no allowance"
        assert after["auth_used"] == 1
        assert after["invocation_rows_total"] == 1
        assert after["attempt_rows_total"] == 1
        assert after["run1_turns"] == 0
        assert after["run1_attempt_rows"] == 0
        assert after["run1_invocations"] == 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# 4. unknown outcome
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [None, InvocationOutcome.OUTCOME_UNKNOWN])
def test_an_unknown_outcome_keeps_the_consumption_and_blocks_the_root(
    tmp_path: Path,
    project,
    task_spec: TaskSpec,
    project_root: Path,
    outcome: InvocationOutcome | None,
) -> None:
    """``settle_invocation(..., outcome=None)`` records ``unknown``, and unknown never refunds.

    Both spellings of "we do not know" (no outcome at all, and an explicit ``outcome_unknown``)
    must land in the same state. The consumption stays - a provider may have been billed - and
    the root refuses a later dispatch under a new revision, because a crash is not a licence to
    spend the same allowance twice.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        limits = RootBudgetLimits(max_top_level_submissions=2, max_repairs=1)
        binding = _binding(
            store,
            project_id=project.project_id,
            repo_path=project_root,
            task_id=task_spec.task_id,
        )
        store.register_root_budget(binding, limits)
        _register_authorization(store, max_submissions=2)
        run_one = _seed_ready_run(
            store, run_id="R-unknown-1", project_id=project.project_id, spec=task_spec
        )
        reservation = _reserve(
            store, run_id=run_one, invocation_id="I-unknown-1", binding=binding, limits=limits
        )
        assert reservation.is_new is True
        assert store.mark_invocation_started("I-unknown-1") is True
        store.settle_invocation(
            "I-unknown-1", outcome=outcome, detail="the process left no observable result"
        )

        intent = store.invocation("I-unknown-1")
        assert intent is not None
        assert intent.state is InvocationStartState.UNKNOWN
        if outcome is None:
            assert intent.outcome is None, "no outcome was observed, and none is invented"
        else:
            assert intent.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert intent.pending is True, "an unknown invocation still blocks its root"
        assert [entry.invocation_id for entry in store.pending_invocations(binding.root_id)] == [
            "I-unknown-1"
        ]

        kept = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=[run_one],
        )
        assert kept["root_used"] == 1, "an unknown outcome does not refund the root"
        assert kept["root_repairs"] == 0
        assert kept["auth_used"] == 1, "an unknown outcome does not refund the approval"
        assert kept["invocation_rows_root"] == 1

        run_two = _seed_ready_run(
            store,
            run_id="R-unknown-2",
            project_id=project.project_id,
            spec=_next_revision(task_spec),
        )
        with pytest.raises(StoreError) as blocked:
            _reserve(
                store,
                run_id=run_two,
                invocation_id="I-unknown-2",
                binding=binding,
                limits=limits,
            )
        message = str(blocked.value)
        assert "unresolved invocation" in message
        assert "I-unknown-1" in message, "the refusal names the invocation that blocks the root"
        assert "never re-dispatched" in message

        after = _snapshot(
            path,
            root_id=binding.root_id,
            authorization_id=AUTHORIZATION_ID,
            run_ids=[run_one, run_two],
        )
        assert after["root_used"] == kept["root_used"]
        assert after["auth_used"] == kept["auth_used"]
        assert after["invocation_rows_total"] == kept["invocation_rows_total"]
        assert after["attempt_rows_total"] == kept["attempt_rows_total"]
        assert after["run0_turns"] == kept["run0_turns"]
        assert after["run1_turns"] == 0, "the blocked revision reserved no turn"
        assert after["run1_attempt_rows"] == 0, "the blocked revision has no attempt"
        assert after["run1_invocations"] == 0
    finally:
        store.close()
