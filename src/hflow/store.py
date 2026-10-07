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
* One dispatch transaction (batch E1): the authorization claim, the run's turn
  reservation, the root's consumption and the invocation record commit together, or
  none of them does.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    AttemptRecord,
    AttemptState,
    CancellationReceipt,
    CheckPhase,
    DeliveryState,
    DispatchReservation,
    DshContextRecord,
    EffectiveConfig,
    EvidenceRecord,
    EvidenceStatus,
    INTEGRATION_ACTIVE_STATES,
    IntegrationReceipt,
    IntegrationRecord,
    IntegrationState,
    InvocationIntent,
    InvocationOutcome,
    InvocationSettlement,
    InvocationStartState,
    InvocationStateCounts,
    RefusalCode,
    RepairRecord,
    ResultReceipt,
    RootBudgetBinding,
    RootBudgetLimits,
    RootBudgetUsage,
    RunAdmissionBinding,
    RunSummary,
    SpawnFact,
    SpawnKind,
    TaskSpec,
    TaskState,
    WorkspaceProvenance,
    canonical_json,
    digest_of,
    json_schema,
)
from .ids import new_attempt_id, new_evidence_id, new_id, parse_ts, utc_now
from .migrate import (
    MIGRATION_BACKUP_SUFFIX as MIGRATION_BACKUP_SUFFIX,
    STORAGE_VERSION as STORAGE_VERSION,
    SUPPORTED_STORAGE_VERSION as SUPPORTED_STORAGE_VERSION,
    MigrationError,
    StepHook,
    effective_version as effective_version,
    migrate,
    recorded_version as recorded_version,
)
from .ownership import OwnerFence, ProcessIdentity

#: Marks a receipt that records an offline reprocessing decision rather than the outcome of
#: the execution it belongs to. The string lives here because both the store guard and the
#: local finalization entry point must agree on it.
OFFLINE_REPROCESSING_KIND = "offline_reprocessing"

#: Prefix of the run note that carries the resolved configuration. A prefix rather than a
#: schema change: the note table already has the durability this record needs, and a reader
#: that does not know the prefix simply sees one more note.
_EFFECTIVE_CONFIG_PREFIX = "effective_config: "
#: Prefix of the run note that carries one frozen candidate's DSH context record. Kept in
#: ``run_notes`` like the effective configuration: no storage version change, and an older build
#: sees one more note.
_DSH_CONTEXT_PREFIX = "dsh_context: "
_ADMISSION_BINDING_PREFIX = "admission_binding: "
_WORKSPACE_PROVENANCE_PREFIX = "workspace_provenance: "


def _same_offline_reprocessing(
    receipt: ResultReceipt, *, source_evidence_id: str, candidate_fingerprint: str
) -> bool:
    """Is this receipt the same offline-reprocessing decision, from the same evidence?

    Used for idempotency: the same source evidence and candidate must return the same
    decision instead of issuing a second delivery.
    """
    provenance = receipt.provenance
    return (
        provenance.get("kind") == OFFLINE_REPROCESSING_KIND
        and provenance.get("source_evidence_id") == source_evidence_id
        and receipt.candidate.fingerprint == candidate_fingerprint
    )

# The schema itself lives in :mod:`hflow.migrate`, which owns the recorded storage version, the
# pre-migration backup and the transaction each step runs in. This module decides *when* to
# migrate (at open) and what the ledger means afterwards.

#: Public contract version of the stored documents (``runs.schema_version``, ``TaskSpec`` and
#: ``ResultReceipt``). Distinct from the storage format version in :mod:`hflow.migrate`: a new
#: table does not change what a stored document means, and vice versa.
SCHEMA_VERSION = 1


#: The invocation states that still block their root. Kept next to the SQL that uses them so a
#: new state cannot be added to the contract and silently forgotten by the ledger: the contract's
#: ``InvocationIntent.pending`` is the definition, and a test asserts the two agree.
INVOCATION_UNRESOLVED_STATES: tuple[str, ...] = (
    InvocationStartState.RESERVED.value,
    InvocationStartState.REQUESTED.value,
    InvocationStartState.STARTED.value,
    InvocationStartState.UNKNOWN.value,
    InvocationStartState.LAUNCH_UNKNOWN.value,
)

#: The states a settlement or a confirmed stop may still close: the dispatch is in flight and
#: nothing final is recorded about it. ``unknown`` and ``launch_unknown`` are unresolved too, but
#: not *open* - only an operator's reconcile closes them - and ``not_started`` and ``settled``
#: already record their final fact.
INVOCATION_OPEN_STATES: tuple[str, ...] = (
    InvocationStartState.RESERVED.value,
    InvocationStartState.REQUESTED.value,
    InvocationStartState.STARTED.value,
)

#: The states ``hflow ledger settle`` may close (ruling 2026-10-03): unresolved and no longer
#: open. An open entry is refused - a controller may still be driving it, and ``hflow resume``
#: is what reconciles it into one of these.
INVOCATION_OPERATOR_SETTLEABLE_STATES: tuple[str, ...] = (
    InvocationStartState.UNKNOWN.value,
    InvocationStartState.LAUNCH_UNKNOWN.value,
)
#: The bound on an operator's attestation text, in characters.
ATTESTATION_MAX_CHARS = 2000

_TERMINAL_STATES: tuple[str, ...] = (
    TaskState.ACCEPTED.value,
    TaskState.BLOCKED.value,
    TaskState.CANCELLED.value,
)
#: The guard every writer that sets ``BLOCKED`` carries: an outcome already recorded - a terminal
#: state, or a delivery receipt - is never relabelled. Its three placeholders take
#: ``_TERMINAL_STATES``.
_LIVE_RUN_GUARD = "task_state NOT IN (?, ?, ?) AND receipt_json IS NULL"


def _add_seconds(timestamp: str, seconds: int) -> str:
    """``timestamp`` (a stored UTC string) plus ``seconds``, in the same textual form."""
    return (
        (parse_ts(timestamp) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    )


def _invocation_from_row(row: sqlite3.Row) -> InvocationIntent:
    """The contract view of one ledger row.

    A missing ``outcome`` is reported as ``None`` rather than defaulted: "no outcome was
    observed" and "the outcome was unknown" are different facts, and the row records the first.
    """
    return InvocationIntent(
        invocation_id=str(row["invocation_id"]),
        root_id=str(row["root_id"] or ""),
        run_id=str(row["run_id"]),
        attempt_id=str(row["attempt_id"]),
        role=str(row["role"]),  # type: ignore[arg-type]
        authorization_id=str(row["authorization_id"] or ""),
        round=int(row["round"]),
        state=InvocationStartState(str(row["state"])),
        reserved_at=str(row["reserved_at"]),
        root_used_at_reservation=int(row["root_used_at_reservation"]),
        authorization_used_at_reservation=int(row["authorization_used_at_reservation"]),
        is_repair=bool(row["is_repair"]),
        launch_requested_at=row["launch_requested_at"],
        started_at=row["started_at"],
        process_started_at=row["process_started_at"],
        process_pid=row["process_pid"],
        spawn_kind=SpawnKind(str(row["spawn_kind"] or SpawnKind.UNKNOWN.value)),
        settled_at=row["settled_at"],
        outcome=InvocationOutcome(str(row["outcome"])) if row["outcome"] else None,
        detail=str(row["detail"] or ""),
    )


class StoreError(RuntimeError):
    pass


class StoredRecordUnreadable(StoreError):
    """A durable structured record cannot be decoded without losing facts."""


class RunNotFound(StoreError):
    pass


class OwnerLostError(StoreError):
    """A guarded write by a controller that no longer owns the run (a takeover superseded it)."""


class SettlementRefused(StoreError):
    """``settle_by_operator`` refused; nothing was written."""


class InvocationNotFound(StoreError):
    """No ledger entry has this invocation id."""


class IntegrationNotFound(StoreError):
    """No integration record has this integration id."""


class IntegrationConflict(StoreError):
    """An integration write was refused by the stored state; nothing was written.

    A compare-and-set whose record is not in an expected state, a second active integration of the
    same run, a second ``applying`` integration of the same repository target, or a run that is not
    an accepted candidate. The message names the current state or the other integration.
    """


#: Prefix of the run notes an integration writes (``integration: <id> ...``).
NOTE_INTEGRATION = "integration"

#: ``IntegrationRecord`` list fields and the JSON column each one is stored in. Every other field
#: is stored in the column of the same name.
_INTEGRATION_LIST_COLUMNS: dict[str, str] = {
    "evidence_ids": "evidence_ids_json",
    "paths": "paths_json",
    "conflict_paths": "conflict_paths_json",
}
#: What ``update_integration`` may change besides ``state``. The identity of an integration - its
#: run, candidate, target and the tip it was prepared against - is fixed at creation, and the
#: ``integrated`` facts (time, basis, receipt) are written only by ``finalize_integration``.
_INTEGRATION_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "mode",
        "integration_commit",
        "integration_tree",
        "integration_ref",
        "fingerprint",
        "evidence_ids",
        "paths",
        "conflict_paths",
        "worktree_path",
        "worktree_state",
        "owner_pid",
        "owner_created",
        "owner_host",
        "apply_intent_at",
        "applied_by",
        "detail",
    }
)


def _integration_states(
    expected: IntegrationState | str | Sequence[IntegrationState | str],
) -> tuple[IntegrationState, ...]:
    """``expected`` as a non-empty tuple of states. A single state is a ``str``, so it is checked
    before the sequence case: iterating it would yield its characters."""
    items = (expected,) if isinstance(expected, str) else tuple(expected)
    states = tuple(IntegrationState(item) for item in items)
    if not states:
        raise ValueError("expected names no integration state")
    return states


def _integration_values(record: IntegrationRecord) -> dict[str, Any]:
    """Column name -> stored value for every ``IntegrationRecord`` field."""
    values: dict[str, Any] = {}
    for name, value in record.model_dump(mode="json").items():
        column = _INTEGRATION_LIST_COLUMNS.get(name)
        values[column or name] = canonical_json(value) if column else value
    return values


class Store:
    """Thin, explicit SQLite wrapper. No ORM, no implicit commits.

    Connections are shared across threads (the controller can be cancelling while a
    background thread drives a run), so every statement and transaction is serialized by a
    re-entrant lock. That is stronger than relying on SQLite's own serialized threading mode:
    it also makes multi-statement transactions atomic with respect to sibling threads
    instead of interleaving with them.
    """

    def __init__(
        self, path: Path | str, *, on_migration_step: StepHook | None = None
    ) -> None:
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

        # Storage versions are handled before anything else touches the file: an unknown newer
        # version is refused with nothing written, and a known older one is snapshotted, then
        # migrated in a single transaction (see ``hflow.migrate``).
        try:
            version_before, self.storage_version, self.migration_backup = migrate(
                self.conn, self.path, on_step=on_migration_step
            )
            # ``version_before`` is the *effective* version - including the inferred 1 of a
            # pre-E1 file that recorded none - so a caller can report what the file was. A file
            # that did not exist before this call was created, not migrated: version 0 is not a
            # migration source and reporting "migrated from 0" would be noise.
            self.migrated_from = (
                version_before
                if 0 < version_before < self.storage_version
                else None
            )
        except BaseException as exc:
            # Any failure to open the ledger closes the connection: a half-open handle would
            # keep the WAL and the file locked while the caller believes the open failed. A
            # migration problem is reported as the store's own error; anything else (a hook that
            # raised, an I/O failure) keeps its own type after the cleanup.
            self.conn.close()
            if isinstance(exc, MigrationError):
                raise StoreError(str(exc)) from exc
            raise

        with self.transaction(autocommit=True):
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

        ``autocommit=True`` is used only for statement batches that manage their own
        transaction boundary (the schema digest seed, and the migrations in
        :mod:`hflow.migrate`, which open their own and must not be nested).
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

    def total_changes(self) -> int:
        """Rows this store's connection has inserted, updated or deleted since it was opened.

        SQLite's own counter (``sqlite3_total_changes``). Two readings tell a caller whether
        anything in between wrote through this store; a statement whose transaction was rolled
        back still counts, so the comparison can over-report a write, never miss one.
        """
        with self._lock:
            return int(self.conn.total_changes)

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
        controller_id: str | None = None,
        owner_token: str | None = None,
        owner_identity: ProcessIdentity | None = None,
        admission_binding: RunAdmissionBinding | None = None,
        effective_config: EffectiveConfig | None = None,
    ) -> sqlite3.Row:
        """Insert a run, or return the existing row for the same ``(project_id, spec_digest)``.

        With ``owner_token`` (the controller passes it, already holding its owner lock) the owner
        is written in the **same insert** - token, pid, creation time, host, label and claim
        generation 1 - so a run this build creates is never NULL-owned, not even for an instant a
        successor could read as "no owner recorded". An existing row (a lost insert race) is
        returned unchanged; the caller decides what to do with a run it did not create. Without
        ``owner_token`` (store-level tests and tools) the run is created unclaimed, as before.
        """
        if owner_token is not None and (controller_id is None or owner_identity is None):
            raise StoreError("an owned create_run needs the controller label and owner identity")
        now = utc_now()
        owner_values: tuple[Any, ...] = (None, None, None, None, None, None, 0)
        if owner_token is not None and owner_identity is not None:
            owner_values = (
                controller_id, now, owner_token, owner_identity.pid, owner_identity.created,
                owner_identity.host, 1,
            )
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
                    created_at, updated_at,
                    claimed_by, claimed_at, owner_token, owner_pid, owner_created, owner_host,
                    claim_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    *owner_values,
                ),
            )
            if admission_binding is not None:
                self._record_structured_note_locked(
                    conn, run_id, _ADMISSION_BINDING_PREFIX, admission_binding
                )
            if effective_config is not None:
                self._record_structured_note_locked(
                    conn, run_id, _EFFECTIVE_CONFIG_PREFIX, effective_config
                )
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def get_run(self, run_id: str) -> sqlite3.Row:
        row = self._fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise RunNotFound(run_id)
        return row

    def claim_run(self, run_id: str, controller_id: str) -> bool:
        """Transactionally claim exclusive scheduling ownership (acceptance A02).

        The label-only form, with no owner identity. It never claims a run that a process owner
        holds (``owner_token`` set): a label is shared by every CLI process and cannot outrank
        one. The controller claims with :meth:`claim_run_owned`.
        """
        now = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET claimed_by = ?, claimed_at = ?, updated_at = ?
                 WHERE run_id = ? AND (claimed_by IS NULL OR claimed_by = ?)
                   AND owner_token IS NULL
                """,
                (controller_id, now, now, run_id, controller_id),
            )
            if cur.rowcount == 1:
                return True
            row = conn.execute("SELECT claimed_by FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            return False

    def claim_run_owned(
        self, run_id: str, controller_id: str, *, token: str, identity: ProcessIdentity
    ) -> int | None:
        """Claim a run for one owner process; return the claim generation, ``None`` when refused.

        A compare-and-set (owner lease, user ruling 2026-10-03): it succeeds only for a run nobody
        has claimed (no owner token and no label) - the generation becomes the next one - or for a
        run this same token already owns, which is idempotent and keeps the generation. A run
        another owner holds, or one a label-only claim holds, is never taken here; taking over a
        run whose owner is provably gone is :meth:`take_over_run`, which blocks it.
        """
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET claimed_by = ?, claimed_at = ?, updated_at = ?,
                       owner_token = ?, owner_pid = ?, owner_created = ?, owner_host = ?,
                       claim_generation = claim_generation + 1
                 WHERE run_id = ? AND owner_token IS NULL AND claimed_by IS NULL
                """,
                (
                    controller_id, now, now, token, identity.pid, identity.created,
                    identity.host, run_id,
                ),
            )
            row = conn.execute(
                "SELECT owner_token, claim_generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["owner_token"] == token:
                return int(row["claim_generation"])
            return None

    def adopt_run(
        self,
        run_id: str,
        *,
        expected_token: str,
        expected_generation: int,
        controller_id: str,
        token: str,
        identity: ProcessIdentity,
        note: str,
    ) -> int | None:
        """Adopt a never-dispatched run from a provably gone owner and keep it live: ONE transaction.

        The caller has already proven the owner gone (lock free and identity gone). This is the
        compare-and-set that makes the adoption exclusive, and it re-checks in the same ``WHERE``
        that nothing could be in flight: the run still has ``expected_token`` at
        ``expected_generation``, is ``DRAFT`` or ``READY`` with no stop intent and no receipt, and
        has **no attempt and no invocation** row. Then the owner becomes ``token``, the generation
        increments (every guarded write by the old owner fails from here on), the state is kept
        and ``note`` is recorded. Returns the new generation, ``None`` when any guard failed
        (nothing written). A run with anything dispatched is never adopted; that is
        :meth:`take_over_run`, which blocks it.
        """
        now = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                   SET owner_token = ?, owner_pid = ?, owner_created = ?, owner_host = ?,
                       claimed_by = ?, claimed_at = ?, updated_at = ?,
                       claim_generation = claim_generation + 1
                 WHERE run_id = ? AND owner_token = ? AND claim_generation = ?
                   AND task_state IN (?, ?) AND receipt_json IS NULL
                   AND cancel_intent_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.run_id = runs.run_id)
                   AND NOT EXISTS (SELECT 1 FROM invocations WHERE invocations.run_id = runs.run_id)
                """,
                (
                    token, identity.pid, identity.created, identity.host, controller_id, now, now,
                    run_id, expected_token, expected_generation,
                    TaskState.DRAFT.value, TaskState.READY.value,
                ),
            )
            if cur.rowcount != 1:
                if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                    raise RunNotFound(run_id)
                return None
            self._record_note_locked(conn, run_id, note)
            generation = conn.execute(
                "SELECT claim_generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["claim_generation"]
            return int(generation)

    def _check_fence_locked(
        self, conn: sqlite3.Connection, run_id: str, fence: OwnerFence | None
    ) -> None:
        """Refuse a guarded write from a controller a takeover superseded. ``None`` skips it."""
        if fence is None:
            return
        row = conn.execute(
            "SELECT owner_token, claim_generation, owner_pid, owner_host FROM runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        if row["owner_token"] != fence.token or int(row["claim_generation"]) != fence.generation:
            raise OwnerLostError(
                f"owner_lost: run {run_id} is owned by pid={row['owner_pid']} "
                f"host={row['owner_host']} at claim generation {row['claim_generation']}, not by "
                f"this controller (generation {fence.generation}); nothing was written"
            )

    def take_over_run(
        self,
        run_id: str,
        *,
        expected_token: str | None,
        expected_generation: int,
        controller_id: str,
        token: str,
        identity: ProcessIdentity,
        reason: str,
        detail: str,
        note: str,
        require_nothing_dispatched: bool = False,
    ) -> tuple[int, int, int] | None:
        """Take a live run from a provably gone owner and block it ``owner_lost``: ONE transaction.

        The caller has already proven the owner gone (lock free and identity gone); this is the
        compare-and-set that makes the decision exclusive. In order, all-or-nothing:

        1. the run still has ``expected_token`` (``None`` for a pre-v6 owner) at
           ``expected_generation`` and is live (not terminal, no receipt) - otherwise nothing is
           written and ``None`` is returned;
        2. the owner becomes the successor and the generation increments, so every guarded write
           by the old owner fails from here on;
        3. every unsettled invocation becomes ``unknown`` / ``launch_unknown`` (the same rule as
           :meth:`mark_unsettled_invocations_unknown`) - the allowance stays spent;
        4. every live attempt is finished as an unknown outcome;
        5. the run is ``BLOCKED`` with ``owner_lost`` and the takeover note is recorded.

        Nothing is dispatched, nothing is stopped and nothing is refunded. Returns
        ``(new_generation, invocations_closed, attempts_closed)``.

        ``require_nothing_dispatched`` adds to the compare-and-set that the run has no attempt and
        no invocation row - the only case in which a run whose owner was never recorded (not
        proven gone) may be taken over.
        """
        now = utc_now()
        nothing_dispatched = (
            "AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.run_id = runs.run_id) "
            "AND NOT EXISTS (SELECT 1 FROM invocations WHERE invocations.run_id = runs.run_id)"
            if require_nothing_dispatched
            else ""
        )
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE runs
                   SET owner_token = ?, owner_pid = ?, owner_created = ?, owner_host = ?,
                       claimed_by = ?, claimed_at = ?, updated_at = ?,
                       claim_generation = claim_generation + 1
                 WHERE run_id = ? AND owner_token IS ? AND claim_generation = ?
                   AND {_LIVE_RUN_GUARD} {nothing_dispatched}
                """,
                (
                    token, identity.pid, identity.created, identity.host, controller_id, now, now,
                    run_id, expected_token, expected_generation, *_TERMINAL_STATES,
                ),
            )
            if cur.rowcount != 1:
                if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                    raise RunNotFound(run_id)
                return None
            closed = self._mark_unsettled_invocations_unknown_locked(conn, run_id, detail)
            result = {"error": reason}
            attempts = conn.execute(
                """
                UPDATE attempts
                   SET state = ?, outcome = ?, result_json = ?, result_digest = ?,
                       block_code = ?, finished_at = ?
                 WHERE run_id = ? AND state IN (?, ?)
                """,
                (
                    AttemptState.OUTCOME_UNKNOWN.value,
                    InvocationOutcome.OUTCOME_UNKNOWN.value,
                    canonical_json(result),
                    digest_of(result),
                    RefusalCode.OWNER_LOST.value,
                    now,
                    run_id,
                    AttemptState.CREATED.value,
                    AttemptState.ACTIVE.value,
                ),
            ).rowcount
            blocked = conn.execute(
                f"""
                UPDATE runs
                   SET task_state = ?, block_code = ?, block_reason = ?, updated_at = ?
                 WHERE run_id = ? AND {_LIVE_RUN_GUARD}
                """,
                (
                    TaskState.BLOCKED.value, RefusalCode.OWNER_LOST.value, reason, now, run_id,
                    *_TERMINAL_STATES,
                ),
            )
            if blocked.rowcount != 1:  # pragma: no cover - the CAS above holds the same guard
                raise StoreError(f"run {run_id} left its live state during the takeover")
            self._record_note_locked(conn, run_id, note)
            generation = conn.execute(
                "SELECT claim_generation FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["claim_generation"]
            return int(generation), int(closed), int(attempts)

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
        """Block a live run; a run that already ended keeps its outcome.

        The write carries the same terminal guard as :meth:`block_unless_stopped`: an
        ``ACCEPTED``, ``BLOCKED`` or ``CANCELLED`` run, or one that carries a delivery receipt, is
        never relabelled. The stored row is returned either way, so a caller can see which of the
        two happened.
        """
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE runs
                   SET task_state = ?, block_code = ?, block_reason = ?, updated_at = ?
                 WHERE run_id = ? AND {_LIVE_RUN_GUARD}
                """,
                (TaskState.BLOCKED.value, code.value, reason, now, run_id, *_TERMINAL_STATES),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            return row

    def block_unless_stopped(
        self,
        run_id: str,
        code: RefusalCode,
        reason: str,
        *,
        held_attempts: Sequence[str] | None = None,
    ) -> bool:
        """Record a block **unless a stop or an earlier outcome already decided this run**.

        The stop decision and this write are one statement, so they cannot interleave: a
        cancellation committed before it wins and this returns ``False``, and a cancellation
        committed after it sees a run that is already ``BLOCKED`` and records its own state
        through the same conditional form. Checking ``cancel_intent_at`` in Python and then
        writing unconditionally is exactly the race this replaces - the check and the write
        must be the same transaction.

        The guard, in full, all in the one ``WHERE`` clause:

        * ``cancel_intent_at IS NULL`` - once a stop is durable, no later failure (a driver
          error, a protocol failure, a rejected review) may relabel the run;
        * the run is not ``ACCEPTED``/``BLOCKED``/``CANCELLED`` and carries no delivery receipt -
          an outcome already recorded is never relabelled, whoever writes next (a concurrent
          identical submission's refusal, a late failure);
        * with ``held_attempts`` (the attempts the calling controller reserved for this run,
          possibly none): the run has no attempt row outside that set. A controller that did not
          reserve the run's attempt is not the one driving it, so its refusal is not the run's
          outcome.

        ``False`` means nothing was written; the caller reads the row to say which guard held.
        """
        now = utc_now()
        params: list[Any] = [TaskState.BLOCKED.value, code.value, reason, now, run_id]
        params.extend(_TERMINAL_STATES)
        foreign = ""
        if held_attempts is not None:
            held = list(held_attempts)
            foreign = "AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.run_id = runs.run_id"
            if held:
                foreign += f" AND attempts.attempt_id NOT IN ({','.join('?' for _ in held)})"
                params.extend(held)
            foreign += ")"
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE runs
                   SET task_state = ?, block_code = ?, block_reason = ?, updated_at = ?
                 WHERE run_id = ? AND cancel_intent_at IS NULL AND {_LIVE_RUN_GUARD}
                   {foreign}
                """,
                params,
            )
            return cur.rowcount == 1

    def register_attempt_invocation_unless_stopped(
        self, run_id: str, attempt_id: str, *, column: str, invocation_id: str
    ) -> bool:
        """Register a role's invocation for a run that has not been stopped, atomically.

        This is the handoff a stop races with: the invocation id a stop would target and the
        record that the process is about to be started must become visible together. Making the
        registration conditional on ``cancel_intent_at IS NULL`` means the two orders are both
        correct - either the stop commits first and no invocation is registered (so the handoff
        is abandoned and nothing is started), or the registration commits first and the stop
        targets *this* invocation instead of the implementer's.

        The whole check is the ``WHERE`` clause, in one statement under ``BEGIN IMMEDIATE``: a
        stop that commits at any point up to this statement wins, and no window exists between
        deciding "not stopped" and writing the registration for one to land in.

        ``column`` is one of the two invocation columns; it is never interpolated from user
        input, and anything else is refused rather than formatted into SQL.
        """
        if column not in {"invocation_id", "review_invocation_id"}:
            raise StoreError(f"unknown invocation column {column!r}")
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE attempts
                   SET {column} = ?
                 WHERE attempt_id = ?
                   AND run_id = ?
                   AND (SELECT cancel_intent_at FROM runs WHERE run_id = ?) IS NULL
                """,
                (invocation_id, attempt_id, run_id, run_id),
            )
            return cur.rowcount == 1

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

    def add_turns_observed(self, run_id: str, turns: int) -> None:
        """Add one applied implementer result's self-reported turns to the run's total.

        Called only for a result that was applied, so a late result never moves it, and added
        rather than replaced, so a repaired run reports the sum of its implementer rounds.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET turns_observed = COALESCE(turns_observed, 0) + ?, updated_at = ? "
                "WHERE run_id = ?",
                (int(turns), utc_now(), run_id),
            )

    def finalize_acceptance(
        self,
        run_id: str,
        receipt: ResultReceipt,
        *,
        checks_digest: str,
        fence: OwnerFence | None = None,
    ) -> None:
        """Accept a run atomically: state change and receipt in the same transaction.

        Refuses if the approved checks changed while the run was executing, which
        would mean the evidence was produced under a different command set
        (acceptance A11), and refuses if a cancellation intent was recorded after the
        result arrived: an accepted cancellation must not be overwritten by a late
        success (the mirror of the stale-attempt rule). ``fence`` refuses it with ``owner_lost``
        when a takeover superseded the caller.
        """
        with self.transaction() as conn:
            self._check_fence_locked(conn, run_id, fence)
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

    def finalize_offline_reprocessing(
        self,
        run_id: str,
        receipt: ResultReceipt,
        *,
        checks_digest: str,
        source_evidence_id: str,
        candidate_fingerprint: str,
        review_evidence: dict[str, Any],
    ) -> bool:
        """Record a *later* decision about an execution that already ended, in one transaction.

        This exists for one narrow case: a run whose execution and checks completed but whose
        delivery was lost to an adapter failure, where the recorded evidence still supports
        acceptance under the fixed build. It is deliberately not a general recovery path, and
        it never fabricates an execution:

        * it refuses anything but the original terminal state - a run that is not ``BLOCKED``
          with a recorded reason, or that has a cancellation intent, is never reopened;
        * it refuses to overwrite a receipt that records a *different* decision, so two
          conflicting finalizations cannot both be delivered;
        * if the same decision is already recorded it changes nothing and reports ``False``,
          which makes the operation idempotent instead of duplicating a delivery;
        * the review evidence and the receipt are written together, so no receipt can name
          evidence that was never stored;
        * it never touches authorization rows or budget counters: a local decision is not a
          model submission, and no allowance is consumed or restored here.

        The caller is responsible for having re-checked the evidence bindings (candidate
        fingerprint, checks digest, verification evidence, review binding) against the
        current bytes; this method enforces the state-machine and receipt-level guards.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["checks_digest"] != checks_digest:
                raise StoreError(
                    f"run {run_id} was admitted under checks digest {row['checks_digest']} but the "
                    f"project contract now provides {checks_digest}; recorded evidence would be stale"
                )
            if row["cancel_intent_at"]:
                raise StoreError(
                    f"run {run_id} has a recorded cancellation intent at {row['cancel_intent_at']}; "
                    "an accepted cancellation is never overwritten by a later decision"
                )
            existing_json = row["receipt_json"]
            if existing_json:
                existing = ResultReceipt.model_validate(json.loads(existing_json))
                if _same_offline_reprocessing(
                    existing,
                    source_evidence_id=source_evidence_id,
                    candidate_fingerprint=candidate_fingerprint,
                ):
                    return False  # the same decision is already recorded: nothing to do
                raise StoreError(
                    f"run {run_id} already carries a delivery receipt for a different decision; "
                    "refusing to overwrite it"
                )
            if row["task_state"] != TaskState.BLOCKED.value or not row["block_reason"]:
                raise StoreError(
                    f"run {run_id} is {row['task_state']} without a recorded failure; offline "
                    "reprocessing only applies to an execution that ended blocked"
                )
            if receipt.provenance.get("kind") != OFFLINE_REPROCESSING_KIND:
                raise StoreError(
                    f"run {run_id}: an offline-reprocessing receipt must declare its provenance"
                )
            if receipt.run_id != run_id:
                raise StoreError(f"receipt is for {receipt.run_id}, not {run_id}")
            if receipt.candidate.fingerprint != candidate_fingerprint:
                raise StoreError(
                    "the receipt's candidate fingerprint is not the evidence the decision was "
                    "derived from"
                )
            attempt_id = receipt.attempt_id
            if not attempt_id:
                raise StoreError("an offline-reprocessing receipt must name the attempt it replays")
            if not receipt.review.evidence_ids:
                raise StoreError("an offline-reprocessing receipt must reference its review evidence")
            review_status = EvidenceStatus(review_evidence["status"])
            cur = conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, run_id, attempt_id, kind, status, check_id,
                    candidate_fingerprint, checks_digest, command_json, exit_code,
                    stdout_digest, stderr_digest, detail, created_at
                ) VALUES (?, ?, ?, 'review', ?, 'review', ?, ?, '[]', NULL, '', '', ?, ?)
                """,
                (
                    receipt.review.evidence_ids[0],
                    run_id,
                    attempt_id,
                    review_status.value,
                    candidate_fingerprint,
                    checks_digest,
                    str(review_evidence.get("detail", ""))[:2000],
                    utc_now(),
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id}: the review evidence could not be recorded")
            now = utc_now()
            cur = conn.execute(
                """
                UPDATE runs
                   SET receipt_json = ?, task_state = ?, phase = NULL, delivery_state = ?,
                       block_code = NULL, block_reason = NULL, updated_at = ?
                 WHERE run_id = ? AND task_state = ? AND receipt_json IS NULL
                """,
                (
                    canonical_json(receipt.model_dump(mode="json")),
                    TaskState.ACCEPTED.value,
                    DeliveryState.LOCAL_CANDIDATE.value,
                    now,
                    run_id,
                    TaskState.BLOCKED.value,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(
                    f"run {run_id} changed while the offline decision was being recorded; nothing "
                    "was written"
                )
            self._record_note_locked(
                conn,
                run_id,
                "offline reprocessing decision: this run was BLOCKED/"
                f"{receipt.provenance.get('original_block_code', 'unknown')} on build "
                f"{receipt.provenance.get('original_runtime_build', 'unknown')}"
                f" ({receipt.provenance.get('original_block_reason', 'no detail')}); delivery was "
                "recorded later from the same recorded evidence by build "
                f"{receipt.runtime_build} through {OFFLINE_REPROCESSING_KIND} "
                f"(source evidence {source_evidence_id}). The original failure is preserved here "
                "and is not a success of that execution. No model submission was made.",
            )
            return True

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

    # -- root budget ledger (batch E1) ---------------------------------------

    def register_root_budget(
        self, binding: RootBudgetBinding, limits: RootBudgetLimits
    ) -> sqlite3.Row:
        """Record a root's binding and its ceilings, or return the existing identical row.

        A root is registered once and never re-initialised. Two ways this could otherwise leak
        a fresh allowance, both refused here:

        * the same root id with *different* limits or a different binding - an artifact could
          ask for more than the approval that opened the root, so the recorded row stands;
        * the same ``(project_id, repo_path, task_id)`` under a different root id - that is the
          same task, and the unique index is what makes "a new root id" impossible rather than
          merely discouraged.
        """
        now = utc_now()
        with self.transaction() as conn:
            row = self._root_registration_row_locked(conn, binding, limits)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO root_budgets (
                        root_id, project_id, task_id, repo_path, ledger_path,
                        max_top_level_submissions, max_repairs, deadline_seconds,
                        limits_digest, binding_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.root_id,
                        binding.project_id,
                        binding.task_id,
                        binding.repo_path,
                        binding.ledger_path,
                        limits.max_top_level_submissions,
                        limits.max_repairs,
                        limits.deadline_seconds,
                        limits.digest(),
                        digest_of(binding.model_dump(mode="json")),
                        now,
                        now,
                    ),
                )
            return conn.execute(
                "SELECT * FROM root_budgets WHERE root_id = ?", (binding.root_id,)
            ).fetchone()

    def check_root_registration(
        self, binding: RootBudgetBinding, limits: RootBudgetLimits
    ) -> None:
        """Raise the ``StoreError`` ``register_root_budget`` would raise, without writing.

        The controller asks this before its other root refusals, so a run presenting ceilings
        that differ from the recorded ones is told so - not that the recorded root cannot afford
        its repair - and a refused run registers nothing. ``register_root_budget`` repeats the
        same checks inside its transaction, which stays the gate.
        """
        with self._lock:
            self._root_registration_row_locked(self.conn, binding, limits)

    def _root_registration_row_locked(
        self,
        conn: sqlite3.Connection,
        binding: RootBudgetBinding,
        limits: RootBudgetLimits,
    ) -> sqlite3.Row | None:
        """The recorded row this root agrees with, ``None`` when it is new; refuse otherwise."""
        row = conn.execute(
            "SELECT * FROM root_budgets WHERE root_id = ?", (binding.root_id,)
        ).fetchone()
        if row is None:
            clash = conn.execute(
                "SELECT root_id, max_top_level_submissions, max_repairs, deadline_seconds "
                "FROM root_budgets WHERE project_id = ? AND repo_path = ? AND task_id = ?",
                (binding.project_id, binding.repo_path, binding.task_id),
            ).fetchone()
            if clash is not None:
                raise StoreError(
                    f"the task {binding.task_id} of {binding.project_id} at {binding.repo_path} "
                    f"already has root {clash['root_id']}; this run resolves {binding.root_id} "
                    "for the same task, and a second root would hand it a second allowance"
                )
            return None
        self._check_root_limits_locked(conn, row, binding, limits)
        if row["binding_digest"] != digest_of(binding.model_dump(mode="json")):
            raise StoreError(
                f"root {binding.root_id} is already recorded against a different binding "
                f"(recorded ledger {row['ledger_path']}, this run resolves "
                f"{binding.ledger_path}); the recorded root stands"
            )
        return row

    def _check_root_limits_locked(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        binding: RootBudgetBinding,
        limits: RootBudgetLimits,
    ) -> None:
        """Refuse a root whose recorded ceilings differ from the ones just presented."""
        recorded = RootBudgetLimits(
            max_top_level_submissions=int(row["max_top_level_submissions"]),
            max_repairs=int(row["max_repairs"]),
            deadline_seconds=int(row["deadline_seconds"]),
        )
        if recorded.digest() != limits.digest():
            raise StoreError(
                f"root {row['root_id']} is recorded with limits {recorded.model_dump(mode='json')} "
                f"but this run presents {limits.model_dump(mode='json')}. A root's ceiling is part "
                "of the approval that opened it; changing it later is not something a run may do, "
                "and there is no top-up path in this build."
            )

    def root_budget_row(self, root_id: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM root_budgets WHERE root_id = ?", (root_id,))

    def root_budget_for_run(self, run_id: str) -> sqlite3.Row | None:
        """This run's root row, or ``None`` for a run that is not spent against a root.

        Read through ``attempts.root_id`` rather than re-deriving it: the ledger is the record
        of what was actually charged, and a legacy run has no root at all.
        """
        return self._fetchone(
            """
            SELECT rb.* FROM root_budgets rb
             WHERE rb.root_id = (SELECT root_id FROM attempts WHERE run_id = ? AND root_id <> ''
                                 ORDER BY created_at LIMIT 1)
            """,
            (run_id,),
        )

    def root_budget_view(self, root_id: str) -> RootBudgetUsage | None:
        """The read model over one root: recorded counters, never estimates."""
        row = self.root_budget_row(root_id)
        if row is None:
            return None
        binding = RootBudgetBinding(
            root_id=str(row["root_id"]),
            project_id=str(row["project_id"]),
            repo_path=str(row["repo_path"]),
            task_id=str(row["task_id"]),
            ledger_path=str(row["ledger_path"]),
        )
        limits = RootBudgetLimits(
            max_top_level_submissions=int(row["max_top_level_submissions"]),
            max_repairs=int(row["max_repairs"]),
            deadline_seconds=int(row["deadline_seconds"]),
        )
        return RootBudgetUsage(
            binding=binding,
            limits=limits,
            used_top_level_submissions=int(row["used_top_level_submissions"]),
            used_repairs=int(row["used_repairs"]),
            run_ids=list(json.loads(row["run_ids_json"] or "[]")),
            authorization_ids=list(json.loads(row["authorization_ids_json"] or "[]")),
            first_dispatch_at=row["first_dispatch_at"],
            deadline_at=row["deadline_at"],
        )

    # -- one dispatch, one transaction (batch E1) ----------------------------

    def reserve_dispatch(
        self,
        *,
        run_id: str,
        controller_id: str,
        invocation_id: str,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
        attempt_id: str | None = None,
        root_binding: RootBudgetBinding | None = None,
        root_limits: RootBudgetLimits | None = None,
        authorization_id: str = "",
        authorization_max: int | None = None,
        required_loop_remaining: int = 1,
        is_repair: bool = False,
        repo_path: str = "",
        fence: OwnerFence | None = None,
    ) -> DispatchReservation:
        """Reserve one top-level dispatch: every counter, in one transaction.

        ``fence`` (owner lease): the caller's owner token and claim generation. When given, the
        reservation is refused with ``owner_lost`` unless the run still has exactly that owner at
        exactly that generation - checked first, inside the same transaction - so a controller a
        takeover superseded can never buy another dispatch.

        This is the single entry point for both roles. Before it, an implementer dispatch and a
        review dispatch were charged in separate commits (authorization claim, turn reservation,
        invocation registration), so a failure between them could leave an allowance spent with
        no attempt row - or a reviewer bought for a run that then discovered it had no budget.
        Everything below is one ``BEGIN IMMEDIATE``:

        1. the run is owned by this controller, is live, and is in the phase this role belongs
           to (an implementer dispatch only before checks, a reviewer only in ``review``);
        2. no cancellation intent is recorded;
        3. for a root run: the root has no unresolved invocation, is inside its deadline, and
           has room for this submission - including its repair count;
        4. the authorization has room and the run's turn ceiling has room;
        5. the attempt row exists (or is created), the invocation intent is inserted, and the
           root, authorization and run counters are incremented.

        Any failure raises ``StoreError`` and rolls the whole thing back: there is no state in
        which one counter moved and another did not.

        Idempotent by ``invocation_id``: a replay returns the recorded intent with
        ``is_new=False`` and charges nothing, so a retried call cannot start a second process.
        A *different* invocation id for a role that already has a live or reserved invocation is
        refused, which is what stops "a new id" from being used to dispatch twice.

        ``root_binding``/``root_limits`` are ``None`` for a run that is not spent against a root
        ledger (every legacy run, and every offline run without a root budget file). They cannot
        be supplied without an ``authorization_id``: a root with nothing to bind it to is not a
        ledger. A rootless dispatch is refused when this ledger already holds a root for the same
        task - derived from the run's project and task and ``repo_path`` (the run's repository;
        when it is empty, any repository of that project and task matches) - because spending
        beside that root would step around its unresolved invocations, its live run and its
        cumulative ceilings.

        ``is_repair`` is the caller's own claim that this dispatch is the bounded repair round
        (batch E2). It is only ever *added* to the ledger's own root-wide rule: a second
        implementer invocation of a root is a repair whether or not a caller says so, and this
        flag cannot make a repair look like a first attempt. On the legacy path there is no
        invocation row to mark, so the claim travels in the caller's own attempt record; only a
        fully offline run gets there with a repair, because admission refuses a repair policy on
        a real transport without a root.

        What this does *not* do on the legacy path: write ``attempts.invocation_id``. That stays
        with the controller's own stop-aware registration, because a stop racing that write is
        coordinated there (``register_attempt_invocation_unless_stopped``). On the root path the
        invocation row and that column are written here, in the same transaction, so a reader
        never sees one without the other.
        """
        if role not in {"implementer", "reviewer", "planner"}:
            raise StoreError(f"unknown dispatch role {role!r}")
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if existing is not None:
                return DispatchReservation(
                    is_new=False,
                    invocation=_invocation_from_row(existing),
                    attempt_id=str(existing["attempt_id"]),
                    role=str(existing["role"]),
                    detail=(
                        "this invocation id is already recorded; the recorded dispatch stands and "
                        "the caller may only coordinate it, never start it again"
                    ),
                )

            self._check_fence_locked(conn, run_id, fence)
            row = self._guard_dispatch_locked(conn, run_id, controller_id, attempt_id, role)

            if root_binding is None and root_limits is None:
                return self._reserve_legacy_locked(
                    conn,
                    run_id=run_id,
                    controller_id=controller_id,
                    role=role,
                    reservation_id=reservation_id,
                    reserved_turns=reserved_turns,
                    reservation_expires_at=reservation_expires_at,
                    authorization_id=authorization_id,
                    authorization_max=authorization_max,
                    attempt_id=attempt_id,
                    row=row,
                    is_repair=is_repair,
                    repo_path=repo_path,
                )

            if root_binding is None or root_limits is None:
                raise StoreError(
                    "a root budget must be supplied with both its binding and its limits; "
                    "charging a root without knowing its ceiling would be an unapproved spend"
                )
            if not authorization_id:
                raise StoreError(
                    "a root budget can only be charged together with an authorization: a root "
                    "records what an approval spent, and there is no approval here"
                )
            if root_binding.project_id != row["project_id"]:
                raise StoreError(
                    f"root {root_binding.root_id} belongs to project {root_binding.project_id}, "
                    f"not to this run's project {row['project_id']}"
                )
            self._check_authorization_locked(conn, authorization_id, authorization_max)
            self._claim_authorization_locked(conn, authorization_id)
            self._reserve_turn_locked(run_id, controller_id, turns=reserved_turns)
            attempt = self._attempt_for_dispatch_locked(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                task_revision=int(row["task_revision"]),
                role=role,
                reservation_id=reservation_id,
                reserved_turns=reserved_turns,
                reservation_expires_at=reservation_expires_at,
                root_id=root_binding.root_id,
                # Only the role that produces the candidate creates an attempt; the reviewer
                # attaches to it. ``is_repair`` says which implementer attempt this is: the run's
                # first, or the one bounded repair (batch E2).
                create_attempt=role == "implementer",
                is_repair=is_repair,
            )
            round_number = self._next_round_locked(conn, root_binding.root_id)
            charged_as_repair = self._charge_root_locked(
                conn,
                root_binding=root_binding,
                root_limits=root_limits,
                role=role,
                run_id=run_id,
                authorization_id=authorization_id,
                required_loop_remaining=int(required_loop_remaining),
                is_repair=is_repair,
            )
            intent = self._insert_invocation_locked(
                conn,
                invocation_id=invocation_id,
                root_id=root_binding.root_id,
                run_id=run_id,
                attempt_id=str(attempt["attempt_id"]),
                role=role,
                authorization_id=authorization_id,
                round_number=round_number,
                is_repair=charged_as_repair,
            )
            turns_after = conn.execute(
                "SELECT turns_reserved FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return DispatchReservation(
                is_new=True,
                invocation=intent,
                attempt_id=str(attempt["attempt_id"]),
                role=role,
                run_turns_reserved=int(turns_after["turns_reserved"]) if turns_after else 0,
                authorization_used=int(intent.authorization_used_at_reservation),
                detail=f"reserved as round {round_number} of root {root_binding.root_id}",
            )

    def root_ids_for_task(self, *, project_id: str, repo_path: str, task_id: str) -> list[str]:
        """The roots this task already has in this ledger. Read-only.

        The same lookup the rootless dispatch transaction makes, asked by the controller before
        a run row exists: a rootless run of a task that has a root is refused while nothing has
        been recorded, so resubmitting the same TaskSpec with its root still dispatches.
        """
        with self._lock:
            return self._task_root_ids_locked(
                self.conn, project_id=project_id, repo_path=repo_path, task_id=task_id
            )

    def _task_root_ids_locked(
        self, conn: sqlite3.Connection, *, project_id: str, repo_path: str, task_id: str
    ) -> list[str]:
        """Root ids recorded for ``(project_id, repo_path, task_id)``; project-wide without a path."""
        rows = (
            conn.execute(
                "SELECT root_id FROM root_budgets WHERE project_id = ? AND repo_path = ? "
                "AND task_id = ?",
                (project_id, str(Path(repo_path).resolve()), task_id),
            ).fetchall()
            if repo_path
            else conn.execute(
                "SELECT root_id FROM root_budgets WHERE project_id = ? AND task_id = ?",
                (project_id, task_id),
            ).fetchall()
        )
        return [str(found["root_id"]) for found in rows]

    def _reserve_legacy_locked(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        controller_id: str,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
        authorization_id: str,
        authorization_max: int | None,
        attempt_id: str | None,
        row: sqlite3.Row,
        is_repair: bool = False,
        repo_path: str = "",
    ) -> DispatchReservation:
        """The pre-E1 path for a run with no root: authorization, turn and attempt together.

        Still one transaction - that part is not new - but no root counter is involved and no
        invocation row is written, because a legacy run's dispatch facts stay where they always
        were (``attempts.invocation_id`` / ``review_invocation_id``). ``is_repair`` still reaches
        the attempt row here: a fully offline run (the fake driver, no root budget file) performs
        the same bounded repair, it simply has no root ledger to charge it to. A real transport
        never repairs on this path: admission refuses its repair policy without a root binding.

        A task that already has a root in this ledger is refused here, inside the transaction: the
        root's unresolved invocations, its live-run gate and its ceilings are enforced only on the
        root path, so a rootless dispatch beside it would bypass all of them.
        """
        task_roots = self._task_root_ids_locked(
            conn, project_id=row["project_id"], repo_path=repo_path, task_id=row["task_id"]
        )
        if task_roots:
            # The controller refuses this before the run row exists (``root_ids_for_task``), so
            # reaching it here means the root was registered after that check - a race. This
            # run's row already exists, and an identical resubmission only returns it.
            raise StoreError(
                f"the task {row['task_id']} of {row['project_id']} has root "
                f"{task_roots[0]} in this ledger, and this run has no root binding: a "
                "dispatch outside the root would step around its unresolved invocations, its "
                "live run and its ceilings. Nothing was reserved. The root was registered after "
                "this run was admitted, so this run is blocked and resubmitting the same "
                "TaskSpec only returns it: submit a new revision with --root-budget-file (and an "
                "authorization covering that root) to run this task against its root."
            )
        if authorization_id:
            self._check_authorization_locked(conn, authorization_id, authorization_max)
            self._claim_authorization_locked(conn, authorization_id)
        self._reserve_turn_locked(run_id, controller_id, turns=reserved_turns)
        attempt = self._attempt_for_dispatch_locked(
            conn,
            attempt_id=attempt_id,
            run_id=run_id,
            task_revision=int(row["task_revision"]),
            role=role,
            reservation_id=reservation_id,
            reserved_turns=reserved_turns,
            reservation_expires_at=reservation_expires_at,
            root_id="",
            create_attempt=role == "implementer",
            is_repair=is_repair,
        )
        used = 0
        if authorization_id:
            auth_row = conn.execute(
                "SELECT used_top_level_submissions FROM authorizations WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()
            used = int(auth_row["used_top_level_submissions"]) if auth_row else 0
        turns_after = conn.execute(
            "SELECT turns_reserved FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return DispatchReservation(
            is_new=True,
            invocation=None,
            attempt_id=str(attempt["attempt_id"]),
            role=role,
            run_turns_reserved=int(turns_after["turns_reserved"]) if turns_after else 0,
            authorization_used=used,
            detail="legacy dispatch: no root ledger is involved for this run",
        )

    def _guard_dispatch_locked(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        controller_id: str,
        attempt_id: str | None,
        role: str,
    ) -> sqlite3.Row:
        """Ownership, liveness, stop and role/phase checks, all inside the transaction."""
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        if row["claimed_by"] != controller_id:
            raise StoreError(
                f"cannot dispatch for {run_id}: claimed by {row['claimed_by']!r}, "
                f"not {controller_id!r}"
            )
        if row["cancel_intent_at"]:
            raise StoreError(
                f"run {run_id} has a cancellation intent recorded at {row['cancel_intent_at']}; "
                "no dispatch is reserved for a stopped run and no counter moves"
            )
        state = TaskState(row["task_state"])
        if state in {TaskState.ACCEPTED, TaskState.BLOCKED, TaskState.CANCELLED}:
            raise StoreError(
                f"run {run_id} is {state.value}; a terminal run never dispatches again"
            )
        phase = row["phase"]
        if role == "reviewer" and phase != CheckPhase.REVIEW.value:
            raise StoreError(
                f"run {run_id} is in phase {phase!r}, not {CheckPhase.REVIEW.value!r}: a review "
                "dispatch is only reserved once the run has entered review"
            )
        if role == "implementer" and phase is not None:
            raise StoreError(
                f"run {run_id} is already in phase {phase!r}: an implementer dispatch is only "
                "reserved before the candidate is checked"
            )
        live = conn.execute(
            "SELECT attempt_id FROM attempts WHERE run_id = ? AND state IN (?, ?)",
            (run_id, AttemptState.CREATED.value, AttemptState.ACTIVE.value),
        ).fetchall()
        if attempt_id is not None and any(
            str(entry["attempt_id"]) != attempt_id for entry in live
        ):
            others = ", ".join(str(entry["attempt_id"]) for entry in live)
            raise StoreError(f"refusing a second live attempt for {run_id}: {others} is active")
        return row

    def _check_authorization_locked(
        self, conn: sqlite3.Connection, authorization_id: str, authorization_max: int | None
    ) -> None:
        """Refuse unless this authorization exists and has room for one more submission."""
        row = conn.execute(
            "SELECT * FROM authorizations WHERE authorization_id = ?", (authorization_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown authorization {authorization_id}")
        if authorization_max is not None and int(row["max_top_level_submissions"]) != int(
            authorization_max
        ):
            raise StoreError(
                f"authorization {authorization_id} is recorded with a ceiling of "
                f"{row['max_top_level_submissions']} but this run presents {authorization_max}; "
                "the recorded approval stands"
            )
        if int(row["used_top_level_submissions"]) + 1 > int(row["max_top_level_submissions"]):
            raise StoreError(
                f"authorization {authorization_id} is exhausted: "
                f"{row['used_top_level_submissions']}/{row['max_top_level_submissions']} "
                "top-level submissions used"
            )

    def _claim_authorization_locked(self, conn: sqlite3.Connection, authorization_id: str) -> None:
        """The authorization's own gate: the UPDATE is the check, the CHECK constraint the backstop."""
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
            raise StoreError(
                f"authorization {authorization_id} could not be charged; nothing was reserved"
            )

    def _attempt_for_dispatch_locked(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str | None,
        run_id: str,
        task_revision: int,
        role: str,
        reservation_id: str,
        reserved_turns: int,
        reservation_expires_at: str,
        root_id: str,
        create_attempt: bool = True,
        is_repair: bool = False,
    ) -> sqlite3.Row:
        """The attempt this dispatch belongs to: the run's own, or one created for it.

        Two roles, two invocations, **one attempt**: the reviewer reviews what the implementer
        produced on the same attempt row, which is why a non-implementer dispatch must find that
        row rather than create one.

        An implementer dispatch may be followed by exactly one *repair* attempt (batch E2) on the
        same revision. ``is_repair`` says which of the two this dispatch is, and the lookup and the
        insert both use it: a second first attempt is refused here, a second repair is refused by
        the same statement and by the schema's ``UNIQUE (run_id, task_revision, role, is_repair)``.

        ``create_attempt=False`` makes this a pure lookup: a role that reviews work somebody else
        produced can never be the reason an attempt exists.
        """
        now = utc_now()
        column = "invocation_id" if role == "implementer" else "review_invocation_id"
        if role == "implementer" and not create_attempt:
            raise StoreError(f"role {role!r} creates the attempt it dispatches; it cannot look one up")
        if role == "implementer":
            existing = conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? AND task_revision = ? AND role = ? "
                "AND is_repair = ?",
                (run_id, task_revision, role, 1 if is_repair else 0),
            ).fetchone()
            if existing is not None:
                what = "repair attempt" if is_repair else "first implementation attempt"
                raise StoreError(
                    f"run {run_id} already has a {what} {existing['attempt_id']} for revision "
                    f"{task_revision}; one run buys one first attempt and at most one repair, and a "
                    "new invocation id does not make it a different attempt"
                )
            created_id = attempt_id or new_attempt_id()
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, run_id, task_revision, role, state, reservation_id,
                    reserved_agent_turns, reserved_expires_at, created_at, root_id, is_repair
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_id,
                    run_id,
                    task_revision,
                    role,
                    AttemptState.ACTIVE.value,
                    reservation_id,
                    reserved_turns,
                    reservation_expires_at,
                    now,
                    root_id,
                    1 if is_repair else 0,
                ),
            )
            conn.execute(
                "UPDATE runs SET current_attempt_id = ?, task_state = ?, updated_at = ? WHERE run_id = ?",
                (created_id, TaskState.RUNNING.value, now, run_id),
            )
            attempt_id = created_id
        else:
            # A review is not its own attempt. The reviewer reviews the *implementer's* attempt:
            # the row that carries the candidate, the evidence and the frozen fingerprint. The
            # role on the attempt is ``implementer``, so looking it up by this dispatch's role
            # would find nothing and refuse every review.
            #
            # Which row is resolved from ``runs.current_attempt_id`` rather than by taking the
            # oldest row of the revision. A repaired run has two implementer attempts for one
            # revision, and the reviewer must attach to the one the run is working on now -
            # otherwise round two's verdict would be recorded against round one's candidate.
            current = conn.execute(
                "SELECT current_attempt_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise RunNotFound(run_id)
            named = str(current["current_attempt_id"] or "")
            existing = (
                conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id = ? AND run_id = ?", (named, run_id)
                ).fetchone()
                if named
                else None
            )
            if existing is not None and int(existing["task_revision"]) != int(task_revision):
                raise StoreError(
                    f"run {run_id}: its current attempt {named} is for revision "
                    f"{existing['task_revision']}, but this {role} dispatch is for revision "
                    f"{task_revision}; the verdict would be recorded against another candidate"
                )
            if existing is None:
                raise StoreError(
                    f"no current attempt exists for run {run_id} revision {task_revision}: a "
                    f"{role} dispatch reviews an attempt the run is already working on"
                )
            if not create_attempt and existing[column]:
                # One review per attempt, whatever the caller names it. Checking only for a
                # *pending* invocation is not enough: the first reviewer settles when its verdict
                # is recorded, and a second id would then buy a second review of the same
                # candidate - and overwrite the attempt's record of the first.
                raise StoreError(
                    f"run {run_id} revision {task_revision} already dispatched a {role} "
                    f"invocation ({existing[column]}); this build buys one review per candidate, "
                    "and a new invocation id does not make it a different review"
                )
            attempt_id = str(existing["attempt_id"])
            if existing["root_id"] in ("", None):
                conn.execute(
                    "UPDATE attempts SET root_id = ? WHERE attempt_id = ?", (root_id, attempt_id)
                )
        row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:  # pragma: no cover - the inserts above guarantee the row
            raise StoreError(f"attempt {attempt_id} vanished inside its own transaction")
        return row

    def _next_round_locked(self, conn: sqlite3.Connection, root_id: str) -> int:
        """The next round number of this root: a counter, not a per-run counter.

        Rounds are numbered across every run and revision of the root, so two runs of the same
        task cannot both call themselves "round 1".
        """
        row = conn.execute(
            "SELECT COALESCE(MAX(round), 0) AS highest FROM invocations WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        return int(row["highest"]) + 1

    def _charge_root_locked(
        self,
        conn: sqlite3.Connection,
        *,
        root_binding: RootBudgetBinding,
        root_limits: RootBudgetLimits,
        role: str,
        run_id: str,
        authorization_id: str,
        required_loop_remaining: int = 1,
        is_repair: bool = False,
    ) -> bool:
        """Charge the root's counters, creating the row if this is the root's first dispatch.

        Returns whether this dispatch is a repair. The rules, in full:

        * the root's first implementer dispatch is not a repair, and any later implementer
          dispatch for the same root is one - including one that arrives under a new revision or
          a new run, because the repair allowance belongs to the task, not to a run's own idea of
          "first attempt";
        * a caller's ``is_repair`` is **added** to that rule and can never subtract from it: a
          repair cannot be disguised as a first attempt, and a run that knows it is repairing is
          charged as one even where the root-wide count alone would not say so;
        * a reviewer dispatch never consumes a repair;
        * an unknown outcome never refunds anything: only this method increments. The one
          decrement in this build is an operator's ``void`` of a ``launch_unknown`` entry
          (``settle_by_operator``), which returns exactly what that dispatch charged and is
          recorded as an attestation; a voided implementer entry therefore does not count as the
          root's first implementer attempt either;
        * the root must be able to pay for the run's *whole remaining loop*
          (``required_loop_remaining``), not merely for this one dispatch: buying an
          implementation whose review cannot be afforded is the half-loop the ceiling check
          refuses.
        """
        now = utc_now()
        row = conn.execute(
            "SELECT * FROM root_budgets WHERE root_id = ?", (root_binding.root_id,)
        ).fetchone()
        if row is None:
            # The same task under a different root id. Checked here as well as in
            # ``register_root_budget`` because this path can create the row, and without the
            # check the unique index would raise a bare ``sqlite3.IntegrityError`` - a crash
            # where every other refusal is a ``StoreError`` the controller can report.
            clash = conn.execute(
                "SELECT root_id FROM root_budgets WHERE project_id = ? AND repo_path = ? AND task_id = ?",
                (root_binding.project_id, root_binding.repo_path, root_binding.task_id),
            ).fetchone()
            if clash is not None:
                raise StoreError(
                    f"the task {root_binding.task_id} of {root_binding.project_id} at "
                    f"{root_binding.repo_path} already has root {clash['root_id']}; this run "
                    f"resolves {root_binding.root_id} for the same task, and a second root would "
                    "hand it a second allowance"
                )
            conn.execute(
                """
                INSERT INTO root_budgets (
                    root_id, project_id, task_id, repo_path, ledger_path,
                    max_top_level_submissions, max_repairs, deadline_seconds,
                    limits_digest, binding_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    root_binding.root_id,
                    root_binding.project_id,
                    root_binding.task_id,
                    root_binding.repo_path,
                    root_binding.ledger_path,
                    root_limits.max_top_level_submissions,
                    root_limits.max_repairs,
                    root_limits.deadline_seconds,
                    root_limits.digest(),
                    digest_of(root_binding.model_dump(mode="json")),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM root_budgets WHERE root_id = ?", (root_binding.root_id,)
            ).fetchone()
        else:
            self._check_root_limits_locked(conn, row, root_binding, root_limits)

        # An unresolved invocation blocks the whole root, whatever run or revision asks next.
        # This is the "an unknown outcome does not unlock the same task" rule, enforced where
        # the decision is actually made rather than in a pre-flight read that a sibling process
        # could invalidate.
        pending = conn.execute(
            f"SELECT invocation_id, role, state FROM invocations "
            f" WHERE root_id = ? AND state IN ({','.join('?' for _ in INVOCATION_UNRESOLVED_STATES)})"
            f" ORDER BY reserved_at",
            (root_binding.root_id, *INVOCATION_UNRESOLVED_STATES),
        ).fetchall()
        if pending:
            first = pending[0]
            raise StoreError(
                f"root {root_binding.root_id} still has {len(pending)} unresolved invocation(s); "
                f"the earliest is {first['invocation_id']} ({first['role']}, "
                f"{first['state']}). No dispatch is reserved for this root until it is settled - "
                "an unknown outcome is reconciled by an operator, never re-dispatched."
            )

        # --- the root's active run owns it exclusively -----------------------------------
        # An unresolved invocation is not the only way a root is busy. Between the implementer's
        # settlement and the release of the run - while the approved checks run, while the review
        # is being dispatched, while a verdict is rendered - there is no pending invocation and
        # the work is still going on. Without this check a second revision could take the same
        # root concurrently, which is one root running two tasks: the exact thing "one active run
        # per root" exists to prevent.
        other_live = conn.execute(
            """
            SELECT run_id, task_revision, task_state FROM runs
             WHERE run_id IN (SELECT DISTINCT run_id FROM invocations WHERE root_id = ?)
               AND run_id <> ?
               AND task_state NOT IN (?, ?, ?)
             ORDER BY created_at
            """,
            (
                root_binding.root_id,
                run_id,
                TaskState.ACCEPTED.value,
                TaskState.BLOCKED.value,
                TaskState.CANCELLED.value,
            ),
        ).fetchall()
        if other_live:
            other = other_live[0]
            raise StoreError(
                f"root {root_binding.root_id} is owned by run {other['run_id']} "
                f"(revision {other['task_revision']}, state {other['task_state']}); that run has "
                f"not reached a terminal state, so no dispatch is reserved for this root from run "
                f"{run_id}. One root runs one task at a time - wait for the active run to finish, "
                "be stopped, or be cancelled."
            )

        deadline_at = row["deadline_at"]
        if deadline_at and parse_ts(str(deadline_at)) <= parse_ts(now):
            raise StoreError(
                f"root {root_binding.root_id} reached its deadline at {deadline_at}; no further "
                "dispatch is reserved for this root"
            )
        # --- the whole remaining loop has to fit, not just this dispatch -------------------
        # Admitting a revision that can afford its implementer but not its reviewer buys an
        # implementation and then blocks - the same "half a loop, fully paid for" outcome the
        # authorization gate already refuses. ``required_loop_remaining`` is how many top-level
        # reservations this run must still be able to make (the one being asked for, plus the
        # review turn a required review will need), so the ceiling is tested against that.
        remaining_loop = max(1, int(required_loop_remaining))
        if int(row["used_top_level_submissions"]) + remaining_loop > int(
            row["max_top_level_submissions"]
        ):
            raise StoreError(
                f"root {root_binding.root_id} cannot complete this run's loop: "
                f"{row['used_top_level_submissions']}/{row['max_top_level_submissions']} "
                f"top-level submissions are used and this run still needs {remaining_loop} more "
                f"(this {role} dispatch plus the rest of the loop). No dispatch is reserved and no "
                "counter moves - obtain a root budget large enough for the whole loop, or stop "
                "here. There is no top-up path in this build."
            )

        try:
            run_ids = json.loads(row["run_ids_json"] or "[]")
            authorization_ids = json.loads(row["authorization_ids_json"] or "[]")
        except json.JSONDecodeError:
            run_ids, authorization_ids = [], []
        if run_id not in run_ids:
            run_ids.append(run_id)
        if authorization_id not in authorization_ids:
            authorization_ids.append(authorization_id)

        # A ``void`` operator settlement returned this entry's charge to the root, so it is not
        # the root's first implementer attempt either: counting it would charge the next
        # implementer as a repair the void just gave back. ``consumed`` entries still count.
        implementers_before = self._charged_implementer_rows_locked(conn, root_binding.root_id)
        is_repair = bool(is_repair) or (role == "implementer" and bool(implementers_before))
        if is_repair and int(row["used_repairs"]) + 1 > int(row["max_repairs"]):
            raise StoreError(
                f"root {root_binding.root_id} has used its {row['max_repairs']} repair attempt(s); "
                "this build does not buy another implementation attempt, and changing the "
                "revision does not reset the count"
            )
        first_dispatch_at = row["first_dispatch_at"] or now
        conn.execute(
            """
            UPDATE root_budgets
               SET used_top_level_submissions = used_top_level_submissions + 1,
                   used_repairs = used_repairs + ?,
                   run_ids_json = ?,
                   authorization_ids_json = ?,
                   first_dispatch_at = ?,
                   deadline_at = ?,
                   updated_at = ?
             WHERE root_id = ?
            """,
            (
                1 if is_repair else 0,
                canonical_json(run_ids),
                canonical_json(authorization_ids),
                first_dispatch_at,
                row["deadline_at"]
                or _add_seconds(first_dispatch_at, int(row["deadline_seconds"])),
                now,
                root_binding.root_id,
            ),
        )
        return is_repair

    def _insert_invocation_locked(
        self,
        conn: sqlite3.Connection,
        *,
        invocation_id: str,
        root_id: str,
        run_id: str,
        attempt_id: str,
        role: str,
        authorization_id: str,
        round_number: int,
        is_repair: bool,
    ) -> InvocationIntent:
        """Write the dispatch record, in the same transaction as the counters above."""
        now = utc_now()
        root_row = conn.execute(
            "SELECT used_top_level_submissions FROM root_budgets WHERE root_id = ?", (root_id,)
        ).fetchone()
        auth_row = conn.execute(
            "SELECT used_top_level_submissions FROM authorizations WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO invocations (
                invocation_id, root_id, run_id, attempt_id, role, authorization_id,
                round, is_repair, state, reserved_at,
                root_used_at_reservation, authorization_used_at_reservation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invocation_id,
                root_id,
                run_id,
                attempt_id,
                role,
                authorization_id,
                round_number,
                1 if is_repair else 0,
                InvocationStartState.RESERVED.value,
                now,
                int(root_row["used_top_level_submissions"]) if root_row else 0,
                int(auth_row["used_top_level_submissions"]) if auth_row else 0,
            ),
        )
        # The role's invocation id stays recorded on the attempt row as well: ``cancel`` routes a
        # stop through it, and a reader that predates the ledger still finds it. Written in this
        # same transaction, so it can never disagree with the invocation record.
        column = "invocation_id" if role == "implementer" else "review_invocation_id"
        conn.execute(
            f"UPDATE attempts SET {column} = ? WHERE attempt_id = ?", (invocation_id, attempt_id)
        )
        return _invocation_from_row(
            conn.execute(
                "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
        )

    # -- invocation lifecycle ------------------------------------------------

    def invocation(self, invocation_id: str) -> InvocationIntent | None:
        row = self._fetchone(
            "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
        )
        return _invocation_from_row(row) if row is not None else None

    def invocations_for(self, run_id: str) -> list[InvocationIntent]:
        return [
            _invocation_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM invocations WHERE run_id = ? ORDER BY reserved_at, rowid", (run_id,)
            )
        ]

    def invocations_for_root(self, root_id: str) -> list[InvocationIntent]:
        return [
            _invocation_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM invocations WHERE root_id = ? ORDER BY reserved_at, rowid",
                (root_id,),
            )
        ]

    @staticmethod
    def _charged_implementer_rows_locked(
        conn: sqlite3.Connection, root_id: str
    ) -> list[sqlite3.Row]:
        """Read implementation charges through the caller's existing lock/transaction."""
        return list(conn.execute(
            "SELECT i.* FROM invocations i WHERE i.root_id = ?"
            " AND i.role = 'implementer' AND NOT EXISTS (SELECT 1 FROM invocation_settlements"
            " s WHERE s.invocation_id = i.invocation_id AND s.settled_as = 'void')"
            " ORDER BY i.reserved_at, i.rowid",
            (root_id,),
        ))

    def charged_implementers_for_root(self, root_id: str) -> list[InvocationIntent]:
        """Implementations still charged to this root; a consumed settlement still counts.

        A void returns the root charge without deleting history or refunding the run or approval.
        Admission and dispatch must use the same history when deciding whether the next
        implementation consumes a repair. Unresolved entries are included, not forgiven.
        """
        with self._lock:
            return [
                _invocation_from_row(row)
                for row in self._charged_implementer_rows_locked(self.conn, root_id)
            ]

    def pending_invocations(self, root_id: str) -> list[InvocationIntent]:
        return [entry for entry in self.invocations_for_root(root_id) if entry.pending]

    def mark_invocation_launch_requested(self, invocation_id: str) -> bool:
        """Record that the controller is about to ask a driver to launch this invocation.

        Deliberately *not* a start. The last moment at which "no process exists" is certainly true
        is this one, so recording a request here is honest; recording a start here is not, because
        the driver may still win a stop in its own gate and create nothing. The process fact comes
        from the driver through :meth:`record_invocation_spawn`.

        A crash after this and before any spawn report leaves ``REQUESTED``: a launch that may
        have been asked for, with no process known. That keeps the root blocked without claiming
        work began.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE invocations
                   SET state = ?, launch_requested_at = ?
                 WHERE invocation_id = ? AND state = ?
                """,
                (
                    InvocationStartState.REQUESTED.value,
                    utc_now(),
                    invocation_id,
                    InvocationStartState.RESERVED.value,
                ),
            )
            if cur.rowcount == 1:
                return True
            # A legacy row (storage v2) has no launch_requested_at and may already be STARTED;
            # the request still has to be recorded, but the state it carries is not downgraded.
            cur = conn.execute(
                """
                UPDATE invocations SET launch_requested_at = ?
                 WHERE invocation_id = ? AND launch_requested_at IS NULL
                """,
                (utc_now(), invocation_id),
            )
            return cur.rowcount == 1

    def record_invocation_spawn(self, fact: SpawnFact) -> bool:
        """Record what a driver observed at the moment its launch decision was final.

        Three separate facts come out of this, and the whole point is that they are separate:

        * ``created`` says the **launch happened**. It sets ``started_at`` and the ``started``
          state, whatever the launch physically creates;
        * ``pid`` says an **operating-system child exists**. Only that fills
          ``process_started_at``/``process_pid``, and only that is counted as a process;
        * ``spawn_kind`` says what this driver's launch creates. The offline driver reports
          ``no_process``: its work happens, no child appears, and a process count must not
          invent one.

        ``created=False`` records ``NOT_STARTED`` - a launch that did not happen, whether a stop
        won the gate or the client failed to start. The consumption stays either way: the
        reservation was committed before this fact was knowable.

        A report never downgrades a row that has already gone further (a settled or unknown
        invocation), and a later "nothing happened" cannot unrecord a launch that did.

        A report that arrives after the entry was closed as ``launch_unknown`` (by a reconcile,
        or by a confirmed stop that saw no report) never reopens it. The result is what the
        closure would have recorded had the report come first, so the two orders agree:

        * ``created=True`` records the launch and its process facts, and the entry becomes
          ``unknown`` - a known launch whose result nobody observed. Returning it to ``started``
          would make it *open* again, so the next settlement could close it and release a root
          the closure kept blocked;
        * ``created=False`` records ``not_started``, exactly as before the closure: the driver's
          own word that no launch happened is the fact the closure was missing, and a closure
          leaves a ``not_started`` entry alone.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state, started_at, process_started_at FROM invocations WHERE invocation_id = ?",
                (fact.invocation_id,),
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown invocation {fact.invocation_id}")
            now = utc_now()
            current = InvocationStartState(str(row["state"]))
            # ``OPERATOR_SETTLED`` is final too: a late report must neither reopen an entry an
            # operator closed (re-blocking a root a new revision may already be using) nor
            # rewrite it as ``not_started``. The attestation row stays the record.
            if current in {
                InvocationStartState.SETTLED,
                InvocationStartState.UNKNOWN,
                InvocationStartState.OPERATOR_SETTLED,
            }:
                return False
            if fact.created and current is InvocationStartState.LAUNCH_UNKNOWN:
                conn.execute(
                    """
                    UPDATE invocations
                       SET state = ?, started_at = ?, launch_requested_at =
                               COALESCE(launch_requested_at, ?),
                           process_started_at = COALESCE(?, process_started_at),
                           process_pid = COALESCE(?, process_pid),
                           spawn_kind = ?,
                           detail = ?
                     WHERE invocation_id = ? AND state = ?
                    """,
                    (
                        InvocationStartState.UNKNOWN.value,
                        now,
                        now,
                        now if fact.pid is not None else None,
                        fact.pid,
                        fact.spawn_kind.value,
                        (
                            "a spawn report arrived after this entry was closed as "
                            f"launch_unknown ({fact.detail[:400]}); the launch is recorded and "
                            "its result was never observed, so the entry is unknown and keeps "
                            "blocking the root"
                        ),
                        fact.invocation_id,
                        InvocationStartState.LAUNCH_UNKNOWN.value,
                    ),
                )
                return True
            if fact.created:
                process_started_at = now if fact.pid is not None else None
                conn.execute(
                    """
                    UPDATE invocations
                       SET state = ?, started_at = ?, launch_requested_at =
                               COALESCE(launch_requested_at, ?),
                           process_started_at = COALESCE(?, process_started_at),
                           process_pid = COALESCE(?, process_pid),
                           spawn_kind = ?,
                           detail = ?
                     WHERE invocation_id = ?
                    """,
                    (
                        InvocationStartState.STARTED.value,
                        now,
                        now,
                        process_started_at,
                        fact.pid,
                        fact.spawn_kind.value,
                        fact.detail[:1000],
                        fact.invocation_id,
                    ),
                )
                return True
            if current is InvocationStartState.NOT_STARTED:
                return False  # already recorded as never launched; keep the first reason
            if row["started_at"] is not None or row["process_started_at"] is not None:
                # A launch is already recorded. A later "nothing happened" cannot unrecord it:
                # the earlier report is a fact and this one is a contradiction, not a correction.
                conn.execute(
                    "UPDATE invocations SET detail = ? WHERE invocation_id = ?",
                    (
                        f"a later spawn report contradicted the recorded launch "
                        f"({fact.detail[:400]}); the recorded launch stands",
                        fact.invocation_id,
                    ),
                )
                return False
            conn.execute(
                """
                UPDATE invocations
                   SET state = ?, started_at = NULL, process_started_at = NULL,
                       process_pid = NULL, spawn_kind = ?,
                       launch_requested_at = COALESCE(launch_requested_at, ?),
                       detail = ?
                 WHERE invocation_id = ?
                """,
                (
                    InvocationStartState.NOT_STARTED.value,
                    fact.spawn_kind.value,
                    now,
                    fact.detail[:1000],
                    fact.invocation_id,
                ),
            )
            return True

    def mark_invocation_started(
        self, invocation_id: str, *, process_created: bool = True, pid: int | None = None
    ) -> bool:
        """Record a launch that happened, without a driver-reported spawn fact.

        Kept for store-level callers and for a driver that does not implement the spawn report.
        The controller does **not** use it on the launch path: it records a launch *request*
        before asking a driver and takes the launch and process facts from the driver's report,
        because assuming that asking means launching is the error this split exists to prevent.

        ``process_created`` is what decides the process fact: a caller that knows no child exists
        (the offline path) must pass ``process_created=False``, which records the launch without
        claiming a process. The default is the conservative one for a caller that says nothing -
        it is a *fact* claim, so the caller has to make it deliberately.
        """
        now = utc_now()
        with self.transaction() as conn:
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

    def settle_invocation(
        self, invocation_id: str, *, outcome: InvocationOutcome | None, detail: str = ""
    ) -> bool:
        """Close an *open* invocation with an observed outcome. Never decrements a counter.

        ``None`` (or ``OUTCOME_UNKNOWN``) records ``UNKNOWN``, which keeps blocking the root: the
        consumption stays because a provider may have been billed, and there is no automatic
        refund, retry or prompt replay anywhere in this build.

        **A compare-and-set from the open states only** (``INVOCATION_OPEN_STATES``). ``True``
        when this call closed the entry; ``False`` when the entry was no longer open, in which
        case its recorded state stands and the caller records why it was not settled:

        * ``NOT_STARTED`` records a fact - no launch happened - and a later driver-side
          ``cancelled`` observation cannot undo it: the process the driver is reporting on did not
          exist. The reported outcome is still attached, so a reader sees both "never launched"
          and what was reported;
        * ``SETTLED`` is terminal: a second settlement (a stop that lands after the result was
          applied) would rewrite a recorded ``completed`` as ``cancelled``;
        * ``UNKNOWN`` and ``LAUNCH_UNKNOWN`` are closed only by an operator's reconcile. A local
          stop or a late driver result is not evidence about what an unobserved invocation did,
          and settling one would unblock the root it is supposed to keep blocked.
        """
        state = (
            InvocationStartState.UNKNOWN
            if outcome is None or outcome is InvocationOutcome.OUTCOME_UNKNOWN
            else InvocationStartState.SETTLED
        )
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE invocations
                   SET state = ?, outcome = ?, settled_at = ?, detail = ?
                 WHERE invocation_id = ?
                   AND state IN ({','.join('?' for _ in INVOCATION_OPEN_STATES)})
                """,
                (
                    state.value,
                    outcome.value if outcome is not None else None,
                    utc_now(),
                    detail[:1000],
                    invocation_id,
                    *INVOCATION_OPEN_STATES,
                ),
            )
            if cur.rowcount == 1:
                return True
            row = conn.execute(
                "SELECT state FROM invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown invocation {invocation_id}")
            if row["state"] == InvocationStartState.NOT_STARTED.value:
                conn.execute(
                    """
                    UPDATE invocations SET outcome = ?, detail = ?
                     WHERE invocation_id = ? AND state = ?
                    """,
                    (
                        outcome.value if outcome is not None else None,
                        detail[:1000],
                        invocation_id,
                        InvocationStartState.NOT_STARTED.value,
                    ),
                )
            return False

    def mark_launch_unresolved(self, invocation_id: str, detail: str) -> None:
        """Record that a launch was requested and nobody can say whether it happened.

        The state that keeps a root blocked without claiming anything: no launch is recorded, no
        process is counted, and the entry stays pending until an operator reconciles it. Used when
        a stop is confirmed after the request and before any spawn report - including a forced
        stop of a process that really existed, which is precisely why an empty timestamp cannot be
        read as "nothing was launched".

        **Idempotent.** An entry can be closed this way twice - ``reconcile`` closes an
        interrupted launch as ``launch_unknown``, and a later caller may try the same closure - and
        the second call must not raise. Only the detail is refreshed; the state, the consumption
        and the "not re-dispatched" boundary are unchanged. (The controller's confirmed stop no
        longer calls this for an entry that is already ``launch_unknown``: it closes only open
        entries, see :data:`INVOCATION_OPEN_STATES`.)

        A row that already recorded a *launch* is refused rather than rewritten: a known launch is
        not an unknown one, and this method exists to avoid merging them.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state, started_at FROM invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown invocation {invocation_id}")
            current = InvocationStartState(str(row["state"]))
            if current is InvocationStartState.LAUNCH_UNKNOWN:
                conn.execute(
                    "UPDATE invocations SET detail = ? WHERE invocation_id = ? AND state = ?",
                    (
                        detail[:1000],
                        invocation_id,
                        InvocationStartState.LAUNCH_UNKNOWN.value,
                    ),
                )
                return
            # A ``started`` row with no ``started_at`` is what the v3 migration leaves of a v2
            # ``started`` row (v2 wrote that state before any process existed): it records a launch
            # request and no observed launch or process - exactly what ``launch_unknown`` means.
            unobserved_start = (
                current is InvocationStartState.STARTED and row["started_at"] is None
            )
            if not unobserved_start and current not in {
                InvocationStartState.REQUESTED,
                InvocationStartState.RESERVED,
            }:
                raise StoreError(
                    f"invocation {invocation_id} recorded a launch (state {current.value}); a "
                    "known launch is not an unresolved one, and its recorded state stands"
                )
            conn.execute(
                """
                UPDATE invocations SET state = ?, detail = ?
                 WHERE invocation_id = ?
                   AND (state IN (?, ?) OR (state = ? AND started_at IS NULL))
                """,
                (
                    InvocationStartState.LAUNCH_UNKNOWN.value,
                    detail[:1000],
                    invocation_id,
                    InvocationStartState.REQUESTED.value,
                    InvocationStartState.RESERVED.value,
                    InvocationStartState.STARTED.value,
                ),
            )

    def mark_invocation_not_started(self, invocation_id: str, detail: str) -> None:
        """Record a reservation that provably never reached a launch.

        The allowance is **kept**: this is a fact about what happened, not a refund. It only stops
        a run that never launched anything from looking like a run that did. Applied from
        ``RESERVED`` (no driver was asked) and from ``REQUESTED`` (a driver was asked and reported
        creating nothing); a row that has gone further is left alone.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE invocations
                   SET state = ?, detail = ?
                 WHERE invocation_id = ? AND state IN (?, ?)
                """,
                (
                    InvocationStartState.NOT_STARTED.value,
                    detail[:1000],
                    invocation_id,
                    InvocationStartState.RESERVED.value,
                    InvocationStartState.REQUESTED.value,
                ),
            )

    def mark_unsettled_invocations_unknown(
        self,
        run_id: str,
        detail: str,
        *,
        note_if_closed: str | None = None,
        closed_before: int = 0,
    ) -> int:
        """Close the loop on a controller that died mid-dispatch.

        Two outcomes, because the ledger knows two different things:

        * a process was created (``started_at`` set) -> ``UNKNOWN``: work really began and its
          result was never observed, so it blocks until an operator reconciles;
        * no launch was recorded (``requested``, and also ``reserved`` - an entry no driver was
          asked to launch yet) -> ``LAUNCH_UNKNOWN``: a transport call may have been made and no
          process is known. It still blocks (the spend is unresolvable without looking), but it
          is not counted as ``ever_started``, because no process was ever reported.

        A row that a driver explicitly reported as creating nothing stays ``NOT_STARTED``: nothing
        is unknown about it, and inventing an unknown would block the root for no reason.

        ``note_if_closed`` is a run note recorded verbatim (not cut to the default bound) in the
        **same** transaction, and only when an entry was closed - by this statement, or by the
        caller's own earlier write (``closed_before``). ``resume --legacy-owner-gone`` records the
        operator's attestation this way: a closure that names an attestation and the attestation's
        words commit together, and a failed note write rolls the closure back with it.
        """
        with self.transaction() as conn:
            closed = self._mark_unsettled_invocations_unknown_locked(conn, run_id, detail)
            if note_if_closed is not None and closed + closed_before > 0:
                self._record_note_locked(conn, run_id, note_if_closed, limit=len(note_if_closed))
            return closed

    def _mark_unsettled_invocations_unknown_locked(
        self, conn: sqlite3.Connection, run_id: str, detail: str
    ) -> int:
        """The body of :meth:`mark_unsettled_invocations_unknown`, inside a caller's transaction."""
        launched = conn.execute(
            """
            UPDATE invocations
               SET state = ?, detail = ?
             WHERE run_id = ? AND state = ? AND started_at IS NOT NULL
            """,
            (
                InvocationStartState.UNKNOWN.value,
                detail[:1000],
                run_id,
                InvocationStartState.STARTED.value,
            ),
        )
        # Every open row with no recorded launch (``started_at`` empty) is a
        # requested-but-unconfirmed one: ``requested`` and ``reserved``, and the ``started``
        # row a v2 ledger leaves after the v3 migration cleared its ``started_at`` (v2 wrote
        # that state before any process existed). A launch may have happened, so the root
        # stays blocked, and no process is known, so no process count may include it.
        launch_only = conn.execute(
            """
            UPDATE invocations
               SET state = ?, detail = ?
             WHERE run_id = ? AND state IN (?, ?, ?) AND started_at IS NULL
            """,
            (
                InvocationStartState.LAUNCH_UNKNOWN.value,
                detail[:1000],
                run_id,
                InvocationStartState.REQUESTED.value,
                InvocationStartState.RESERVED.value,
                InvocationStartState.STARTED.value,
            ),
        )
        return int(launched.rowcount + launch_only.rowcount)

    def unstarted_invocations(self, run_id: str) -> list[InvocationIntent]:
        """Invocations whose allowance was charged but for which no process was reported.

        This is the read a report uses to keep reservations, launches and processes apart: a
        ``RESERVED``, ``REQUESTED`` or ``NOT_STARTED`` entry is a consumed allowance with no known
        child, never a call.
        """
        return [
            entry
            for entry in self.invocations_for(run_id)
            if entry.started_at is None
        ]

    def invocation_state_counts(self, run_id: str) -> InvocationStateCounts:
        """Per-state counts, plus the process facts, in one statement (one snapshot, no skew).

        ``processes`` comes from ``process_started_at`` - what a driver reported about the
        machine - and never from a state or a result. That is the whole point of the split: an
        offline invocation that completed in-process is ``settled`` with ``no_process``, and a
        request whose report never arrived is ``launch_unknown`` with nothing.
        """
        rows = self._fetchall(
            """
            SELECT state, COUNT(*) AS n,
                   SUM(CASE WHEN process_started_at IS NOT NULL THEN 1 ELSE 0 END) AS with_process,
                   SUM(CASE WHEN spawn_kind = ? THEN 1 ELSE 0 END) AS childless
              FROM invocations WHERE run_id = ? GROUP BY state
            """,
            (SpawnKind.NO_PROCESS.value, run_id),
        )
        counts = InvocationStateCounts()
        for row in rows:
            state = InvocationStartState(str(row["state"]))
            number = int(row["n"])
            counts.processes += int(row["with_process"])
            counts.childless_launches += int(row["childless"])
            if state is InvocationStartState.RESERVED:
                counts.reserved = number
            elif state is InvocationStartState.REQUESTED:
                counts.requested = number
            elif state is InvocationStartState.STARTED:
                counts.started = number
            elif state is InvocationStartState.NOT_STARTED:
                counts.not_started = number
            elif state is InvocationStartState.SETTLED:
                counts.settled = number
            elif state is InvocationStartState.LAUNCH_UNKNOWN:
                counts.launch_unknown = number
            elif state is InvocationStartState.OPERATOR_SETTLED:
                counts.operator_settled = number
            else:
                counts.unknown = number
        counts.open = counts.reserved + counts.requested + counts.started
        return counts

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
        """Record the implementer's invocation id on its attempt. **Unconditional.**

        Not what a controller should call before starting an implementer: a stop that commits
        between the reservation and this write would be missed, and the driver started anyway.
        :meth:`register_attempt_invocation_unless_stopped` is the conditional form the controller
        uses; this one stays for store-level tests and callers that hold their own coordination.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET invocation_id = ? WHERE attempt_id = ?",
                (invocation_id, attempt_id),
            )

    def record_review_invocation(self, attempt_id: str, invocation_id: str) -> None:
        """Track the review invocation separately from the implementer invocation.

        Review runs as its own process with its own ID - it is not a continuation of the
        implementer session - even though both are recorded against the same attempt.

        Unconditional, and therefore **not** what a controller should call before starting a
        reviewer: a stop commits between this write and the driver launch it would miss.
        :meth:`register_attempt_invocation_unless_stopped` is the conditional form the
        controller uses; this one stays for the store-level tests and callers that hold their
        own coordination.
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

    def attach_review_result_unless_stopped(
        self, run_id: str, attempt_id: str, payload: dict[str, Any]
    ) -> bool:
        """Apply a reviewer result to its attempt **unless the run's stop is recorded**, atomically.

        The reviewer's form of ``finish_attempt(unless_stopped=True)``: the stop decision and the
        first write of the result are one statement, so a stop committed at any point up to it
        wins. ``True`` when the result was attached; ``False`` when a stop is recorded, in which
        case nothing was written and the caller treats the result as a late one. An unknown
        attempt is refused with ``StoreError``, as :meth:`attach_review_result` does.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE attempts SET review_json = ?
                 WHERE attempt_id = ?
                   AND (SELECT cancel_intent_at FROM runs WHERE run_id = ?) IS NULL
                """,
                (canonical_json(payload), attempt_id, run_id),
            )
            if cur.rowcount == 1:
                return True
            if conn.execute(
                "SELECT 1 FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone() is None:
                raise StoreError(f"unknown attempt {attempt_id}")
            return False

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
        """Record a stop's receipt without touching the run's state.

        For a stop that does not decide the run - one that reached a run that had already
        ended. A stop that ends a live run uses :meth:`record_cancel_outcome`, because the
        receipt is what makes a repeated ``cancel`` return early and must never exist without
        the block it implies.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET cancel_receipt_json = ?, updated_at = ? WHERE run_id = ?",
                (canonical_json(receipt.model_dump(mode="json")), utc_now(), run_id),
            )

    def record_cancel_outcome(
        self, run_id: str, receipt: CancellationReceipt, code: RefusalCode, reason: str
    ) -> bool:
        """Record a stop's receipt **and** the terminal block it implies, in one statement.

        The receipt is the stop's idempotency key: once it exists, a repeated ``cancel``
        returns it. Writing it in its own transaction before the block left a window - a
        ``database is locked`` from another process, a Ctrl+C - after which the run kept a
        receipt and no block, and every later ``cancel`` returned that receipt while the run
        stayed ``RUNNING``. One ``UPDATE`` makes the two facts appear together or not at all.

        Only a live run is ended this way. A run that is already ``ACCEPTED``/``BLOCKED``/
        ``CANCELLED`` or carries a delivery receipt (another stop or writer got there first) is
        not relabelled, and ``False`` says nothing - neither the block nor this receipt - was
        written.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE runs
                   SET cancel_receipt_json = ?, task_state = ?, block_code = ?,
                       block_reason = ?, updated_at = ?
                 WHERE run_id = ? AND {_LIVE_RUN_GUARD}
                """,
                (
                    canonical_json(receipt.model_dump(mode="json")),
                    TaskState.BLOCKED.value,
                    code.value,
                    reason,
                    utc_now(),
                    run_id,
                    *_TERMINAL_STATES,
                ),
            )
            if cur.rowcount == 1:
                return True
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise RunNotFound(run_id)
            return False

    def cancel_state(self, run_id: str) -> tuple[str | None, CancellationReceipt | None]:
        row = self.get_run(run_id)
        receipt = (
            CancellationReceipt.model_validate(json.loads(row["cancel_receipt_json"]))
            if row["cancel_receipt_json"]
            else None
        )
        return (str(row["cancel_intent_at"]) if row["cancel_intent_at"] else None, receipt)

    def record_note(
        self,
        run_id: str,
        note: str,
        *,
        limit: int = 1000,
        only_if_absent_prefix: str | None = None,
    ) -> None:
        """Append an operator-facing note to the run's own audit trail.

        Notes live in their own table rather than in ``block_reason``, because a terminal
        transition clears that column and an audit fact (an authorization being consumed, a
        dirty target being left alone) must survive the run reaching a final state.

        ``limit`` bounds an operator note by default. Structured documents use separate write
        helpers that preserve them verbatim and reject conflicts.

        ``only_if_absent_prefix`` makes the note write-once for that prefix: the first recorded
        value wins and a later operator remark with the prefix is dropped.
        """
        with self.transaction() as conn:
            if only_if_absent_prefix is not None and self._note_with_prefix(
                conn, run_id, only_if_absent_prefix
            ):
                return
            self._record_note_locked(conn, run_id, note, limit=limit)

    def _note_with_prefix(
        self, conn: sqlite3.Connection, run_id: str, prefix: str
    ) -> sqlite3.Row | None:
        """Exact prefix match. ``substr`` rather than ``LIKE`` so a prefix containing ``%`` or
        ``_`` is still matched literally."""
        return conn.execute(
            "SELECT note_id FROM run_notes WHERE run_id = ? AND substr(note, 1, ?) = ? LIMIT 1",
            (run_id, len(prefix), prefix),
        ).fetchone()

    def _record_note_locked(
        self, conn: sqlite3.Connection, run_id: str, note: str, *, limit: int = 1000
    ) -> None:
        """The note insert, for callers that already hold a transaction."""
        row = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        conn.execute(
            "INSERT INTO run_notes (note_id, run_id, note, created_at) VALUES (?, ?, ?, ?)",
            (new_evidence_id(), run_id, note[:limit], utc_now()),
        )

    def notes_for(self, run_id: str) -> list[str]:
        return [row["note"] for row in self._fetchall(
            "SELECT note FROM run_notes WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
        )]

    def record_effective_config(self, run_id: str, config: EffectiveConfig) -> None:
        """Record the configuration this run executes under, once, verbatim.

        Kept in ``run_notes`` as one canonical-JSON line rather than in a new column: it needs
        the same durability as every other audit fact (a terminal transition must not erase it)
        and none of the columns in ``runs`` are read as a document. The stored text *is* the
        ``EffectiveConfig`` document - not a paraphrase with a digest bolted on - so a reader
        re-derives the same identity from the same contract. Written only when absent: a run's
        recorded configuration is part of its identity and is never rewritten.
        """
        with self.transaction() as conn:
            self._record_structured_note_locked(conn, run_id, _EFFECTIVE_CONFIG_PREFIX, config)

    def _structured_note_locked(
        self, conn: sqlite3.Connection, run_id: str, prefix: str, model: Any
    ) -> Any:
        rows = conn.execute(
            "SELECT note FROM run_notes WHERE run_id = ? AND substr(note, 1, ?) = ?",
            (run_id, len(prefix), prefix),
        ).fetchall()
        records = []
        for row in rows:
            try:
                records.append(model.model_validate_json(row["note"][len(prefix):]))
            except (ValidationError, ValueError) as exc:
                raise StoredRecordUnreadable(
                    f"run {run_id} has an unreadable {prefix.rstrip(': ')} record"
                ) from exc
        if records and any(record != records[0] for record in records[1:]):
            raise StoredRecordUnreadable(
                f"run {run_id} has conflicting {prefix.rstrip(': ')} records"
            )
        return records[0] if records else None

    def _record_structured_note_locked(
        self, conn: sqlite3.Connection, run_id: str, prefix: str, record: Any
    ) -> None:
        existing = self._structured_note_locked(conn, run_id, prefix, type(record))
        if existing is not None:
            if existing != record:
                raise StoreError(f"run {run_id}: conflicting immutable {prefix.rstrip(': ')}")
            return
        note = prefix + canonical_json(record.model_dump(mode="json"))
        self._record_note_locked(conn, run_id, note, limit=len(note))

    def admission_binding_for(self, run_id: str) -> RunAdmissionBinding | None:
        with self._lock:
            return self._structured_note_locked(
                self.conn, run_id, _ADMISSION_BINDING_PREFIX, RunAdmissionBinding
            )

    def workspace_provenance_for(self, run_id: str) -> WorkspaceProvenance | None:
        with self._lock:
            return self._structured_note_locked(
                self.conn, run_id, _WORKSPACE_PROVENANCE_PREFIX, WorkspaceProvenance
            )

    def effective_config_for(self, run_id: str) -> EffectiveConfig | None:
        """The recorded configuration, or ``None`` for a run that predates config binding."""
        with self._lock:
            return self._structured_note_locked(
                self.conn, run_id, _EFFECTIVE_CONFIG_PREFIX, EffectiveConfig
            )

    def record_dsh_context(self, run_id: str, record: DshContextRecord) -> None:
        """Record which of a frozen candidate's changed paths are on the DSH context list."""
        note = _DSH_CONTEXT_PREFIX + canonical_json(record.model_dump(mode="json"))
        # Never cut: a cut document would read back as unreadable. The path list is bounded by
        # the candidate's diff, which the receipt stores in full anyway.
        self.record_note(run_id, note, limit=len(note))

    def dsh_context_for(self, run_id: str) -> list[DshContextRecord]:
        """Every DSH context record this run kept, oldest first.

        A note that cannot be read as a ``DshContextRecord`` raises instead of being skipped,
        like :meth:`repair_records_for`: a silently dropped record would make a partial list
        look complete. Only ``status``/``report`` read these; the controller never does.
        """
        records: list[DshContextRecord] = []
        for note in self.notes_for(run_id):
            if not note.startswith(_DSH_CONTEXT_PREFIX):
                continue
            try:
                loaded = json.loads(note[len(_DSH_CONTEXT_PREFIX) :])
            except (json.JSONDecodeError, RecursionError) as exc:
                raise StoredRecordUnreadable(
                    f"run {run_id} has an unreadable dsh_context record ({exc}); it is not skipped"
                ) from exc
            try:
                records.append(DshContextRecord.model_validate(loaded))
            except ValidationError as exc:
                raise StoredRecordUnreadable(
                    f"run {run_id} has a dsh_context record that is not a valid "
                    f"DshContextRecord: {exc}"
                ) from exc
        return records

    def record_worktree(
        self, run_id: str, path: Path, *, provenance: WorkspaceProvenance | None = None
    ) -> None:
        """Record the workspace this run was given. Written before any work happens in it."""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            binding = self._structured_note_locked(
                conn, run_id, _ADMISSION_BINDING_PREFIX, RunAdmissionBinding
            )
            if binding is not None:
                expected = WorkspaceProvenance(
                    project_root=binding.project_root, git_common_dir=binding.git_common_dir,
                    worktree_path=binding.worktree_path,
                )
                if provenance != expected or str(path) != expected.worktree_path:
                    raise StoreError(f"run {run_id}: worktree provenance differs from admission binding")
            if row["worktree_path"] and row["worktree_path"] != str(path):
                raise StoreError(f"run {run_id}: recorded worktree path is immutable")
            if provenance is not None:
                if str(path) != provenance.worktree_path:
                    raise StoreError(f"run {run_id}: worktree provenance has another path")
                self._record_structured_note_locked(
                    conn, run_id, _WORKSPACE_PROVENANCE_PREFIX, provenance
                )
            conn.execute(
                "UPDATE runs SET worktree_path = ?, worktree_state = ?, updated_at = ? WHERE run_id = ?",
                (str(path), "READY", utc_now(), run_id),
            )

    def record_cleanup_intent(
        self, run_id: str, operator: str, *, expected_path: str | None = None,
        expected_provenance: WorkspaceProvenance | None = None,
        expected_root_repository: str | None = None,
    ) -> str:
        """Claim the right to remove this run's workspace, transactionally (CAS-style).

        Refuses while another cleanup is in flight, so two operators cannot both decide to
        delete the same directory. The intent is durable before any git command runs, which
        is what makes an interrupted cleanup reconcilable afterwards. Idempotent: the first
        intent timestamp wins.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["cleanup_done_at"]:
                return "already_removed"
            if row["worktree_state"] == "REMOVING" and row["cleanup_intent_at"]:
                return "in_progress"
            if row["task_state"] not in _TERMINAL_STATES:
                return "refused: run is not terminal"
            if conn.execute(
                "SELECT 1 FROM attempts WHERE run_id = ? AND state IN (?, ?) LIMIT 1",
                (run_id, AttemptState.ACTIVE.value, AttemptState.CREATED.value),
            ).fetchone() is not None:
                return "refused: an attempt is active"
            if expected_path is not None and row["worktree_path"] != expected_path:
                return "refused: worktree path changed"
            if expected_path is not None:
                actual = self._structured_note_locked(
                    conn, run_id, _WORKSPACE_PROVENANCE_PREFIX, WorkspaceProvenance
                )
                if actual != expected_provenance:
                    return "refused: worktree provenance changed"
            if expected_root_repository is not None:
                root = conn.execute(
                    "SELECT rb.repo_path FROM root_budgets rb WHERE rb.root_id = "
                    "(SELECT root_id FROM attempts WHERE run_id = ? AND root_id <> '' "
                    "ORDER BY created_at LIMIT 1)", (run_id,),
                ).fetchone()
                if root is None or root["repo_path"] != expected_root_repository:
                    return "refused: recorded repository source changed"
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

    #: Authorization fields that may never change for a reused ``authorization_id``. A binding
    #: digest alone is not enough: the user's own words, the provenance and the ceiling are
    #: immutable parts of an approval too, and rewriting one of them while keeping the id would
    #: silently turn a spent artifact into a differently-worded or larger one.
    _AUTHORIZATION_IMMUTABLE_FIELDS = (
        "user_text",
        "provided_by",
        "authorized_at",
        "max_top_level_submissions",
        "mode",
        # Where the *record* came from. Without this, a synthesized offline record and a real
        # user artifact could swap places under one id while the binding digest stayed equal.
        "origin",
    )

    def register_authorization(self, record: dict[str, Any]) -> sqlite3.Row:
        """Record a one-shot user authorization, or return the existing identical record.

        Re-using an ``authorization_id`` is only allowed when nothing that defines the approval
        changed - not only the binding digest but the verbatim user text, the provenance, the
        timestamp and the ceiling. The existing row is never overwritten, so a consumed
        authorization cannot be revived by editing its file.
        """
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
                changed = [
                    field
                    for field in self._AUTHORIZATION_IMMUTABLE_FIELDS
                    if field in existing.keys() and existing[field] != record.get(field)
                ]
                if changed:
                    raise StoreError(
                        f"authorization {record['authorization_id']} already exists with different "
                        f"immutable field(s) {changed}; an approval is not edited in place - the "
                        "recorded one stands"
                    )
                return existing
            conn.execute(
                """
                INSERT INTO authorizations (
                    authorization_id, mode, binding_digest, user_text, provided_by,
                    authorized_at, max_top_level_submissions, created_at,
                    root_id, root_budget_json, root_limits_json, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(record.get("root_id", "") or ""),
                    str(record.get("root_budget_json", "") or ""),
                    str(record.get("root_limits_json", "") or ""),
                    # Persisted, not just carried in memory: a record's origin that survives only
                    # until the process exits is not a recorded fact. ``user_artifact`` is the
                    # default for a caller that does not say - the shape every artifact had before
                    # this field existed.
                    str(record.get("origin", "user_artifact") or "user_artifact"),
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
        unless_stopped: bool = False,
        fence: OwnerFence | None = None,
    ) -> sqlite3.Row:
        """Apply an attempt result only if it still belongs to the live revision.

        ``unless_stopped=True`` is the run thread's form: the result is also refused when the
        run's stop is recorded (``cancel_intent_at`` set), decided in the same transaction as the
        write. An unconfirmed stop leaves the attempt live on purpose - work may still be running
        - so without this a late result would be applied to an attempt the stop already decided.
        The stop's own bookkeeping (``cancel`` finishing the attempt as ``CANCELLED``) uses the
        plain form.

        ``fence`` refuses the write with ``owner_lost`` when a takeover superseded the caller.
        """
        now = utc_now()
        with self.transaction() as conn:
            self._check_fence_locked(conn, run_id, fence)
            self._guard_current(conn, run_id, attempt_id)
            if unless_stopped:
                stop = conn.execute(
                    "SELECT cancel_intent_at FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if stop["cancel_intent_at"]:
                    raise StoreError(
                        f"run {run_id} has a stop recorded at {stop['cancel_intent_at']}; the "
                        f"result for attempt {attempt_id} is not applied"
                    )
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
        self,
        *,
        run_id: str,
        attempt_id: str,
        phase: CheckPhase,
        fence: OwnerFence | None = None,
    ) -> sqlite3.Row:
        now = utc_now()
        with self.transaction() as conn:
            self._check_fence_locked(conn, run_id, fence)
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

    def reopen_for_repair(
        self, run_id: str, controller_id: str, *, fence: OwnerFence | None = None
    ) -> sqlite3.Row:
        """Move a run back to its implementation phase for its single repair attempt.

        The E1 guard is right for its own rule: an implementer is only reserved before the
        candidate is checked, because a second implementation of the same revision used to be
        impossible. Batch E2 makes exactly one exception, and this is the transaction that decides
        it - the controller calls it immediately before the repair reservation, so the phase guard
        in :meth:`_guard_dispatch_locked` sees a run that has been reopened rather than one whose
        checks are still running.

        What it refuses, and why each one is not merely a formality:

        * a run another controller owns - two schedulers must not both decide to repair;
        * a run with a recorded cancellation intent - a repair never overrides a stop, and every
          later dispatch is refused by the same fact;
        * a terminal run (``ACCEPTED``/``BLOCKED``/``CANCELLED``) - a repair is a decision taken
          inside a live run, not a way to revive one a verdict already ended. That is what keeps
          "repair" from becoming a resurrection command for a historical ``BLOCKED`` run.

        What it deliberately does **not** do: it does not touch ``current_attempt_id`` (the
        previous round's reviewer attached to that row, and acceptance reads it), it does not
        decrement any counter, and it does not charge the repair. The single place a repair is
        consumed stays ``reserve_dispatch(..., is_repair=True)``, which increments the root's
        repair counter - two writers for one counter would let them disagree. ``repairs_used`` on
        the run is not touched here either; it stays the historical per-run repair counter.
        """
        with self.transaction() as conn:
            self._check_fence_locked(conn, run_id, fence)
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["claimed_by"] != controller_id:
                raise StoreError(
                    f"cannot reopen {run_id} for a repair: it is claimed by {row['claimed_by']!r}, "
                    f"not {controller_id!r}"
                )
            if row["cancel_intent_at"]:
                raise StoreError(
                    f"cannot reopen {run_id} for a repair: a cancellation intent was recorded at "
                    f"{row['cancel_intent_at']}, and a repair never overrides a stop"
                )
            state = TaskState(row["task_state"])
            if state in {TaskState.ACCEPTED, TaskState.BLOCKED, TaskState.CANCELLED}:
                raise StoreError(
                    f"cannot reopen {run_id} for a repair: the run is {state.value}, and a repair "
                    "is only ever a decision taken inside a live run - it does not revive a run an "
                    "acceptance, a block or a stop already ended"
                )
            cur = conn.execute(
                """
                UPDATE runs
                   SET task_state = ?, phase = NULL, updated_at = ?
                 WHERE run_id = ?
                   AND cancel_intent_at IS NULL
                   AND task_state NOT IN (?, ?, ?)
                """,
                (
                    TaskState.RUNNING.value,
                    utc_now(),
                    run_id,
                    TaskState.ACCEPTED.value,
                    TaskState.BLOCKED.value,
                    TaskState.CANCELLED.value,
                ),
            )
            if cur.rowcount != 1:  # pragma: no cover - the checks above hold the same transaction
                raise StoreError(
                    f"run {run_id} changed while it was being reopened for a repair; nothing was "
                    "written and no attempt may be dispatched from here"
                )
            return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

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
        exit_reason: str = "",
        stdout_digest: str = "",
        stderr_digest: str = "",
        detail: str = "",
    ) -> EvidenceRecord:
        """Store one observation. ``exit_reason`` is *why* it ended as it did, or empty.

        The default is deliberately empty rather than a plausible value: a caller that did not
        observe a reason must not appear to have one, because batch E2 classifies an automatic
        repair from this field alone. An empty reason is a fact ("no reason was observed") and
        makes the row ineligible to trigger one.
        """
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, run_id, attempt_id, kind, status, check_id,
                    candidate_fingerprint, checks_digest, command_json, exit_code,
                    exit_reason, stdout_digest, stderr_digest, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    exit_reason,
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
            exit_reason=exit_reason,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            detail=detail,
            created_at=now,
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
        """The contract view of one stored evidence row.

        ``exit_reason`` is read as stored, including the empty string a pre-v5 row carries: an
        empty reason means "no reason was observed", and that is what keeps a legacy row
        ineligible to trigger an automatic repair. It is never defaulted to something plausible.
        """
        return EvidenceRecord(
            evidence_id=str(row["evidence_id"]),
            run_id=str(row["run_id"]),
            attempt_id=str(row["attempt_id"]),
            kind=str(row["kind"]),  # type: ignore[arg-type]
            status=EvidenceStatus(str(row["status"])),
            check_id=str(row["check_id"] or ""),
            candidate_fingerprint=str(row["candidate_fingerprint"]),
            checks_digest=str(row["checks_digest"]),
            command=list(json.loads(row["command_json"] or "[]")),
            exit_code=row["exit_code"],
            exit_reason=str(row["exit_reason"] or ""),
            stdout_digest=str(row["stdout_digest"] or ""),
            stderr_digest=str(row["stderr_digest"] or ""),
            detail=str(row["detail"] or ""),
            created_at=str(row["created_at"]),
        )

    def evidence_records_for(self, run_id: str) -> list[EvidenceRecord]:
        """Every evidence row of this run as a validated contract object, oldest first."""
        return [self._evidence_from_row(row) for row in self.evidence_for(run_id)]

    def evidence_for(self, run_id: str, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self._fetchall(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
            )
        return self._fetchall(
            "SELECT * FROM evidence WHERE run_id = ? AND kind = ? ORDER BY created_at, rowid",
            (run_id, kind),
        )

    # -- repair decisions (batch E2) -----------------------------------------

    def record_repair_record(self, run_id: str, record: RepairRecord) -> None:
        """Append one repair decision to the run's own record. Never rewrites an earlier one.

        A refusal is stored exactly like an ``allowed`` decision: "we did not repair because the
        check that failed is an environment error, not a business assertion" is the answer an
        operator needs, and a record that kept only the allowed decisions would leave the run
        looking arbitrary.

        The stored text *is* the ``RepairRecord`` document, not a paraphrase: a reader re-derives
        the same decision from the same contract. ``decided_at`` is whatever the caller recorded
        and is never rewritten here, and one call appends one row - a run's decisions are an
        ordered log, not a field that the latest writer wins.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            conn.execute(
                """
                INSERT INTO run_repair_records (record_id, run_id, record_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    new_evidence_id(),
                    run_id,
                    canonical_json(record.model_dump(mode="json")),
                    utc_now(),
                ),
            )

    def repair_records_for(self, run_id: str) -> list[RepairRecord]:
        """Every repair decision this run recorded, in the order it recorded them.

        A row that cannot be read as a ``RepairRecord`` raises instead of being skipped: these are
        facts, and silently dropping one would make a partial list look complete.
        """
        records: list[RepairRecord] = []
        for row in self._fetchall(
            "SELECT record_json FROM run_repair_records WHERE run_id = ? "
            "ORDER BY created_at, rowid",
            (run_id,),
        ):
            try:
                loaded = json.loads(str(row["record_json"]))
            except (json.JSONDecodeError, RecursionError) as exc:
                raise StoredRecordUnreadable(
                    f"run {run_id} has an unreadable repair record ({exc}); it is not skipped"
                ) from exc
            try:
                records.append(RepairRecord.model_validate(loaded))
            except ValidationError as exc:
                raise StoredRecordUnreadable(
                    f"run {run_id} has a repair record that is not a valid RepairRecord: {exc}"
                ) from exc
        return records

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

    def reserved_turns_and_attempts(self, run_id: str) -> tuple[int, int]:
        """``(turns_reserved, attempt_count)`` read in **one statement**.

        Two separate reads are two snapshots: a dispatch can commit between them, and an
        observer would then see a budget that looks smaller than the attempts it covers. That
        skew is an artifact of the observation, not of the transaction, so a consistency check
        has to observe both facts at once - a single SQL statement is a single snapshot.
        """
        row = self._fetchone(
            """
            SELECT
                r.turns_reserved AS reserved,
                (SELECT COUNT(*) FROM attempts a WHERE a.run_id = r.run_id) AS attempts
              FROM runs r
             WHERE r.run_id = ?
            """,
            (run_id,),
        )
        if row is None:
            raise RunNotFound(run_id)
        return int(row["reserved"]), int(row["attempts"])

    # -- operator settlement of an unresolved ledger entry (ruling 2026-10-03) --------------

    def settle_by_operator(
        self,
        invocation_id: str,
        *,
        settled_as: str,
        attested_by: str,
        attestation: str,
    ) -> InvocationSettlement:
        """Close an ``unknown`` / ``launch_unknown`` entry by an operator's attestation.

        One ``BEGIN IMMEDIATE`` transaction: a compare-and-set of ``invocations.state`` from the
        settleable states to ``operator_settled``, the appended ``invocation_settlements`` row,
        a run note and, for ``void`` only, the return of what this dispatch charged to the root.
        Any refusal raises ``SettlementRefused`` (``InvocationNotFound`` for an unknown id) and
        writes nothing.

        * ``consumed`` (the CLI default) keeps every counter spent: the conservative error is to
          over-count a launch that may never have happened.
        * ``void`` is refused for ``unknown`` - a launch was reported, so provider spend may have
          occurred - and allowed only for ``launch_unknown``. It returns one top-level submission
          to the root, plus one repair when the dispatch was charged as the repair. The
          authorization's counter and the run's reserved turns are **not** returned: an approval
          is spent per live task (rule 10), and the run is not revived.
        * resolve-once: an entry already ``operator_settled`` is refused (the settlement table's
          UNIQUE ``invocation_id`` is the backstop).
        * an open entry (``reserved`` / ``requested`` / ``started``) or any other state is
          refused: ``hflow resume`` reconciles an interrupted run first.
        * the run must have ended (``ACCEPTED`` / ``BLOCKED`` / ``CANCELLED``): a run that has not
          may still have a controller driving it.
        * an ended run is not proof that nothing is running (a cross-process ``hflow cancel``
          whose stop was not confirmed ends the run while its owner and agent may live on). The
          owner-death proof lives above the store: ``hflow ledger settle`` first calls
          :func:`hflow.controller.operator_settle_refusal`, which refuses unless the run's owner
          is provably gone (the takeover rule of :func:`hflow.ownership.assess_owner`) and the
          entry's recorded child process reads ``gone``. This method does not probe processes
          itself, so a direct caller must apply that check first.

        The run's task state, block code and outcome are never touched and nothing is
        dispatched. The only effect is that the root no longer counts this entry as unresolved,
        so a *new* revision may reserve - within the ceilings that remain, with its own approval.
        """
        if settled_as not in {"consumed", "void"}:
            raise SettlementRefused(f"--as must be 'consumed' or 'void', not {settled_as!r}")
        if not isinstance(attestation, str) or not attestation.strip():
            raise SettlementRefused(
                "the attestation is blank; an operator settlement records what the operator "
                "claims and why, and an empty claim records nothing"
            )
        if len(attestation) > ATTESTATION_MAX_CHARS:
            raise SettlementRefused(
                f"the attestation is {len(attestation)} characters; the bound is "
                f"{ATTESTATION_MAX_CHARS}. Shorten it (it is a statement, not a log)"
            )
        if "\x00" in attestation:
            raise SettlementRefused("the attestation contains a NUL character")
        who = (attested_by or "").strip() or "unknown"
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise InvocationNotFound(f"no ledger entry has invocation id {invocation_id}")
            prior = str(row["state"])
            if prior == InvocationStartState.OPERATOR_SETTLED.value:
                done = conn.execute(
                    "SELECT settled_as, attested_by, attested_at FROM invocation_settlements"
                    " WHERE invocation_id = ?",
                    (invocation_id,),
                ).fetchone()
                already = (
                    f" as {done['settled_as']} by {done['attested_by']} at {done['attested_at']}"
                    if done is not None
                    else ""
                )
                raise SettlementRefused(
                    f"invocation {invocation_id} is already operator_settled{already}; an entry "
                    "is settled once and the recorded settlement stands"
                )
            if prior in INVOCATION_OPEN_STATES:
                raise SettlementRefused(
                    f"invocation {invocation_id} is {prior}: still open, so a controller may be "
                    "driving it. Run `hflow resume <run_id>` to reconcile the run first; only an "
                    "unknown or launch_unknown entry can be settled by an operator"
                )
            if prior not in INVOCATION_OPERATOR_SETTLEABLE_STATES:
                raise SettlementRefused(
                    f"invocation {invocation_id} is {prior}, which already records its final "
                    "fact; only an unknown or launch_unknown entry can be settled by an operator "
                    "(`hflow resume` reconciles a run that is still unresolved)"
                )
            if settled_as == "void" and prior != InvocationStartState.LAUNCH_UNKNOWN.value:
                raise SettlementRefused(
                    f"invocation {invocation_id} is {prior}: a launch was reported, so provider "
                    "spend may have occurred and its charge is not returned. Only a "
                    "launch_unknown entry can be voided; settle this one as consumed"
                )
            run = conn.execute(
                "SELECT task_state FROM runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            if run is None or str(run["task_state"]) not in _TERMINAL_STATES:
                state = str(run["task_state"]) if run is not None else "missing"
                raise SettlementRefused(
                    f"run {row['run_id']} is {state}, not ended; a controller may still own it. "
                    "Use `hflow resume` or `hflow cancel` first - an operator settles only an "
                    "entry of a run that has ended"
                )
            root_id = str(row["root_id"] or "")
            returned_top_level = 0
            returned_repairs = 0
            now = utc_now()
            if settled_as == "void":
                if not root_id:
                    raise SettlementRefused(
                        f"invocation {invocation_id} records no root, so there is no root "
                        "counter to return its charge to; settle it as consumed"
                    )
                returned_top_level = 1
                returned_repairs = 1 if int(row["is_repair"]) else 0
                cur = conn.execute(
                    """
                    UPDATE root_budgets
                       SET used_top_level_submissions = used_top_level_submissions - 1,
                           used_repairs = used_repairs - ?,
                           updated_at = ?
                     WHERE root_id = ?
                       AND used_top_level_submissions >= 1
                       AND used_repairs >= ?
                    """,
                    (returned_repairs, now, root_id, returned_repairs),
                )
                if cur.rowcount != 1:
                    raise SettlementRefused(
                        f"root {root_id} cannot take back this entry's charge (its row is missing "
                        "or its counters are already below what this dispatch charged); nothing "
                        "was written"
                    )
            cas = conn.execute(
                "UPDATE invocations SET state = ? WHERE invocation_id = ? AND state = ?",
                (InvocationStartState.OPERATOR_SETTLED.value, invocation_id, prior),
            )
            if cas.rowcount != 1:  # pragma: no cover - the transaction holds the write lock
                raise SettlementRefused(
                    f"invocation {invocation_id} changed state during the settlement; retry"
                )
            settlement = InvocationSettlement(
                settlement_id=new_id("S"),
                invocation_id=invocation_id,
                run_id=str(row["run_id"]),
                root_id=root_id,
                prior_state=prior,  # type: ignore[arg-type]
                settled_as=settled_as,  # type: ignore[arg-type]
                attested_by=who,
                attested_at=now,
                attestation=attestation,
                returned_top_level_submissions=returned_top_level,
                returned_repairs=returned_repairs,
            )
            conn.execute(
                """
                INSERT INTO invocation_settlements (
                    settlement_id, invocation_id, run_id, root_id, prior_state, settled_as,
                    basis, attested_by, attested_at, attestation,
                    returned_top_level_submissions, returned_repairs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settlement.settlement_id,
                    settlement.invocation_id,
                    settlement.run_id,
                    settlement.root_id,
                    settlement.prior_state,
                    settlement.settled_as,
                    settlement.basis,
                    settlement.attested_by,
                    settlement.attested_at,
                    settlement.attestation,
                    settlement.returned_top_level_submissions,
                    settlement.returned_repairs,
                ),
            )
            self._record_note_locked(
                conn,
                settlement.run_id,
                f"ledger: invocation {invocation_id} ({prior}) settled as {settled_as} by "
                f"operator attestation (not observed); attested_by {who} at {now}. The run's "
                "state and outcome are unchanged and nothing is re-dispatched",
            )
            return settlement

    @staticmethod
    def _settlement_from_row(row: sqlite3.Row) -> InvocationSettlement:
        return InvocationSettlement(
            settlement_id=str(row["settlement_id"]),
            invocation_id=str(row["invocation_id"]),
            run_id=str(row["run_id"]),
            root_id=str(row["root_id"] or ""),
            prior_state=str(row["prior_state"]),  # type: ignore[arg-type]
            settled_as=str(row["settled_as"]),  # type: ignore[arg-type]
            basis=str(row["basis"]),  # type: ignore[arg-type]
            attested_by=str(row["attested_by"]),
            attested_at=str(row["attested_at"]),
            attestation=str(row["attestation"]),
            returned_top_level_submissions=int(row["returned_top_level_submissions"]),
            returned_repairs=int(row["returned_repairs"]),
        )

    def settlement_for(self, invocation_id: str) -> InvocationSettlement | None:
        row = self._fetchone(
            "SELECT * FROM invocation_settlements WHERE invocation_id = ?", (invocation_id,)
        )
        return self._settlement_from_row(row) if row is not None else None

    def settlements_for(self, run_id: str) -> list[InvocationSettlement]:
        """This run's operator settlements, oldest first. Attestations, not observations."""
        return [
            self._settlement_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM invocation_settlements WHERE run_id = ?"
                " ORDER BY attested_at, rowid",
                (run_id,),
            )
        ]

    # -- controlled integration (batch I2, storage v8) ----------------------------------------

    @staticmethod
    def _integration_from_row(row: sqlite3.Row) -> IntegrationRecord:
        """The contract view of one ``integrations`` row. An unreadable row raises, never skips."""
        try:
            data = {
                name: (
                    json.loads(str(row[_INTEGRATION_LIST_COLUMNS[name]]))
                    if name in _INTEGRATION_LIST_COLUMNS
                    else row[name]
                )
                for name in IntegrationRecord.model_fields
            }
            return IntegrationRecord.model_validate(data)
        except (ValidationError, ValueError, RecursionError) as exc:
            raise StoredRecordUnreadable(
                f"integration {row['integration_id']} is not a readable IntegrationRecord: {exc}"
            ) from exc

    def _integration_row_locked(
        self, conn: sqlite3.Connection, integration_id: str
    ) -> IntegrationRecord:
        row = conn.execute(
            "SELECT * FROM integrations WHERE integration_id = ?", (integration_id,)
        ).fetchone()
        if row is None:
            raise IntegrationNotFound(f"no integration record has id {integration_id}")
        return self._integration_from_row(row)

    @staticmethod
    def _integrity_conflict_locked(
        conn: sqlite3.Connection, record: IntegrationRecord, exc: sqlite3.IntegrityError
    ) -> IntegrationConflict:
        """Name the integration a uniqueness refusal collided with, when there is one."""
        active = tuple(state.value for state in INTEGRATION_ACTIVE_STATES)
        if record.state.value in active:
            other = conn.execute(
                f"SELECT integration_id, state FROM integrations WHERE run_id = ? "
                f"AND state IN ({', '.join('?' for _ in active)}) AND integration_id <> ? "
                "ORDER BY created_at, rowid LIMIT 1",
                (record.run_id, *active, record.integration_id),
            ).fetchone()
            if other is not None:
                return IntegrationConflict(
                    f"run {record.run_id} already has an active integration "
                    f"{other['integration_id']} ({other['state']}); a run has at most one "
                    "preparing, checking or applying integration"
                )
        if record.state is IntegrationState.APPLYING:
            other = conn.execute(
                "SELECT integration_id, run_id FROM integrations WHERE git_common_dir = ? "
                "AND target_ref = ? AND state = ? AND integration_id <> ? LIMIT 1",
                (
                    record.git_common_dir,
                    record.target_ref,
                    IntegrationState.APPLYING.value,
                    record.integration_id,
                ),
            ).fetchone()
            if other is not None:
                return IntegrationConflict(
                    f"integration {other['integration_id']} (run {other['run_id']}) is already "
                    f"applying to {record.target_ref} in {record.git_common_dir}; a repository "
                    "target has at most one applying integration"
                )
        return IntegrationConflict(f"integration {record.integration_id} was not written: {exc}")

    def create_integration(self, record: IntegrationRecord) -> IntegrationRecord:
        """Record a new ``preparing`` integration of an accepted run's candidate.

        One ``BEGIN IMMEDIATE`` transaction. The run must exist, be ``ACCEPTED`` with a
        ``LOCAL_CANDIDATE`` delivery receipt, and the record must name that receipt's attempt,
        candidate commit and base and the run's task and checks digest. A ``ready`` integration of
        the same run is marked ``superseded`` (a new prepare replaces it); a second *active* one is
        refused by the schema's partial unique index and reported naming it. The store stamps
        ``created_at``/``updated_at``; the stored record is returned.
        """
        record = IntegrationRecord.model_validate(record.model_dump())
        if record.state is not IntegrationState.PREPARING:
            raise ValueError(
                f"a new integration is recorded as preparing, not {record.state.value}"
            )
        now = utc_now()
        record = record.model_copy(update={"created_at": now, "updated_at": now})
        with self.transaction() as conn:
            run = conn.execute(
                "SELECT task_id, task_state, delivery_state, checks_digest, receipt_json "
                "FROM runs WHERE run_id = ?",
                (record.run_id,),
            ).fetchone()
            if run is None:
                raise IntegrationConflict(f"run {record.run_id} does not exist; nothing to integrate")
            if run["task_state"] != TaskState.ACCEPTED.value:
                raise IntegrationConflict(
                    f"run {record.run_id} is {run['task_state']}, not ACCEPTED; only an accepted "
                    "candidate is integrated"
                )
            if not run["receipt_json"]:
                raise IntegrationConflict(
                    f"run {record.run_id} is ACCEPTED but records no delivery receipt; there is "
                    "no frozen candidate to integrate"
                )
            if run["delivery_state"] != DeliveryState.LOCAL_CANDIDATE.value:
                raise IntegrationConflict(
                    f"run {record.run_id} has delivery state {run['delivery_state']}, not "
                    f"{DeliveryState.LOCAL_CANDIDATE.value}"
                )
            try:
                receipt = ResultReceipt.model_validate_json(str(run["receipt_json"]))
            except (ValidationError, ValueError) as exc:
                raise StoredRecordUnreadable(
                    f"run {record.run_id} has an unreadable delivery receipt: {exc}"
                ) from exc
            mismatches = [
                f"{name} {given!r} != {recorded!r}"
                for name, given, recorded in (
                    ("task_id", record.task_id, str(run["task_id"])),
                    ("attempt_id", record.attempt_id, receipt.attempt_id),
                    ("checks_digest", record.checks_digest, str(run["checks_digest"])),
                    ("candidate_commit", record.candidate_commit, receipt.candidate.git_commit),
                    ("base_commit", record.base_commit, receipt.candidate.base_commit),
                )
                if given != recorded
            ]
            if mismatches:
                raise ValueError(
                    f"integration {record.integration_id} does not describe run "
                    f"{record.run_id}'s accepted candidate: {'; '.join(mismatches)}"
                )
            if not record.candidate_commit:
                raise IntegrationConflict(
                    f"run {record.run_id}'s receipt records no Git candidate commit; only a "
                    "candidate frozen in a Git worktree can be integrated"
                )
            superseded = [
                str(row["integration_id"])
                for row in conn.execute(
                    "SELECT integration_id FROM integrations WHERE run_id = ? AND state = ? "
                    "ORDER BY created_at, rowid",
                    (record.run_id, IntegrationState.READY.value),
                )
            ]
            if superseded:
                conn.execute(
                    "UPDATE integrations SET state = ?, detail = ?, updated_at = ? "
                    "WHERE run_id = ? AND state = ?",
                    (
                        IntegrationState.SUPERSEDED.value,
                        f"superseded by {record.integration_id}: a new prepare of the same run",
                        now,
                        record.run_id,
                        IntegrationState.READY.value,
                    ),
                )
            values = _integration_values(record)
            columns = list(values)
            try:
                conn.execute(
                    f"INSERT INTO integrations ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [values[column] for column in columns],
                )
            except sqlite3.IntegrityError as exc:
                raise self._integrity_conflict_locked(conn, record, exc) from exc
            note = (
                f"{NOTE_INTEGRATION}: {record.integration_id} prepared against "
                f"{record.target_ref} at {record.target_tip}"
            )
            if superseded:
                note += f"; superseded ready integration(s) {', '.join(superseded)}"
            self._record_note_locked(conn, record.run_id, note)
            return self._integration_row_locked(conn, record.integration_id)

    def integration(self, integration_id: str) -> IntegrationRecord | None:
        row = self._fetchone(
            "SELECT * FROM integrations WHERE integration_id = ?", (integration_id,)
        )
        return self._integration_from_row(row) if row is not None else None

    def integrations_for(self, run_id: str) -> list[IntegrationRecord]:
        """This run's integration records, oldest first."""
        return [
            self._integration_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM integrations WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            )
        ]

    def update_integration(
        self,
        integration_id: str,
        *,
        expected: IntegrationState | Sequence[IntegrationState],
        state: IntegrationState | None = None,
        **fields: Any,
    ) -> IntegrationRecord:
        """Compare-and-set one integration record from one of ``expected``.

        ``fields`` is limited to ``_INTEGRATION_MUTABLE_FIELDS`` (an unknown key is a
        ``ValueError``), and the result is validated as an ``IntegrationRecord`` before it is
        written. ``state`` can never become ``integrated`` here - :meth:`finalize_integration`
        writes that together with its receipt - and an ``integrated`` record never changes state.

        Raises ``IntegrationNotFound`` for an unknown id, and ``IntegrationConflict`` (nothing
        written) when the record is not in an expected state or a uniqueness rule refuses the
        write (a second ``applying`` integration of the same repository target, a second active
        integration of the same run); the message names the current state or the other record.
        """
        unknown = sorted(set(fields) - _INTEGRATION_MUTABLE_FIELDS)
        if unknown:
            raise ValueError(
                f"update_integration cannot set {', '.join(unknown)}; it may set state and "
                f"{', '.join(sorted(_INTEGRATION_MUTABLE_FIELDS))}"
            )
        target = IntegrationState(state) if state is not None else None
        if target is IntegrationState.INTEGRATED:
            raise ValueError(
                "update_integration never sets integrated; finalize_integration records it "
                "together with the integration receipt"
            )
        allowed = _integration_states(expected)
        with self.transaction() as conn:
            current = self._integration_row_locked(conn, integration_id)
            if current.state not in allowed:
                raise IntegrationConflict(
                    f"integration {integration_id} is {current.state.value}, expected "
                    f"{' or '.join(item.value for item in allowed)}; nothing was written"
                )
            if (
                current.state is IntegrationState.INTEGRATED
                and target is not None
                and target is not IntegrationState.INTEGRATED
            ):
                raise IntegrationConflict(
                    f"integration {integration_id} is integrated; an integrated record keeps its "
                    "state and receipt"
                )
            updates: dict[str, Any] = dict(fields)
            if target is not None:
                updates["state"] = target
            updates["updated_at"] = utc_now()
            merged = IntegrationRecord.model_validate({**current.model_dump(), **updates})
            values = _integration_values(merged)
            columns = [_INTEGRATION_LIST_COLUMNS.get(name, name) for name in updates]
            try:
                cur = conn.execute(
                    f"UPDATE integrations SET {', '.join(f'{column} = ?' for column in columns)} "
                    f"WHERE integration_id = ? AND state IN ({', '.join('?' for _ in allowed)})",
                    [
                        *(values[column] for column in columns),
                        integration_id,
                        *(item.value for item in allowed),
                    ],
                )
            except sqlite3.IntegrityError as exc:
                raise self._integrity_conflict_locked(conn, merged, exc) from exc
            if cur.rowcount != 1:  # pragma: no cover - the transaction holds the write lock
                raise IntegrationConflict(
                    f"integration {integration_id} changed during the update; nothing was written"
                )
            return self._integration_row_locked(conn, integration_id)

    def finalize_integration(
        self,
        integration_id: str,
        *,
        expected: IntegrationState | Sequence[IntegrationState],
        receipt: IntegrationReceipt,
    ) -> IntegrationRecord:
        """Record that the target contains the integration commit: ``integrated`` + its receipt.

        One transaction: a compare-and-set from ``expected`` to ``integrated`` that writes
        ``integrated_at``, ``basis``, ``applied_by`` (when the receipt names one) and the receipt
        as canonical JSON, plus a run note. The receipt must describe this record - its run, task,
        attempt, candidate, target, the tip it was prepared against and, once recorded, its
        integration commit, tree and mode - or nothing is written. The run's own row (its receipt,
        task state and delivery state) is never touched: the run stays ``LOCAL_CANDIDATE``.
        """
        if receipt.integration_id != integration_id:
            raise ValueError(
                f"the receipt is for integration {receipt.integration_id}, not {integration_id}"
            )
        allowed = _integration_states(expected)
        if IntegrationState.INTEGRATED in allowed:
            raise ValueError(
                "finalize_integration moves a record into integrated once; an integrated record "
                "keeps the receipt it has"
            )
        with self.transaction() as conn:
            current = self._integration_row_locked(conn, integration_id)
            if current.state not in allowed:
                raise IntegrationConflict(
                    f"integration {integration_id} is {current.state.value}, expected "
                    f"{' or '.join(item.value for item in allowed)}; nothing was written"
                )
            checks: list[tuple[str, Any, Any]] = [
                ("run_id", receipt.run_id, current.run_id),
                ("task_id", receipt.task_id, current.task_id),
                ("attempt_id", receipt.attempt_id, current.attempt_id),
                ("candidate.git_commit", receipt.candidate.git_commit, current.candidate_commit),
                ("candidate.base_commit", receipt.candidate.base_commit, current.base_commit),
                ("target_ref", receipt.target_ref, current.target_ref),
                ("target_tip_before", receipt.target_tip_before, current.target_tip),
            ]
            if current.integration_commit:
                checks.append(
                    ("integration_commit", receipt.integration_commit, current.integration_commit)
                )
            if current.integration_tree:
                checks.append(
                    ("integration_tree", receipt.integration_tree, current.integration_tree)
                )
            if current.mode is not None:
                checks.append(("mode", receipt.mode, current.mode))
            mismatches = [
                f"{name} {given!r} != {recorded!r}"
                for name, given, recorded in checks
                if given != recorded
            ]
            if mismatches:
                raise ValueError(
                    f"the receipt does not describe integration {integration_id}: "
                    f"{'; '.join(mismatches)}"
                )
            cur = conn.execute(
                f"""
                UPDATE integrations
                   SET state = ?, mode = ?, integration_commit = ?, integration_tree = ?,
                       integrated_at = ?, basis = ?, applied_by = ?, receipt_json = ?,
                       updated_at = ?
                 WHERE integration_id = ? AND state IN ({', '.join('?' for _ in allowed)})
                """,
                (
                    IntegrationState.INTEGRATED.value,
                    receipt.mode,
                    receipt.integration_commit,
                    receipt.integration_tree,
                    receipt.integrated_at,
                    receipt.basis,
                    receipt.applied_by or current.applied_by,
                    canonical_json(receipt.model_dump(mode="json")),
                    utc_now(),
                    integration_id,
                    *(item.value for item in allowed),
                ),
            )
            if cur.rowcount != 1:  # pragma: no cover - the transaction holds the write lock
                raise IntegrationConflict(
                    f"integration {integration_id} changed during finalization; nothing was written"
                )
            self._record_note_locked(
                conn,
                current.run_id,
                f"{NOTE_INTEGRATION}: {integration_id} integrated into {receipt.target_ref} at "
                f"{receipt.integration_commit} ({receipt.basis})",
            )
            return self._integration_row_locked(conn, integration_id)

    def integration_receipt(self, integration_id: str) -> IntegrationReceipt | None:
        """The integration's receipt; ``None`` when the id is unknown or it is not integrated."""
        row = self._fetchone(
            "SELECT receipt_json FROM integrations WHERE integration_id = ?", (integration_id,)
        )
        if row is None or row["receipt_json"] is None:
            return None
        try:
            return IntegrationReceipt.model_validate_json(str(row["receipt_json"]))
        except (ValidationError, ValueError) as exc:
            raise StoredRecordUnreadable(
                f"integration {integration_id} has an unreadable receipt: {exc}"
            ) from exc
