"""Test-only shortcuts to a ledger state. None of these is a production path.

The controller claims a run with an owner identity (``Store.claim_run_owned``), buys every dispatch
through ``Store.reserve_dispatch`` and takes launch facts from a driver's report
(``Store.record_invocation_spawn``). A store-level test often needs the state those leave without
driving a controller; these write it directly, or through the production calls where one exists.
"""

from __future__ import annotations

import sqlite3

from hflow.contracts import (
    EffectiveConfig,
    InvocationIntent,
    InvocationSettlement,
    InvocationStartState,
    SpawnKind,
)
from hflow.ids import utc_now
from hflow.store import _EFFECTIVE_CONFIG_PREFIX, RunNotFound, Store, _model_from_row


def pending_invocations(store: Store, root_id: str) -> list[InvocationIntent]:
    """The root's ledger entries that are still pending, oldest first."""
    return [entry for entry in store.invocations_for_root(root_id) if entry.pending]


def turns_remaining(store: Store, run_id: str) -> int:
    """The run's turn ceiling minus its reserved turns (the generated column)."""
    return int(store.get_run(run_id)["turns_remaining"])


def record_effective_config(store: Store, run_id: str, config: EffectiveConfig) -> None:
    """Record a run's effective configuration after its creation, through the store's own rule.

    Production records it in ``create_run``'s insert; this is the same write-once note, for a
    test of what a second, different configuration does.
    """
    with store.transaction() as conn:
        store._record_structured_note_locked(  # noqa: SLF001
            conn, run_id, _EFFECTIVE_CONFIG_PREFIX, config
        )


def claim_run(store: Store, run_id: str, controller_id: str) -> bool:
    """A label-only claim, with no owner identity: what a run claimed before the owner lease has.

    Refused (``False``) for a run another label holds or that a process owner holds.
    """
    now = utc_now()
    with store.transaction() as conn:
        cur = conn.execute(
            "UPDATE runs SET claimed_by = ?, claimed_at = ?, updated_at = ? "
            "WHERE run_id = ? AND (claimed_by IS NULL OR claimed_by = ?) AND owner_token IS NULL",
            (controller_id, now, now, run_id, controller_id),
        )
        if cur.rowcount == 1:
            return True
        if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
            raise RunNotFound(run_id)
        return False


def reserve_turn(store: Store, run_id: str, controller_id: str, *, turns: int = 1) -> None:
    """The run's own turn gate (the one ``reserve_dispatch`` applies), in its own transaction."""
    with store.transaction():
        store._reserve_turn_locked(run_id, controller_id, turns=turns)  # noqa: SLF001


def dispatch_attempt(
    store: Store,
    *,
    run_id: str,
    controller_id: str,
    attempt_id: str,
    invocation_id: str,
    reservation_id: str = "B-test",
    reserved_turns: int = 1,
    reservation_expires_at: str = "2999-01-01T00:00:00Z",
) -> sqlite3.Row:
    """A rootless implementer dispatch through ``reserve_dispatch``, its invocation registered.

    The rootless path writes no invocation row; the implementer's invocation id goes on the
    attempt through the controller's own stop-aware registration. Returns the attempt row.
    """
    store.reserve_dispatch(
        run_id=run_id,
        controller_id=controller_id,
        invocation_id=invocation_id,
        role="implementer",
        reservation_id=reservation_id,
        reserved_turns=reserved_turns,
        reservation_expires_at=reservation_expires_at,
        attempt_id=attempt_id,
    )
    assert store.register_attempt_invocation_unless_stopped(
        run_id, attempt_id, column="invocation_id", invocation_id=invocation_id
    )
    return next(row for row in store.attempts_for(run_id) if row["attempt_id"] == attempt_id)


def mark_invocation_started(
    store: Store, invocation_id: str, *, process_created: bool = True, pid: int | None = None
) -> bool:
    """Record a launch from ``reserved``/``requested`` without a driver's spawn report.

    ``process_created`` decides the process fact: ``False`` records a launch that created no
    child (``no_process``).
    """
    now = utc_now()
    with store.transaction() as conn:
        cur = conn.execute(
            """
            UPDATE invocations
               SET state = ?, started_at = ?,
                   launch_requested_at = COALESCE(launch_requested_at, ?),
                   process_started_at = COALESCE(?, process_started_at),
                   process_pid = COALESCE(?, process_pid),
                   spawn_kind = ?
             WHERE invocation_id = ? AND state IN (?, ?)
            """,
            (
                InvocationStartState.STARTED.value,
                now,
                now,
                now if process_created else None,
                pid,
                SpawnKind.PROCESS.value if process_created else SpawnKind.NO_PROCESS.value,
                invocation_id,
                InvocationStartState.RESERVED.value,
                InvocationStartState.REQUESTED.value,
            ),
        )
        return cur.rowcount == 1


def claim_authorized_submission(store: Store, authorization_id: str) -> int:
    """Spend one submission of an authorization through its own gate; return the used count."""
    with store.transaction() as conn:
        store._check_authorization_locked(conn, authorization_id, None)  # noqa: SLF001
        store._claim_authorization_locked(conn, authorization_id)  # noqa: SLF001
        row = conn.execute(
            "SELECT used_top_level_submissions FROM authorizations WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        return int(row["used_top_level_submissions"])


def settlement_for(store: Store, invocation_id: str) -> InvocationSettlement | None:
    """The operator settlement recorded for one ledger entry, if any."""
    row = store._fetchone(  # noqa: SLF001
        "SELECT * FROM invocation_settlements WHERE invocation_id = ?", (invocation_id,)
    )
    return _model_from_row(InvocationSettlement, row) if row is not None else None
