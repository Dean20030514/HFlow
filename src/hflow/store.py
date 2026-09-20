"""SQLite: the single authoritative store for runtime state (plan 9.2).

Design rules enforced here, not by convention:

* Budget reservation and the state transition that justifies it happen in the
  *same* transaction. ``SQLITE_CONSTRAINT`` / ``OperationalError`` inside that
  transaction rolls back both.
* A database CHECK constraint makes it impossible to reserve more turns than a
  run's ceiling, even if a caller's logic is wrong.
* Dispatch intent (attempt row + reservation) is durable *before* any external
  process starts, so a crash between "process started" and "result stored" is
  recoverable as ``OUTCOME_UNKNOWN`` instead of a silent re-run.
* Result application is a compare-and-set on ``(task_revision, current_attempt_id)``,
  so a late result from an old attempt cannot overwrite a newer one.
* Large outputs are not stored: evidence keeps content hashes plus a bounded
  excerpt, so the database never becomes a log dump.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import (
    AttemptRecord,
    AttemptState,
    CancellationReceipt,
    CheckPhase,
    DeliveryState,
    EvidenceRecord,
    EvidenceStatus,
    InvocationOutcome,
    RefusalCode,
    ResultReceipt,
    RunSummary,
    TaskSpec,
    TaskState,
    canonical_json,
    digest_of,
    json_schema,
)
from .ids import new_evidence_id, utc_now

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id                TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    task_id               TEXT NOT NULL,
    schema_version        INTEGER NOT NULL,
    spec_digest           TEXT NOT NULL,
    task_spec_json        TEXT NOT NULL,
    task_revision         INTEGER NOT NULL,
    task_state            TEXT NOT NULL,
    phase                 TEXT,
    delivery_state        TEXT NOT NULL DEFAULT 'NONE',
    claimed_by            TEXT,
    claimed_at            TEXT,
    current_attempt_id    TEXT,
    controller_build      TEXT NOT NULL,
    checks_digest         TEXT NOT NULL,
    turn_limit            INTEGER NOT NULL,
    repair_limit          INTEGER NOT NULL,
    turns_reserved        INTEGER NOT NULL DEFAULT 0,
    repairs_used          INTEGER NOT NULL DEFAULT 0,
    turns_observed        INTEGER,
    turns_remaining       INTEGER GENERATED ALWAYS AS (turn_limit - turns_reserved) VIRTUAL,
    block_code            TEXT,
    block_reason          TEXT,
    receipt_json          TEXT,
    cancel_intent_at      TEXT,
    cancel_receipt_json   TEXT,
    worktree_path         TEXT,
    worktree_state        TEXT NOT NULL DEFAULT 'NONE',
    cleanup_intent_at     TEXT,
    cleanup_done_at       TEXT,
    cleanup_error         TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (turns_reserved >= 0),
    CHECK (turns_reserved <= turn_limit),
    CHECK (repairs_used >= 0),
    CHECK (repairs_used <= repair_limit)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_spec_digest
    ON runs (project_id, spec_digest);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id            TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(run_id),
    task_revision         INTEGER NOT NULL,
    role                  TEXT NOT NULL,
    state                 TEXT NOT NULL,
    reservation_id        TEXT,
    reserved_agent_turns  INTEGER NOT NULL DEFAULT 0,
    reserved_expires_at   TEXT,
    process_id            INTEGER,
    process_started_at    TEXT,
    process_identity      TEXT,
    session_id            TEXT,
    invocation_id         TEXT,
    review_invocation_id  TEXT,
    outcome               TEXT,
    result_json           TEXT,
    result_digest         TEXT,
    review_json           TEXT,
    reconcile_json        TEXT,
    block_code            TEXT,
    created_at            TEXT NOT NULL,
    finished_at           TEXT,
    UNIQUE (run_id, task_revision, role)
);

CREATE INDEX IF NOT EXISTS ix_attempts_run ON attempts (run_id, created_at);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id           TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(run_id),
    attempt_id            TEXT NOT NULL REFERENCES attempts(attempt_id),
    kind                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    check_id              TEXT,
    candidate_fingerprint TEXT NOT NULL,
    checks_digest         TEXT NOT NULL,
    command_json          TEXT NOT NULL DEFAULT '[]',
    exit_code             INTEGER,
    stdout_digest         TEXT NOT NULL DEFAULT '',
    stderr_digest         TEXT NOT NULL DEFAULT '',
    detail                TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_evidence_run ON evidence (run_id, kind, check_id);

CREATE TABLE IF NOT EXISTS run_notes (
    note_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_run_notes_run ON run_notes (run_id, created_at);

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id   TEXT PRIMARY KEY,
    mode               TEXT NOT NULL,
    binding_digest     TEXT NOT NULL,
    user_text          TEXT NOT NULL,
    provided_by        TEXT NOT NULL,
    authorized_at      TEXT NOT NULL,
    max_top_level_submissions INTEGER NOT NULL,
    used_top_level_submissions INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    CHECK (used_top_level_submissions >= 0),
    CHECK (used_top_level_submissions <= max_top_level_submissions)
);
"""


class StoreError(RuntimeError):
    pass


class RunNotFound(StoreError):
    pass


class Store:
    """Thin, explicit SQLite wrapper. No ORM, no implicit commits.

    Connections are shared across threads (the controller can be cancelling while a
    background thread drives a run), so every statement and transaction is serialized by a
    re-entrant lock. That is stronger than relying on SQLite's own serialized threading mode:
    it also makes multi-statement transactions atomic with respect to sibling threads
    instead of interleaving with them.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            # WAL keeps a reader (`status`) from blocking the writer (`run`).
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        with self.transaction(autocommit=True):
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('contracts_schema_digest', ?)",
                (
                    digest_of(
                        {
                            name: json_schema(model)
                            for name, model in (
                                ("RunSummary", RunSummary),
                                ("AttemptRecord", AttemptRecord),
                                ("EvidenceRecord", EvidenceRecord),
                                ("ResultReceipt", ResultReceipt),
                                ("TaskSpec", TaskSpec),
                            )
                        }
                    ),
                ),
            )

    # -- transaction helpers -------------------------------------------------

    @contextmanager
    def transaction(self, *, autocommit: bool = False) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT/ROLLBACK around one logical state change.

        ``autocommit=True`` is used only for schema bootstrap, because
        ``executescript`` would itself commit a surrounding explicit transaction.
        """
        with self._lock:
            if autocommit:
                before = self.conn.in_transaction
                try:
                    yield self.conn
                except BaseException:
                    if self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise
                if self.conn.in_transaction and not before:
                    self.conn.execute("COMMIT")
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """Locked single-row read. Every query goes through a locked helper."""
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- runs ----------------------------------------------------------------

    def find_run_by_spec_digest(self, project_id: str, spec_digest: str) -> sqlite3.Row | None:
        return self._fetchone(
            "SELECT * FROM runs WHERE project_id = ? AND spec_digest = ?",
            (project_id, spec_digest),
        )

    def create_run(
        self,
        *,
        run_id: str,
        project_id: str,
        spec: TaskSpec,
        spec_digest: str,
        controller_build: str,
        checks_digest: str,
        turn_limit: int,
        repair_limit: int,
    ) -> sqlite3.Row:
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? AND spec_digest = ?",
                (project_id, spec_digest),
            ).fetchone()
            if existing is not None:
                return existing
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, task_id, schema_version, spec_digest, task_spec_json,
                    task_revision, task_state, phase, delivery_state,
                    controller_build, checks_digest, turn_limit, repair_limit,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    spec.task_id,
                    spec.schema_version,
                    spec_digest,
                    canonical_json(spec.model_dump(mode="json")),
                    spec.revision,
                    TaskState.DRAFT.value,
                    DeliveryState.NONE.value,
                    controller_build,
                    checks_digest,
                    turn_limit,
                    repair_limit,
                    now,
                    now,
                ),
            )
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def get_run(self, run_id: str) -> sqlite3.Row:
        row = self._fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise RunNotFound(run_id)
        return row

    def claim_run(self, run_id: str, controller_id: str) -> bool:
        """Transactionally claim exclusive scheduling ownership (acceptance A02)."""
        now = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET claimed_by = ?, claimed_at = ?, updated_at = ?
                 WHERE run_id = ? AND (claimed_by IS NULL OR claimed_by = ?)
                """,
                (controller_id, now, now, run_id, controller_id),
            )
            if cur.rowcount == 1:
                return True
            row = conn.execute("SELECT claimed_by FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            return False

    def set_task_state(
        self,
        run_id: str,
        expected_states: Sequence[TaskState],
        new_state: TaskState,
        *,
        phase: CheckPhase | None = None,
        clear_phase: bool = False,
        delivery_state: DeliveryState | None = None,
        idempotent: bool = False,
    ) -> sqlite3.Row:
        """Guarded state transition; raises if the run is not in an expected state.

        ``idempotent=True`` treats "already in ``new_state``" as success, which lets a
        resumed or re-entered drive step record intent without inventing a transition.
        """
        placeholders = ",".join("?" for _ in expected_states)
        now = utc_now()
        with self.transaction() as conn:
            if idempotent:
                current = conn.execute(
                    "SELECT task_state FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if current is None:
                    raise RunNotFound(run_id)
                if current["task_state"] == new_state.value:
                    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assignments = ["task_state = ?", "updated_at = ?"]
            params: list[Any] = [new_state.value, now]
            if clear_phase:
                assignments.append("phase = NULL")
            else:
                assignments.append("phase = ?")
                params.append(phase.value if phase else None)
            if delivery_state is not None:
                assignments.append("delivery_state = ?")
                params.append(delivery_state.value)
            params.extend([run_id, *[state.value for state in expected_states]])
            cur = conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} "
                f"WHERE run_id = ? AND task_state IN ({placeholders})",
                params,
            )
            if cur.rowcount != 1:
                row = conn.execute("SELECT task_state FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if row is None:
                    raise RunNotFound(run_id)
                raise StoreError(
                    f"illegal transition for {run_id}: state is {row['task_state']}, "
                    f"expected one of {[s.value for s in expected_states]}"
                )
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def set_blocked(self, run_id: str, code: RefusalCode, reason: str) -> sqlite3.Row:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET task_state = ?, block_code = ?, block_reason = ?, updated_at = ?
                 WHERE run_id = ?
                """,
                (TaskState.BLOCKED.value, code.value, reason, now, run_id),
            )
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def save_receipt(self, run_id: str, receipt: ResultReceipt) -> None:
        """Persist a receipt without touching state.

        Prefer :meth:`finalize_acceptance`; this exists for the rare case of repairing a
        receipt on an already terminal run, and callers must still go through admission.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET receipt_json = ?, updated_at = ? WHERE run_id = ?",
                (canonical_json(receipt.model_dump(mode="json")), utc_now(), run_id),
            )

    def set_turns_observed(self, run_id: str, turns: int | None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET turns_observed = ?, updated_at = ? WHERE run_id = ?",
                (turns, utc_now(), run_id),
            )

    def finalize_acceptance(
        self, run_id: str, receipt: ResultReceipt, *, checks_digest: str
    ) -> None:
        """Accept a run atomically: state change and receipt in the same transaction.

        Refuses if the approved checks changed while the run was executing, which
        would mean the evidence was produced under a different command set
        (acceptance A11), and refuses if a cancellation intent was recorded after the
        result arrived: an accepted cancellation must not be overwritten by a late
        success (the mirror of the stale-attempt rule).
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT task_state, checks_digest, cancel_intent_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["checks_digest"] != checks_digest:
                raise StoreError(
                    f"run {run_id} was admitted under checks digest {row['checks_digest']} but the "
                    f"project contract now provides {checks_digest}; evidence would be stale"
                )
            if row["task_state"] != TaskState.CHECKING.value:
                raise StoreError(
                    f"run {run_id} is {row['task_state']}, not CHECKING; refusing to accept"
                )
            if row["cancel_intent_at"]:
                raise StoreError(
                    f"run {run_id} has a recorded cancellation intent at {row['cancel_intent_at']}; "
                    "a late success cannot overwrite it"
                )
            conn.execute(
                "UPDATE runs SET receipt_json = ?, updated_at = ? WHERE run_id = ?",
                (canonical_json(receipt.model_dump(mode="json")), utc_now(), run_id),
            )
            cur = conn.execute(
                """
                UPDATE runs
                   SET task_state = ?, phase = NULL, delivery_state = ?,
                       block_code = NULL, block_reason = NULL, updated_at = ?
                 WHERE run_id = ? AND task_state = ?
                """,
                (
                    TaskState.ACCEPTED.value,
                    DeliveryState.LOCAL_CANDIDATE.value,
                    utc_now(),
                    run_id,
                    TaskState.CHECKING.value,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id} left CHECKING during acceptance; no receipt written")

    # -- budget --------------------------------------------------------------

    def reserve_turn(
        self,
        run_id: str,
        controller_id: str,
        *,
        turns: int = 1,
    ) -> None:
        """Atomically reserve budget for the next dispatch (plan 10.3, acceptance A03)."""
        with self.transaction():
            self._reserve_turn_locked(run_id, controller_id, turns=turns)

    def _reserve_turn_locked(
        self,
        run_id: str,
        controller_id: str,
        *,
        turns: int = 1,
    ) -> None:
        """The actual gate. Callers must already hold a transaction.

        The UPDATE is the gate: it succeeds only while the caller still owns the
        run, the run is not terminal, and the ceiling is not exceeded. The CHECK
        constraint is the backstop.
        """
        now = utc_now()
        terminal = (TaskState.ACCEPTED.value, TaskState.BLOCKED.value, TaskState.CANCELLED.value)
        cur = self.conn.execute(
            """
            UPDATE runs
               SET turns_reserved = turns_reserved + ?, updated_at = ?
             WHERE run_id = ?
               AND claimed_by = ?
               AND task_state NOT IN (?, ?, ?)
               AND turns_reserved + ? <= turn_limit
            """,
            (turns, now, run_id, controller_id, *terminal, turns),
        )
        if cur.rowcount == 1:
            return
        row = self.get_run(run_id)
        if row["claimed_by"] != controller_id:
            raise StoreError(
                f"cannot reserve budget for {run_id}: claimed by {row['claimed_by']!r}, "
                f"not {controller_id!r}"
            )
        raise StoreError(
            f"budget exhausted for {run_id}: {row['turns_reserved']}/{row['turn_limit']} turns reserved"
        )

    def release_reservation(self, run_id: str, turns: int) -> None:
        """Give budget back only when a dispatch provably never reached a model."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET turns_reserved = turns_reserved - ?, updated_at = ?
                 WHERE run_id = ? AND turns_reserved >= ?
                """,
                (turns, utc_now(), run_id, turns),
            )
            if cur.rowcount != 1:
                raise StoreError(f"cannot release {turns} turn(s) for {run_id}: underflow")

    def turns_remaining(self, run_id: str) -> int:
        row = self.get_run(run_id)
        return int(row["turns_remaining"])

    # -- attempts ------------------------------------------------------------

    def open_attempt(self, run_id: str) -> sqlite3.Row | None:
        """The newest attempt, whatever its state (used for recovery decisions)."""
        return self._fetchone(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (run_id,),
        )

    def attempt_by_invocation(self, invocation_id: str) -> sqlite3.Row | None:
        return self._fetchone(
            "SELECT * FROM attempts WHERE invocation_id = ?", (invocation_id,)
        )

    def dispatch_attempt(
        self,
        *,
        run_id: str,
        controller_id: str,
        attempt_id: str,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
    ) -> sqlite3.Row:
        """Reserve budget **and** record dispatch intent in one transaction.

        This is the only sanctioned way to start an attempt. If anything inside the
        transaction fails, both the reservation and the attempt disappear: there is
        no state where budget was spent on a dispatch that never happened.
        """
        with self.transaction():
            self._reserve_turn_locked(run_id, controller_id, turns=reserved_turns)
            row = self.get_run(run_id)
            return self.create_attempt(
                attempt_id=attempt_id,
                run_id=run_id,
                task_revision=int(row["task_revision"]),
                role=role,
                reservation_id=reservation_id,
                reserved_turns=reserved_turns,
                reservation_expires_at=reservation_expires_at,
                in_transaction=True,
            )

    def create_attempt(
        self,
        *,
        attempt_id: str,
        run_id: str,
        task_revision: int,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
        in_transaction: bool = False,
    ) -> sqlite3.Row:
        """Persist dispatch intent before any external process starts (plan 9.3)."""
        if in_transaction:
            return self._create_attempt_locked(
                attempt_id=attempt_id,
                run_id=run_id,
                task_revision=task_revision,
                role=role,
                reservation_id=reservation_id,
                reserved_turns=reserved_turns,
                reservation_expires_at=reservation_expires_at,
            )
        with self.transaction():
            return self._create_attempt_locked(
                attempt_id=attempt_id,
                run_id=run_id,
                task_revision=task_revision,
                role=role,
                reservation_id=reservation_id,
                reserved_turns=reserved_turns,
                reservation_expires_at=reservation_expires_at,
            )

    def _create_attempt_locked(
        self,
        *,
        attempt_id: str,
        run_id: str,
        task_revision: int,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
    ) -> sqlite3.Row:
        now = utc_now()
        active = self.conn.execute(
            "SELECT attempt_id FROM attempts WHERE run_id = ? AND state IN (?, ?)",
            (run_id, AttemptState.CREATED.value, AttemptState.ACTIVE.value),
        ).fetchone()
        if active is not None:
            raise StoreError(
                f"refusing to open a second live attempt for {run_id}: {active['attempt_id']} is active"
            )
        self.conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, run_id, task_revision, role, state, reservation_id,
                reserved_agent_turns, reserved_expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                run_id,
                task_revision,
                role,
                AttemptState.ACTIVE.value,
                reservation_id,
                reserved_turns,
                reservation_expires_at,
                now,
            ),
        )
        self.conn.execute(
            """
            UPDATE runs
               SET current_attempt_id = ?, task_state = ?, updated_at = ?
             WHERE run_id = ?
            """,
            (attempt_id, TaskState.RUNNING.value, now, run_id),
        )
        return self.conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    def record_process_identity(
        self, attempt_id: str, *, pid: int, started_at: str, identity: str, session_id: str | None
    ) -> None:
        """A PID alone is not proof; store the start time and a managed-instance marker."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE attempts
                   SET process_id = ?, process_started_at = ?, process_identity = ?, session_id = ?
                 WHERE attempt_id = ?
                """,
                (pid, started_at, identity, session_id, attempt_id),
            )

    def record_invocation(self, attempt_id: str, invocation_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET invocation_id = ? WHERE attempt_id = ?",
                (invocation_id, attempt_id),
            )

    def record_review_invocation(self, attempt_id: str, invocation_id: str) -> None:
        """Track the review invocation separately from the implementer invocation.

        Review runs as its own process with its own ID - it is not a continuation of the
        implementer session - even though both are recorded against the same attempt.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET review_invocation_id = ? WHERE attempt_id = ?",
                (invocation_id, attempt_id),
            )

    def reserve_review_turn(self, run_id: str, controller_id: str) -> None:
        """Reserve the review turn. Refuses when the ceiling cannot cover it (plan 10.3).

        Kept separate from dispatch so a run that can afford implementation but not
        review stops *before* the review is bought, instead of discovering it after.
        """
        with self.transaction():
            self._reserve_turn_locked(run_id, controller_id, turns=1)

    def attach_review_result(self, attempt_id: str, payload: dict[str, Any]) -> None:
        """Record that a review invocation happened for this attempt."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE attempts SET review_json = ? WHERE attempt_id = ?",
                (canonical_json(payload), attempt_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"unknown attempt {attempt_id}")

    def record_cancel_intent(self, run_id: str) -> str:
        """Note that a stop was requested, *before* asking anything to stop.

        Recorded first on purpose: if the process dies while we are stopping it, the intent
        is already durable, so a late result cannot be accepted as if nothing happened.
        Idempotent - the first timestamp wins.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT cancel_intent_at FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["cancel_intent_at"]:
                return str(row["cancel_intent_at"])
            now = utc_now()
            conn.execute(
                "UPDATE runs SET cancel_intent_at = ?, updated_at = ? WHERE run_id = ?",
                (now, now, run_id),
            )
            return now

    def record_cancel_receipt(self, run_id: str, receipt: CancellationReceipt) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET cancel_receipt_json = ?, updated_at = ? WHERE run_id = ?",
                (canonical_json(receipt.model_dump(mode="json")), utc_now(), run_id),
            )

    def cancel_state(self, run_id: str) -> tuple[str | None, CancellationReceipt | None]:
        row = self.get_run(run_id)
        receipt = (
            CancellationReceipt.model_validate(json.loads(row["cancel_receipt_json"]))
            if row["cancel_receipt_json"]
            else None
        )
        return (str(row["cancel_intent_at"]) if row["cancel_intent_at"] else None, receipt)

    def record_note(self, run_id: str, note: str) -> None:
        """Append an operator-facing note to the run's own audit trail.

        Notes live in their own table rather than in ``block_reason``, because a terminal
        transition clears that column and an audit fact (an authorization being consumed, a
        dirty target being left alone) must survive the run reaching a final state.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            conn.execute(
                "INSERT INTO run_notes (note_id, run_id, note, created_at) VALUES (?, ?, ?, ?)",
                (new_evidence_id(), run_id, note[:1000], utc_now()),
            )

    def notes_for(self, run_id: str) -> list[str]:
        return [row["note"] for row in self._fetchall(
            "SELECT note FROM run_notes WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
        )]

    def record_worktree(self, run_id: str, path: Path) -> None:
        """Record the workspace this run was given. Written before any work happens in it."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET worktree_path = ?, worktree_state = ?, updated_at = ? WHERE run_id = ?",
                (str(path), "READY", utc_now(), run_id),
            )

    def record_cleanup_intent(self, run_id: str, operator: str) -> str:
        """Claim the right to remove this run's workspace, transactionally (CAS-style).

        Refuses while another cleanup is in flight, so two operators cannot both decide to
        delete the same directory. The intent is durable before any git command runs, which
        is what makes an interrupted cleanup reconcilable afterwards. Idempotent: the first
        intent timestamp wins.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT cleanup_intent_at, cleanup_done_at, worktree_state FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["cleanup_done_at"]:
                return "already_removed"
            if row["worktree_state"] == "REMOVING" and row["cleanup_intent_at"]:
                return "in_progress"
            now = utc_now()
            conn.execute(
                """
                UPDATE runs SET cleanup_intent_at = ?, worktree_state = ?, updated_at = ?
                 WHERE run_id = ?
                """,
                (now, "REMOVING", now, run_id),
            )
            return f"claimed by {operator} at {now}"

    def finish_cleanup(self, run_id: str, *, error: str = "") -> None:
        """Record the outcome of a cleanup attempt without touching business state.

        A failed or refused attempt releases the claim: the workspace is still there, so the
        run must be cleanable again once the operator fixes what blocked it. Leaving the claim
        set would wedge the run in REMOVING forever.
        """
        with self.transaction() as conn:
            if error:
                conn.execute(
                    """
                    UPDATE runs SET worktree_state = ?, cleanup_intent_at = NULL,
                                    cleanup_error = ?, updated_at = ?
                     WHERE run_id = ?
                    """,
                    ("PRESENT", error[:500], utc_now(), run_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE runs SET worktree_state = ?, cleanup_done_at = ?, cleanup_error = NULL,
                                    updated_at = ?
                     WHERE run_id = ?
                    """,
                    ("REMOVED", utc_now(), utc_now(), run_id),
                )

    def workspace_state(self, run_id: str) -> dict[str, Any]:
        row = self.get_run(run_id)
        return {
            "worktree_path": row["worktree_path"],
            "worktree_state": row["worktree_state"],
            "cleanup_intent_at": row["cleanup_intent_at"],
            "cleanup_done_at": row["cleanup_done_at"],
            "cleanup_error": row["cleanup_error"],
        }

    # -- authorizations -------------------------------------------------------

    def register_authorization(self, record: dict[str, Any]) -> sqlite3.Row:
        """Record a one-shot user authorization, or return the existing identical record."""
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM authorizations WHERE authorization_id = ?",
                (record["authorization_id"],),
            ).fetchone()
            if existing is not None:
                if existing["binding_digest"] != record["binding_digest"]:
                    raise StoreError(
                        f"authorization {record['authorization_id']} already exists bound to a "
                        "different target; refusing to reuse it"
                    )
                return existing
            conn.execute(
                """
                INSERT INTO authorizations (
                    authorization_id, mode, binding_digest, user_text, provided_by,
                    authorized_at, max_top_level_submissions, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["authorization_id"],
                    record["mode"],
                    record["binding_digest"],
                    record["user_text"],
                    record["provided_by"],
                    record["authorized_at"],
                    int(record["max_top_level_submissions"]),
                    utc_now(),
                ),
            )
            return conn.execute(
                "SELECT * FROM authorizations WHERE authorization_id = ?",
                (record["authorization_id"],),
            ).fetchone()

    def claim_authorized_submission(self, authorization_id: str) -> int:
        """Consume one top-level submission from an authorization, atomically.

        The UPDATE is the gate and the CHECK constraint is the backstop, mirroring budget
        reservation: a restart, a new run id or a resubmitted identical spec cannot restore
        allowance. A refusal means "do not dispatch", not "dispatch and hope".
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE authorizations
                   SET used_top_level_submissions = used_top_level_submissions + 1
                 WHERE authorization_id = ?
                   AND used_top_level_submissions + 1 <= max_top_level_submissions
                """,
                (authorization_id,),
            )
            if cur.rowcount != 1:
                row = conn.execute(
                    "SELECT used_top_level_submissions, max_top_level_submissions FROM authorizations "
                    "WHERE authorization_id = ?",
                    (authorization_id,),
                ).fetchone()
                if row is None:
                    raise StoreError(f"unknown authorization {authorization_id}")
                raise StoreError(
                    f"authorization {authorization_id} is exhausted: "
                    f"{row['used_top_level_submissions']}/{row['max_top_level_submissions']} "
                    "top-level submissions used"
                )
            row = conn.execute(
                "SELECT used_top_level_submissions FROM authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            return int(row["used_top_level_submissions"])

    def authorization_state(self, authorization_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM authorizations WHERE authorization_id = ?", (authorization_id,)
        )
        return dict(row) if row is not None else None

    def record_reconcile(self, attempt_id: str, payload: dict[str, Any]) -> None:
        """Store what an interruption check observed. Never a new dispatch."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET reconcile_json = ? WHERE attempt_id = ?",
                (canonical_json(payload), attempt_id),
            )

    def _guard_current(self, conn: sqlite3.Connection, run_id: str, attempt_id: str) -> sqlite3.Row:
        """Compare-and-set precondition: this attempt is still the run's live revision."""
        run = conn.execute(
            "SELECT current_attempt_id, task_revision FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if run is None:
            raise RunNotFound(run_id)
        if attempt is None:
            raise StoreError(f"unknown attempt {attempt_id}")
        if run["current_attempt_id"] != attempt_id or run["task_revision"] != attempt["task_revision"]:
            raise StoreError(
                f"stale result rejected for {attempt_id}: run {run_id} is on attempt "
                f"{run['current_attempt_id']} revision {run['task_revision']}"
            )
        return attempt

    def finish_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        state: AttemptState,
        outcome: InvocationOutcome | None,
        result: dict[str, Any] | None,
        block_code: RefusalCode | None = None,
    ) -> sqlite3.Row:
        """Apply an attempt result only if it still belongs to the live revision."""
        now = utc_now()
        with self.transaction() as conn:
            self._guard_current(conn, run_id, attempt_id)
            cur = conn.execute(
                """
                UPDATE attempts
                   SET state = ?, outcome = ?, result_json = ?, result_digest = ?,
                       block_code = ?, finished_at = ?
                 WHERE attempt_id = ? AND state IN (?, ?)
                """,
                (
                    state.value,
                    outcome.value if outcome else None,
                    canonical_json(result) if result is not None else None,
                    digest_of(result) if result is not None else None,
                    block_code.value if block_code else None,
                    now,
                    attempt_id,
                    AttemptState.CREATED.value,
                    AttemptState.ACTIVE.value,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(f"attempt {attempt_id} is not live; result not applied")
            return conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()

    def advance_to_checking(
        self, *, run_id: str, attempt_id: str, phase: CheckPhase
    ) -> sqlite3.Row:
        now = utc_now()
        with self.transaction() as conn:
            self._guard_current(conn, run_id, attempt_id)
            cur = conn.execute(
                """
                UPDATE runs
                   SET task_state = ?, phase = ?, updated_at = ?
                 WHERE run_id = ? AND task_state = ?
                """,
                (TaskState.CHECKING.value, phase.value, now, run_id, TaskState.RUNNING.value),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id} is not RUNNING; cannot enter CHECKING")
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def start_repair_cycle(self, run_id: str, controller_id: str) -> int:
        """Bounded repair cycle (plan 10.4); refuses when the cycle budget is spent."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT repairs_used, repair_limit, claimed_by FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["claimed_by"] != controller_id:
                raise StoreError(f"run {run_id} is owned by {row['claimed_by']!r}")
            if row["repairs_used"] + 1 > row["repair_limit"]:
                raise StoreError(
                    f"repair budget exhausted for {run_id}: "
                    f"{row['repairs_used']}/{row['repair_limit']} cycles used"
                )
            conn.execute(
                """
                UPDATE runs
                   SET repairs_used = repairs_used + 1, current_attempt_id = NULL,
                       task_state = ?, phase = NULL, updated_at = ?
                 WHERE run_id = ?
                """,
                (TaskState.READY.value, utc_now(), run_id),
            )
            return int(row["repairs_used"]) + 1

    # -- evidence ------------------------------------------------------------

    def record_evidence(
        self,
        *,
        evidence_id: str,
        run_id: str,
        attempt_id: str,
        kind: str,
        status: EvidenceStatus,
        candidate_fingerprint: str,
        checks_digest: str,
        check_id: str = "",
        command: Sequence[str] = (),
        exit_code: int | None = None,
        stdout_digest: str = "",
        stderr_digest: str = "",
        detail: str = "",
    ) -> EvidenceRecord:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, run_id, attempt_id, kind, status, check_id,
                    candidate_fingerprint, checks_digest, command_json, exit_code,
                    stdout_digest, stderr_digest, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    run_id,
                    attempt_id,
                    kind,
                    status.value,
                    check_id,
                    candidate_fingerprint,
                    checks_digest,
                    canonical_json(list(command)),
                    exit_code,
                    stdout_digest,
                    stderr_digest,
                    detail,
                    now,
                ),
            )
        return EvidenceRecord(
            evidence_id=evidence_id,
            run_id=run_id,
            attempt_id=attempt_id,
            kind=kind,  # type: ignore[arg-type]
            status=status,
            check_id=check_id,
            candidate_fingerprint=candidate_fingerprint,
            checks_digest=checks_digest,
            command=list(command),
            exit_code=exit_code,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            detail=detail,
            created_at=now,
        )

    def evidence_for(self, run_id: str, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self._fetchall(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
            )
        return self._fetchall(
            "SELECT * FROM evidence WHERE run_id = ? AND kind = ? ORDER BY created_at, rowid",
            (run_id, kind),
        )

    # -- read model ----------------------------------------------------------

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )

    def attempts_for(self, run_id: str) -> list[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
        )

    def invocation_counts(self, run_id: str) -> tuple[int, int]:
        """``(implementer_invocations, reviewer_invocations)`` for this run.

        Reported separately on purpose: a review is its own process with its own
        reserved turn, so collapsing both into one "invocations" number would either
        understate what was dispatched or overstate what the implementer did.
        """
        row = self._fetchone(
            """
            SELECT
                COALESCE(SUM(CASE WHEN invocation_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS implementer,
                COALESCE(SUM(CASE WHEN review_invocation_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS reviewer
              FROM attempts
             WHERE run_id = ?
            """,
            (run_id,),
        )
        assert row is not None
        return int(row["implementer"]), int(row["reviewer"])
