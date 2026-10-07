"""``hflow ledger settle``: an operator closes an unresolved ledger entry by attestation.

The ruling (2026-10-03) this file checks:

* only ``unknown`` and ``launch_unknown`` entries can be settled, into the new terminal state
  ``operator_settled``, by one compare-and-set, with an appended attestation row;
* ``consumed`` (the default) keeps every counter spent; ``void`` is refused for ``unknown`` and,
  for ``launch_unknown``, returns exactly what that dispatch charged to the root;
* an entry is settled once; open entries and final entries are refused, pointing at
  ``hflow resume``; a blank or oversized attestation is refused;
* the run's task state and outcome never change and nothing is re-dispatched - the only effect is
  that a *new* revision may reserve against the root;
* status / report / doctor render the entry as an attestation, never as an observation;
* the storage migration that adds the table keeps every existing row.

Everything is offline: a real ``Store`` on a file under ``tmp_path``, no driver, no model.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from pathlib import Path

import pytest

import hflow.ownership as ownership
from hflow import migrate
from hflow.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from hflow.contracts import (
    AttemptState,
    CheckPhase,
    InvocationIntent,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RootBudgetBinding,
    RootBudgetLimits,
    SpawnFact,
    SpawnKind,
    TaskSpec,
    TaskState,
)
from hflow.store import (
    ATTESTATION_MAX_CHARS,
    INVOCATION_OPERATOR_SETTLEABLE_STATES,
    INVOCATION_UNRESOLVED_STATES,
    InvocationNotFound,
    SettlementRefused,
    Store,
    StoreError,
)
from tests.test_batch_e_ledger import (
    AUTHORIZATION_ID,
    _binding,
    _next_revision,
    _register_authorization,
    _reserve,
    _seed_ready_run,
    _snapshot,
)
from tests.test_owner_lease import (
    _controller,
    _dead_identity,
    _seed_owned_dispatch,
    _start_idle_child,
    windows_only,
)

ATTEST ="Checked the provider console for 2026-10-03: no request from this launch appears."


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _root(
    store: Store, project, task_spec: TaskSpec, project_root: Path, *, repairs: int = 1
) -> tuple[RootBudgetBinding, RootBudgetLimits]:
    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=repairs)
    binding = _binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    return binding, limits


def _unknown_entry(
    store: Store,
    *,
    run_id: str,
    invocation_id: str,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
) -> None:
    """An implementer launch that was reported and never settled; the run blocks."""
    assert _reserve(
        store, run_id=run_id, invocation_id=invocation_id, binding=binding, limits=limits
    ).is_new
    assert store.mark_invocation_started(invocation_id) is True
    store.settle_invocation(invocation_id, outcome=None, detail="no observable result")
    store.set_blocked(run_id, RefusalCode.OUTCOME_UNKNOWN, "seeded unknown outcome")


def _launch_unknown_entry(
    store: Store,
    *,
    run_id: str,
    invocation_id: str,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    role: str = "implementer",
    block: RefusalCode = RefusalCode.CANCELLED_BY_OPERATOR,
) -> None:
    """A launch that was requested and never reported either way; the run blocks."""
    assert _reserve(
        store, run_id=run_id, invocation_id=invocation_id, binding=binding, limits=limits,
        role=role,
    ).is_new
    assert store.mark_invocation_launch_requested(invocation_id) is True
    store.mark_launch_unresolved(invocation_id, "stop confirmed before any spawn report")
    store.set_blocked(run_id, block, "seeded launch_unknown")


def _new_revision_can_reserve(
    store: Store,
    project,
    task_spec: TaskSpec,
    binding: RootBudgetBinding,
    limits: RootBudgetLimits,
    *,
    run_id: str = "R-next",
    invocation_id: str = "I-next",
) -> InvocationIntent:
    run = _seed_ready_run(
        store, run_id=run_id, project_id=project.project_id, spec=_next_revision(task_spec)
    )
    reservation = _reserve(
        store, run_id=run, invocation_id=invocation_id, binding=binding, limits=limits
    )
    assert reservation.is_new and reservation.invocation is not None
    return reservation.invocation


def _counters(store: Store, binding: RootBudgetBinding, run_ids: list[str]) -> dict[str, object]:
    return _snapshot(
        store.path, root_id=binding.root_id, authorization_id=AUTHORIZATION_ID, run_ids=run_ids
    )


# --------------------------------------------------------------------------
# the state vocabulary
# --------------------------------------------------------------------------


def test_operator_settled_is_terminal_and_not_unresolved() -> None:
    state = InvocationStartState.OPERATOR_SETTLED
    assert state.value == "operator_settled"
    assert state.value not in INVOCATION_UNRESOLVED_STATES
    assert INVOCATION_OPERATOR_SETTLEABLE_STATES == ("unknown", "launch_unknown")
    intent = InvocationIntent(
        invocation_id="I-x", root_id="root-x", run_id="R-x", attempt_id="A-x",
        role="implementer", reserved_at="2026-01-01T00:00:00Z", state=state,
    )
    assert intent.pending is False
    assert intent.process_created is False, "an attestation does not create a process fact"


# --------------------------------------------------------------------------
# consumed
# --------------------------------------------------------------------------


def test_consumed_from_unknown_unblocks_the_root_for_a_new_revision_and_keeps_counters(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    store = Store(tmp_path / "data" / "hflow.sqlite")
    try:
        binding, limits = _root(store, project, task_spec, project_root)
        run_one = _seed_ready_run(
            store, run_id="R-one", project_id=project.project_id, spec=task_spec
        )
        _unknown_entry(store, run_id=run_one, invocation_id="I-one", binding=binding, limits=limits)
        before = _counters(store, binding, [run_one])
        run_before = store.get_run(run_one)

        # Blocked first: the unknown entry holds the root.
        with pytest.raises(StoreError, match="unresolved invocation"):
            _new_revision_can_reserve(store, project, task_spec, binding, limits,
                                      run_id="R-refused", invocation_id="I-refused")

        settlement = store.settle_by_operator(
            "I-one", settled_as="consumed", attested_by="operator-1", attestation=ATTEST
        )
        assert settlement.prior_state == "unknown"
        assert settlement.settled_as == "consumed"
        assert settlement.basis == "operator_attested"
        assert settlement.attested_by == "operator-1"
        assert settlement.attestation == ATTEST
        assert settlement.returned_top_level_submissions == 0
        assert settlement.returned_repairs == 0

        intent = store.invocation("I-one")
        assert intent is not None and intent.state is InvocationStartState.OPERATOR_SETTLED
        assert intent.outcome is None, "no outcome is invented by an attestation"
        assert intent.pending is False
        after = _counters(store, binding, [run_one])
        for key in ("root_used", "root_repairs", "auth_used", "run0_turns", "invocation_rows_total"):
            assert after[key] == before[key], key

        # The run is untouched: still BLOCKED, same code and reason, nothing re-dispatched.
        run_after = store.get_run(run_one)
        assert run_after["task_state"] == TaskState.BLOCKED.value
        assert run_after["block_code"] == run_before["block_code"]
        assert run_after["block_reason"] == run_before["block_reason"]
        assert len(store.invocations_for(run_one)) == 1

        # A NEW revision may now reserve; the consumed entry still counts as the first
        # implementer attempt, so the next one is charged as the root's repair.
        # (The refused revision's run row exists and reserved nothing; it now may.)
        reservation = _reserve(
            store, run_id="R-refused", invocation_id="I-next", binding=binding, limits=limits
        )
        assert reservation.is_new and reservation.invocation is not None
        assert reservation.invocation.is_repair is True
        final = _counters(store, binding, [run_one])
        assert final["root_used"] == int(before["root_used"]) + 1
        assert final["root_repairs"] == int(before["root_repairs"]) + 1
    finally:
        store.close()


def test_consumed_from_launch_unknown_keeps_counters(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-l", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-l", binding=binding, limits=limits)
    before = _counters(store, binding, [run])
    settlement = store.settle_by_operator(
        "I-l", settled_as="consumed", attested_by="op", attestation=ATTEST
    )
    assert settlement.prior_state == "launch_unknown"
    assert _counters(store, binding, [run])["root_used"] == before["root_used"]


# --------------------------------------------------------------------------
# void
# --------------------------------------------------------------------------


def test_void_from_launch_unknown_returns_the_root_charge(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root, repairs=0)
    run = _seed_ready_run(store, run_id="R-v", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-v", binding=binding, limits=limits)
    before = _counters(store, binding, [run])
    assert before["root_used"] == 1

    settlement = store.settle_by_operator(
        "I-v", settled_as="void", attested_by="op", attestation=ATTEST
    )
    assert settlement.returned_top_level_submissions == 1
    assert settlement.returned_repairs == 0
    after = _counters(store, binding, [run])
    assert after["root_used"] == 0
    assert after["root_repairs"] == 0
    assert after["auth_used"] == before["auth_used"], "the approval's counter is not returned"
    assert after["run0_turns"] == before["run0_turns"], "the run is not revived"
    assert store.get_run(run)["task_state"] == TaskState.BLOCKED.value

    # The voided first attempt does not turn the next implementer into a repair: with
    # max_repairs=0 the next revision can still dispatch its first implementation.
    nxt = _new_revision_can_reserve(store, project, task_spec, binding, limits)
    assert nxt.is_repair is False
    final = _counters(store, binding, [run])
    assert final["root_used"] == 1 and final["root_repairs"] == 0


def test_void_of_a_repair_launch_returns_the_repair_too(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root, repairs=1)
    run_one = _seed_ready_run(store, run_id="R-a", project_id=project.project_id, spec=task_spec)
    _unknown_entry(store, run_id=run_one, invocation_id="I-a", binding=binding, limits=limits)
    store.settle_by_operator("I-a", settled_as="consumed", attested_by="op", attestation=ATTEST)
    run_two = _seed_ready_run(
        store, run_id="R-b", project_id=project.project_id, spec=_next_revision(task_spec)
    )
    _launch_unknown_entry(store, run_id=run_two, invocation_id="I-b", binding=binding, limits=limits)
    assert store.invocation("I-b").is_repair is True  # type: ignore[union-attr]
    before = _counters(store, binding, [run_one, run_two])
    assert (before["root_used"], before["root_repairs"]) == (2, 1)

    settlement = store.settle_by_operator(
        "I-b", settled_as="void", attested_by="op", attestation=ATTEST
    )
    assert (settlement.returned_top_level_submissions, settlement.returned_repairs) == (1, 1)
    after = _counters(store, binding, [run_one, run_two])
    assert (after["root_used"], after["root_repairs"]) == (1, 0)


def test_void_of_a_reviewer_launch_returns_one_submission_and_no_repair(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-r", project_id=project.project_id, spec=task_spec)
    reservation = _reserve(
        store, run_id=run, invocation_id="I-impl", binding=binding, limits=limits
    )
    store.mark_invocation_started("I-impl")
    store.settle_invocation("I-impl", outcome=InvocationOutcome.COMPLETED, detail="seeded")
    store.finish_attempt(
        run_id=run, attempt_id=reservation.attempt_id, state=AttemptState.SUCCEEDED,
        outcome=InvocationOutcome.COMPLETED, result={"invocation_id": "I-impl"},
    )
    store.advance_to_checking(run_id=run, attempt_id=reservation.attempt_id, phase=CheckPhase.REVIEW)
    assert _reserve(
        store, run_id=run, invocation_id="I-rev", binding=binding, limits=limits, role="reviewer"
    ).is_new
    store.mark_invocation_launch_requested("I-rev")
    store.mark_launch_unresolved("I-rev", "stop confirmed before any spawn report")
    store.set_blocked(run, RefusalCode.CANCELLED_BY_OPERATOR, "seeded")
    before = _counters(store, binding, [run])

    settlement = store.settle_by_operator(
        "I-rev", settled_as="void", attested_by="op", attestation=ATTEST
    )
    assert (settlement.returned_top_level_submissions, settlement.returned_repairs) == (1, 0)
    after = _counters(store, binding, [run])
    assert after["root_used"] == int(before["root_used"]) - 1
    assert after["root_repairs"] == before["root_repairs"]


def test_void_from_unknown_is_refused_and_writes_nothing(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-u", project_id=project.project_id, spec=task_spec)
    _unknown_entry(store, run_id=run, invocation_id="I-u", binding=binding, limits=limits)
    before = _counters(store, binding, [run])
    notes_before = store.notes_for(run)

    with pytest.raises(SettlementRefused, match="provider spend may have occurred"):
        store.settle_by_operator("I-u", settled_as="void", attested_by="op", attestation=ATTEST)

    assert store.invocation("I-u").state is InvocationStartState.UNKNOWN  # type: ignore[union-attr]
    assert store.settlement_for("I-u") is None
    assert _counters(store, binding, [run]) == before
    assert store.notes_for(run) == notes_before


@pytest.mark.parametrize("created", [True, False])
def test_a_late_spawn_report_or_result_changes_nothing_after_an_operator_settle(
    store: Store, project, task_spec: TaskSpec, project_root: Path, created: bool
) -> None:
    """Rule 4: a late fact for an old invocation neither reopens nor rewrites the entry."""
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-late", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-late", binding=binding, limits=limits)
    store.settle_by_operator("I-late", settled_as="void", attested_by="op", attestation=ATTEST)
    before = store.invocation("I-late")

    assert store.record_invocation_spawn(
        SpawnFact(invocation_id="I-late", created=created, pid=4242 if created else None,
                  spawn_kind=SpawnKind.PROCESS if created else SpawnKind.UNKNOWN,
                  detail="late report")
    ) is False
    assert store.settle_invocation(
        "I-late", outcome=InvocationOutcome.COMPLETED, detail="late result"
    ) is False
    store.mark_invocation_not_started("I-late", "late stop")
    assert store.invocation("I-late") == before
    assert store.pending_invocations(binding.root_id) == []


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_second_settle_is_refused(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-2", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-2", binding=binding, limits=limits)
    store.settle_by_operator("I-2", settled_as="void", attested_by="op", attestation=ATTEST)
    after_first = _counters(store, binding, [run])

    for settled_as in ("void", "consumed"):
        with pytest.raises(SettlementRefused, match="already operator_settled as void"):
            store.settle_by_operator(
                "I-2", settled_as=settled_as, attested_by="op", attestation=ATTEST
            )
    assert _counters(store, binding, [run]) == after_first, "a second void returns nothing"


def test_two_connections_settling_the_same_entry_commit_exactly_one(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        binding, limits = _root(store, project, task_spec, project_root)
        run = _seed_ready_run(store, run_id="R-race", project_id=project.project_id, spec=task_spec)
        _launch_unknown_entry(
            store, run_id=run, invocation_id="I-race", binding=binding, limits=limits
        )
    finally:
        store.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def settle() -> None:
        own = Store(path)
        try:
            barrier.wait()
            own.settle_by_operator(
                "I-race", settled_as="void", attested_by="op", attestation=ATTEST
            )
            results.append("ok")
        except SettlementRefused:
            results.append("refused")
        finally:
            own.close()

    threads = [threading.Thread(target=settle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "refused"]
    check = Store(path)
    try:
        assert _counters(check, binding, [run])["root_used"] == 0, "returned exactly once"
    finally:
        check.close()


@pytest.mark.parametrize("open_state", ["reserved", "requested", "started"])
def test_an_open_entry_is_refused_and_points_to_resume(
    store: Store, project, task_spec: TaskSpec, project_root: Path, open_state: str
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-open", project_id=project.project_id, spec=task_spec)
    assert _reserve(store, run_id=run, invocation_id="I-open", binding=binding, limits=limits).is_new
    if open_state in {"requested", "started"}:
        store.mark_invocation_launch_requested("I-open")
    if open_state == "started":
        store.mark_invocation_started("I-open")
    # The run ended without closing the entry (a cancelled_by_operator whose ledger write failed).
    store.set_blocked(run, RefusalCode.CANCELLED_BY_OPERATOR, "seeded")
    assert store.invocation("I-open").state.value == open_state  # type: ignore[union-attr]

    with pytest.raises(SettlementRefused, match="hflow resume"):
        store.settle_by_operator("I-open", settled_as="consumed", attested_by="op", attestation=ATTEST)
    assert store.invocation("I-open").state.value == open_state  # type: ignore[union-attr]


@pytest.mark.parametrize("final_state", ["settled", "not_started"])
def test_a_final_entry_is_refused(
    store: Store, project, task_spec: TaskSpec, project_root: Path, final_state: str
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-final", project_id=project.project_id, spec=task_spec)
    assert _reserve(store, run_id=run, invocation_id="I-final", binding=binding, limits=limits).is_new
    if final_state == "settled":
        store.mark_invocation_started("I-final")
        store.settle_invocation("I-final", outcome=InvocationOutcome.COMPLETED, detail="seeded")
    else:
        store.mark_invocation_not_started("I-final", "seeded")
    store.set_blocked(run, RefusalCode.CANCELLED_BY_OPERATOR, "seeded")
    with pytest.raises(SettlementRefused, match="already records its final fact"):
        store.settle_by_operator("I-final", settled_as="consumed", attested_by="op", attestation=ATTEST)


def test_an_entry_of_a_run_that_has_not_ended_is_refused(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """Without an owner-death proof, a live run may still have a controller driving it."""
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-live", project_id=project.project_id, spec=task_spec)
    assert _reserve(store, run_id=run, invocation_id="I-live", binding=binding, limits=limits).is_new
    store.mark_invocation_launch_requested("I-live")
    store.mark_launch_unresolved("I-live", "seeded")
    with pytest.raises(SettlementRefused, match="not ended"):
        store.settle_by_operator("I-live", settled_as="consumed", attested_by="op", attestation=ATTEST)
    assert store.invocation("I-live").state is InvocationStartState.LAUNCH_UNKNOWN  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("attestation", "match"),
    [
        ("", "blank"),
        ("   \n\t ", "blank"),
        ("x" * (ATTESTATION_MAX_CHARS + 1), "the bound is 2000"),
        ("bad\x00text", "NUL"),
    ],
    ids=["empty", "whitespace", "oversized", "nul"],
)
def test_a_blank_or_oversized_attestation_is_refused(
    store: Store, project, task_spec: TaskSpec, project_root: Path, attestation: str, match: str
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-att", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-att", binding=binding, limits=limits)
    with pytest.raises(SettlementRefused, match=match):
        store.settle_by_operator("I-att", settled_as="void", attested_by="op", attestation=attestation)
    assert store.invocation("I-att").state is InvocationStartState.LAUNCH_UNKNOWN  # type: ignore[union-attr]


def test_exactly_the_bound_is_accepted(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-max", project_id=project.project_id, spec=task_spec)
    _launch_unknown_entry(store, run_id=run, invocation_id="I-max", binding=binding, limits=limits)
    text = "y" * ATTESTATION_MAX_CHARS
    assert store.settle_by_operator(
        "I-max", settled_as="consumed", attested_by="op", attestation=text
    ).attestation == text


def test_unknown_id_and_bad_choice_are_refused(store: Store) -> None:
    with pytest.raises(InvocationNotFound):
        store.settle_by_operator("I-nope", settled_as="consumed", attested_by="op", attestation=ATTEST)
    with pytest.raises(SettlementRefused, match="'consumed' or 'void'"):
        store.settle_by_operator("I-nope", settled_as="refund", attested_by="op", attestation=ATTEST)


def test_the_schema_backs_the_rules(
    store: Store, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A writer that bypassed ``settle_by_operator`` still cannot void an unknown entry."""
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-ddl", project_id=project.project_id, spec=task_spec)
    _unknown_entry(store, run_id=run, invocation_id="I-ddl", binding=binding, limits=limits)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO invocation_settlements (settlement_id, invocation_id, run_id, root_id, "
            "prior_state, settled_as, attested_by, attested_at, attestation) "
            "VALUES ('S-x', 'I-ddl', ?, ?, 'unknown', 'void', 'op', '2026-10-03T00:00:00Z', 'x')",
            (run, binding.root_id),
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli_ledger(tmp_path: Path, project, task_spec: TaskSpec, project_root: Path) -> Path:
    data_dir = tmp_path / "cli-data"
    store = Store(data_dir / "hflow.sqlite")
    try:
        binding, limits = _root(store, project, task_spec, project_root)
        run_u = _seed_ready_run(store, run_id="R-cu", project_id=project.project_id, spec=task_spec)
        _unknown_entry(store, run_id=run_u, invocation_id="I-cu", binding=binding, limits=limits)
    finally:
        store.close()
    return data_dir


def test_cli_exit_codes_and_output(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path, capsys
) -> None:
    data_dir = _cli_ledger(tmp_path, project, task_spec, project_root)
    base = ["--data-dir", str(data_dir), "ledger", "settle"]

    # usage: --attest is required, --as has two choices
    with pytest.raises(SystemExit) as missing:
        main([*base, "I-cu"])
    assert missing.value.code == EXIT_USAGE
    with pytest.raises(SystemExit) as bad_choice:
        main([*base, "I-cu", "--as", "refund", "--attest", ATTEST])
    assert bad_choice.value.code == EXIT_USAGE
    capsys.readouterr()

    # unknown invocation id: usage, like an unknown run
    assert main([*base, "I-missing", "--attest", ATTEST]) == EXIT_USAGE
    assert "unknown invocation" in capsys.readouterr().err

    # refused: void on unknown, blank attestation
    assert main([*base, "I-cu", "--as", "void", "--attest", ATTEST]) == EXIT_REFUSED
    assert "refused:" in capsys.readouterr().err
    assert main([*base, "I-cu", "--attest", "   "]) == EXIT_REFUSED
    capsys.readouterr()

    # success: consumed is the default
    assert main([*base, "I-cu", "--attest", ATTEST]) == EXIT_OK
    out = capsys.readouterr().out
    assert "as consumed, by operator attestation (not observed)" in out
    assert "prior state   unknown -> operator_settled" in out
    assert "returned      nothing" in out
    assert "not an observation" in out
    assert "attested_by" in out and "recorded, not authenticated" in out

    # resolve once
    assert main([*base, "I-cu", "--attest", ATTEST]) == EXIT_REFUSED
    assert "already operator_settled" in capsys.readouterr().err


def test_cli_void_json_reports_the_returned_counters(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path, capsys
) -> None:
    data_dir = tmp_path / "cli-void"
    store = Store(data_dir / "hflow.sqlite")
    try:
        binding, limits = _root(store, project, task_spec, project_root)
        run = _seed_ready_run(store, run_id="R-cv", project_id=project.project_id, spec=task_spec)
        _launch_unknown_entry(store, run_id=run, invocation_id="I-cv", binding=binding, limits=limits)
    finally:
        store.close()
    code = main([
        "ledger", "settle", "I-cv", "--as", "void", "--attest", ATTEST, "--json",
        "--data-dir", str(data_dir),
    ])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["settlement"]["settled_as"] == "void"
    assert payload["settlement"]["basis"] == "operator_attested"
    assert payload["settlement"]["returned_top_level_submissions"] == 1
    assert payload["root_used_top_level_submissions"] == 0
    assert "not an observation" in payload["note"]


def test_status_report_and_doctor_say_not_observed(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path, capsys
) -> None:
    data_dir = _cli_ledger(tmp_path, project, task_spec, project_root)
    assert main(["--data-dir", str(data_dir), "ledger", "settle", "I-cu", "--attest", ATTEST]) == 0
    capsys.readouterr()

    assert main(["status", "R-cu", "--data-dir", str(data_dir),
                 "--project-root", str(project_root)]) == EXIT_OK
    status = capsys.readouterr().out
    assert "operator_settled=1 (by attestation, not observed)" in status
    assert "settled by operator attestation (not observed): unknown -> operator_settled" in status
    assert f"attestation: {ATTEST}" in status
    assert "billed usage: unknown" in status
    assert "state         BLOCKED" in status, "the run's state is unchanged"

    assert main(["report", "R-cu", "--json", "--data-dir", str(data_dir),
                 "--project-root", str(project_root)]) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    (settlement,) = report["invocation_settlements"]
    assert settlement["basis"] == "operator_attested"
    assert settlement["attestation"] == ATTEST
    counts = report["invocation_counts"]
    assert counts["operator_settled"] == 1
    assert counts["unknown"] == 0
    assert counts["processes"] == 1, "the process fact recorded before is kept, not added to"
    assert report["model_calls_made"] == 0

    assert main(["doctor", "--json", "--data-dir", str(data_dir)]) in {0, 3, 4, 5}
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ledger_settlements"]["consumed"] == 1
    assert doctor["ledger_settlements"]["void"] == 0
    assert "not observed" in doctor["ledger_settlements"]["detail"]


# --------------------------------------------------------------------------
# storage migration
# --------------------------------------------------------------------------


#: The last storage version without ``invocation_settlements`` (v7 added it).
_PRE_SETTLEMENT_VERSION = 6


def _strip_to_pre_settlement_shape(connection: sqlite3.Connection) -> None:
    """Drop what v7 and every later version added, so the file is a genuine v6 shape."""
    connection.execute("DROP TABLE IF EXISTS integrations")
    connection.execute("DROP TABLE invocation_settlements")


def test_migration_to_the_settlement_version_keeps_rows(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    """A ledger from before the settlement table (v6) migrates with every row intact."""
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        binding, limits = _root(store, project, task_spec, project_root)
        run = _seed_ready_run(store, run_id="R-mig", project_id=project.project_id, spec=task_spec)
        _unknown_entry(store, run_id=run, invocation_id="I-mig", binding=binding, limits=limits)
        before = _counters(store, binding, [run])
        intent_before = store.invocation("I-mig")
    finally:
        store.close()

    previous = _PRE_SETTLEMENT_VERSION
    connection = sqlite3.connect(str(path))
    try:
        _strip_to_pre_settlement_shape(connection)
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'storage_version'", (str(previous),)
        )
        connection.commit()
    finally:
        connection.close()

    reopened = Store(path)
    try:
        assert reopened.storage_version == migrate.STORAGE_VERSION
        assert reopened.migrated_from == previous
        assert reopened.migration_backup == migrate.backup_path_for(path, previous)
        assert reopened.migration_backup.exists()
        assert _counters(reopened, binding, [run]) == before
        assert reopened.invocation("I-mig") == intent_before, "the unknown entry is not rewritten"
        assert reopened.settlements_for(run) == []
        # Still blocking until an operator settles it with this build.
        assert reopened.pending_invocations(binding.root_id)[0].invocation_id == "I-mig"
        reopened.settle_by_operator(
            "I-mig", settled_as="consumed", attested_by="op", attestation=ATTEST
        )
        assert reopened.pending_invocations(binding.root_id) == []
    finally:
        reopened.close()


def test_a_failed_settlement_migration_rolls_back(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    Store(path).close()
    previous = _PRE_SETTLEMENT_VERSION
    connection = sqlite3.connect(str(path))
    try:
        _strip_to_pre_settlement_shape(connection)
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'storage_version'", (str(previous),)
        )
        connection.commit()
    finally:
        connection.close()

    def boom(step: str) -> None:
        if step.startswith("v7:"):
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        Store(path, on_migration_step=boom)
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'storage_version'"
        ).fetchone()[0] == str(previous)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'invocation_settlements'"
        ).fetchone() is None
    finally:
        connection.close()


# --------------------------------------------------------------------------
# rule 4: a late spawn report after a settlement leaves a note, nothing else
# --------------------------------------------------------------------------


def test_a_late_spawn_report_through_the_controller_notes_the_settlement_and_changes_nothing(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script
) -> None:
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-latec", project_id=project.project_id, spec=task_spec)
    reservation = _reserve(
        store, run_id=run, invocation_id="I-latec", binding=binding, limits=limits
    )
    assert reservation.is_new
    assert store.mark_invocation_launch_requested("I-latec") is True
    store.mark_launch_unresolved("I-latec", "stop confirmed before any spawn report")
    store.set_blocked(run, RefusalCode.CANCELLED_BY_OPERATOR, "seeded launch_unknown")
    store.settle_by_operator("I-latec", settled_as="void", attested_by="op", attestation=ATTEST)
    before = store.invocation("I-latec")
    counters = _counters(store, binding, [run])
    state = store.get_run(run)["task_state"]

    report = _controller(store, project_root, fake_script)._spawn_reporter(reservation)
    report(SpawnFact(invocation_id="I-latec", created=True, pid=4242,
                     spawn_kind=SpawnKind.PROCESS, detail="late report"))

    assert store.invocation("I-latec") == before
    assert _counters(store, binding, [run]) == counters
    assert store.get_run(run)["task_state"] == state
    assert any(
        "a spawn report (created=True, pid=4242) arrived after an operator settlement; the "
        "settlement stands" in note
        for note in store.notes_for(run)
    )


# --------------------------------------------------------------------------
# settle refuses while the run's owner or the entry's child may be alive
# --------------------------------------------------------------------------


def _ended_owned_entry(
    store: Store,
    run_request,
    project_root: Path,
    *,
    token: str,
    identity: ownership.ProcessIdentity,
    child_pid: int | None,
) -> tuple[str, str]:
    """An owned run whose launch was reported (with ``child_pid``) and never observed, ended
    BLOCKED outcome_unknown: what a cross-process cancel with an unconfirmed stop leaves once
    `hflow resume` has reconciled it. The owner token stays the original controller's."""
    run_id, invocation_id = _seed_owned_dispatch(
        store, run_request=run_request, project_root=project_root, token=token,
        identity=identity, child_pid=child_pid, launched=True,
    )
    store.settle_invocation(invocation_id, outcome=None, detail="no observable result")
    store.set_blocked(run_id, RefusalCode.OUTCOME_UNKNOWN, "stop not confirmed")
    entry = store.invocation(invocation_id)
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN
    assert store.get_run(run_id)["owner_token"] == token
    return run_id, invocation_id


def _cli_settle(store: Store, invocation_id: str) -> int:
    return main([
        "--data-dir", str(store.path.parent), "ledger", "settle", invocation_id,
        "--attest", ATTEST,
    ])


def _assert_refused_unchanged(store: Store, invocation_id: str, code: int, capsys) -> str:
    err = capsys.readouterr().err
    assert code == EXIT_REFUSED, err
    assert err.startswith("refused: ") and "nothing was written" in err
    entry = store.invocation(invocation_id)
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN
    assert store.settlement_for(invocation_id) is None
    return err


def _stop(child) -> None:
    child.stdin.close()
    child.wait(30)


def _gone_child_pid() -> int:
    child = _start_idle_child()
    _stop(child)
    return child.pid


@windows_only
def test_settle_is_refused_while_the_owner_lock_is_held(
    store: Store, run_request, project_root: Path, capsys
) -> None:
    token = ownership.new_owner_token()
    _run, inv = _ended_owned_entry(
        store, run_request, project_root, token=token, identity=_dead_identity(),
        child_pid=_gone_child_pid(),
    )
    lock = ownership.OwnerLock(ownership.lock_path_for(store.path.parent, token))
    lock.acquire()
    try:
        err = _assert_refused_unchanged(store, inv, _cli_settle(store, inv), capsys)
    finally:
        lock.release()
    assert "may still be alive" in err and "lock=held" in err
    assert "hflow resume" in err


@windows_only
def test_settle_is_refused_while_the_owner_identity_matches(
    store: Store, run_request, project_root: Path, capsys
) -> None:
    _run, inv = _ended_owned_entry(
        store, run_request, project_root, token=ownership.new_owner_token(),
        identity=ownership.current_identity(), child_pid=_gone_child_pid(),
    )
    err = _assert_refused_unchanged(store, inv, _cli_settle(store, inv), capsys)
    assert "may still be alive" in err and "identity=matching" in err


@windows_only
def test_settle_is_refused_while_the_entrys_child_matches(
    store: Store, run_request, project_root: Path, capsys
) -> None:
    child = _start_idle_child()
    try:
        _run, inv = _ended_owned_entry(
            store, run_request, project_root, token=ownership.new_owner_token(),
            identity=_dead_identity(), child_pid=child.pid,
        )
        err = _assert_refused_unchanged(store, inv, _cli_settle(store, inv), capsys)
    finally:
        _stop(child)
    assert f"child process pid={child.pid}" in err and "may still be running" in err


@windows_only
def test_settle_is_refused_when_the_childs_probe_is_unknown(
    store: Store, run_request, project_root: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _start_idle_child()
    try:
        _run, inv = _ended_owned_entry(
            store, run_request, project_root, token=ownership.new_owner_token(),
            identity=_dead_identity(), child_pid=child.pid,
        )
        real = ownership._open_process
        monkeypatch.setattr(
            ownership,
            "_open_process",
            lambda pid: (None, ownership.ERROR_ACCESS_DENIED) if pid == child.pid else real(pid),
        )
        err = _assert_refused_unchanged(store, inv, _cli_settle(store, inv), capsys)
    finally:
        _stop(child)
    assert "cannot be judged from here" in err
    assert f"Win32 error {ownership.ERROR_ACCESS_DENIED}" in err


@windows_only
def test_settle_is_refused_when_the_owner_probe_is_unknown(
    store: Store, run_request, project_root: Path, capsys
) -> None:
    elsewhere = dataclasses.replace(_dead_identity(), host="another-host-entirely")
    _run, inv = _ended_owned_entry(
        store, run_request, project_root, token=ownership.new_owner_token(), identity=elsewhere,
        child_pid=None,
    )
    err = _assert_refused_unchanged(store, inv, _cli_settle(store, inv), capsys)
    assert "identity=unknown" in err


@windows_only
def test_settle_is_allowed_when_the_owner_and_the_child_are_gone(
    store: Store, run_request, project_root: Path, capsys
) -> None:
    _run, inv = _ended_owned_entry(
        store, run_request, project_root, token=ownership.new_owner_token(),
        identity=_dead_identity(), child_pid=_gone_child_pid(),
    )
    assert _cli_settle(store, inv) == EXIT_OK, capsys.readouterr().err
    assert "prior state   unknown -> operator_settled" in capsys.readouterr().out
    entry = store.invocation(inv)
    assert entry is not None and entry.state is InvocationStartState.OPERATOR_SETTLED


@windows_only
def test_settle_of_a_run_with_no_owner_token_still_refuses_a_matching_child(
    store: Store, project, task_spec: TaskSpec, project_root: Path, capsys
) -> None:
    """A label-only (pre-v6 style) claim has no lock to examine; the child rule still applies."""
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-legacy", project_id=project.project_id, spec=task_spec)
    assert store.get_run(run)["owner_token"] is None
    assert _reserve(
        store, run_id=run, invocation_id="I-legacy", binding=binding, limits=limits
    ).is_new
    assert store.mark_invocation_launch_requested("I-legacy") is True
    child = _start_idle_child()
    try:
        assert store.record_invocation_spawn(
            SpawnFact(invocation_id="I-legacy", created=True, pid=child.pid,
                      spawn_kind=SpawnKind.PROCESS, detail="legacy spawn")
        ) is True
        store.settle_invocation("I-legacy", outcome=None, detail="no observable result")
        store.set_blocked(run, RefusalCode.OUTCOME_UNKNOWN, "stop not confirmed")
        err = _assert_refused_unchanged(store, "I-legacy", _cli_settle(store, "I-legacy"), capsys)
        assert "may still be running" in err
    finally:
        _stop(child)
    assert _cli_settle(store, "I-legacy") == EXIT_OK, capsys.readouterr().err


def test_a_pre_v6_run_with_a_recorded_controller_settles_only_on_an_explicit_attestation(
    store: Store, project, task_spec: TaskSpec, project_root: Path, capsys
) -> None:
    """A controller pid recorded before owner identity has no host: HFlow cannot judge it.

    Settle refuses and names the way out; ``--legacy-owner-gone`` makes the operator's claim
    part of the recorded attestation instead of HFlow inferring it.
    """
    binding, limits = _root(store, project, task_spec, project_root)
    run = _seed_ready_run(store, run_id="R-legacy", project_id=project.project_id, spec=task_spec)
    assert store.get_run(run)["owner_token"] is None
    _unknown_entry(store, run_id=run, invocation_id="I-legacy", binding=binding, limits=limits)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE attempts SET process_id = ?, process_started_at = ? WHERE run_id = ?",
            (4194300, "2000-01-01T00:00:00Z", run),
        )

    err = _assert_refused_unchanged(store, "I-legacy", _cli_settle(store, "I-legacy"), capsys)
    assert "--legacy-owner-gone" in err and "no host" in err, err

    code = main([
        "--data-dir", str(store.path.parent), "ledger", "settle", "I-legacy",
        "--attest", ATTEST, "--legacy-owner-gone",
    ])
    assert code == EXIT_OK, capsys.readouterr().err
    settlement = store.settlement_for("I-legacy")
    assert settlement is not None
    assert "operator attests the pre-v6 owner of this run is gone" in settlement.attestation
