"""Versioned SQLite migrations for the HFlow ledger.

Why this exists as its own module: batch E1 adds tables and columns to a database that
already holds the user's previous runs, authorizations and evidence. "Recreate the schema"
would silently drop exactly the records the design treats as facts, so the change is a
numbered migration instead - and a numbered migration is something a test can interrupt,
re-run and compare, which an idempotent ``executescript`` bootstrap is not.

The rules enforced here:

* Every migration runs inside one ``BEGIN IMMEDIATE`` transaction made of plain
  ``execute`` statements. ``executescript`` is never used for a migration because it
  commits the surrounding transaction implicitly, which would make a half-applied
  migration survivable.
* The file is copied first, with the SQLite backup API (a consistent snapshot, not a
  file copy of a database that may have a live WAL), and only when a migration is
  actually needed. The copy is never overwritten: the earliest pre-migration state is
  the only one that can be restored to.
* A database whose recorded version is newer than this build is refused *before* any
  write. An older build cannot know what a newer layout means, so opening it read-write
  would corrupt facts it does not understand.
* Re-opening an already-migrated database does nothing: the version check short-circuits
  and no backup is written twice.

What this is not: a schema-evolution platform. There is no downgrade path (restore the
backup instead), no partial-version support and no migration of data *between* shapes -
E1 only ever adds.

The E1 chain so far: v1 is the original bootstrap, v2 adds the root ledger and the invocation
record, v3 splits "a launch was requested" from "a launch happened" (and records where an
authorization record came from), v4 adds the process facts a driver actually reports. A v2 row
past ``reserved`` keeps its state and outcome and becomes a launch *request*; a v3 row keeps
``spawn_kind = unknown`` because v3 recorded no driver report to inherit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

#: Storage format version. Separate from the public contract version: the file layout can
#: gain a table while every published contract keeps its own meaning.
STORAGE_VERSION = 4
#: The highest storage version this build knows how to open.
SUPPORTED_STORAGE_VERSION = 4
#: Suffix of the pre-migration snapshot, next to the database.
MIGRATION_BACKUP_SUFFIX = ".pre-v{version}.bak"

#: Called after each migration step, so a test can raise at a chosen point and observe that
#: the whole migration rolled back. A no-op in production.
StepHook = Callable[[str], None]


class MigrationError(RuntimeError):
    """A migration could not be applied. The database is left exactly as it was."""


# --------------------------------------------------------------------------
# v1 was the original bootstrap: it stays here, byte for byte, because a v0 database
# (one that predates versioning) has to become v1 through the same path as a new file.
# --------------------------------------------------------------------------

_V1_SCHEMA = """
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

# --------------------------------------------------------------------------
# v2 (batch E1): the root ledger and the per-invocation dispatch record
# --------------------------------------------------------------------------

_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS root_budgets (
    root_id                   TEXT PRIMARY KEY,
    project_id                TEXT NOT NULL,
    task_id                   TEXT NOT NULL,
    repo_path                 TEXT NOT NULL,
    ledger_path               TEXT NOT NULL,
    max_top_level_submissions INTEGER NOT NULL,
    max_repairs               INTEGER NOT NULL,
    deadline_seconds          INTEGER NOT NULL,
    limits_digest             TEXT NOT NULL,
    binding_digest            TEXT NOT NULL,
    used_top_level_submissions INTEGER NOT NULL DEFAULT 0,
    used_repairs              INTEGER NOT NULL DEFAULT 0,
    run_ids_json              TEXT NOT NULL DEFAULT '[]',
    authorization_ids_json    TEXT NOT NULL DEFAULT '[]',
    first_dispatch_at         TEXT,
    deadline_at               TEXT,
    notes                     TEXT NOT NULL DEFAULT '',
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    CHECK (used_top_level_submissions >= 0),
    CHECK (used_top_level_submissions <= max_top_level_submissions),
    CHECK (used_repairs >= 0),
    CHECK (used_repairs <= max_repairs)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_root_budgets_binding
    ON root_budgets (project_id, repo_path, task_id);

CREATE TABLE IF NOT EXISTS invocations (
    invocation_id        TEXT PRIMARY KEY,
    root_id              TEXT NOT NULL DEFAULT '',
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    attempt_id           TEXT NOT NULL REFERENCES attempts(attempt_id),
    role                 TEXT NOT NULL,
    authorization_id     TEXT NOT NULL DEFAULT '',
    round                INTEGER NOT NULL DEFAULT 0,
    is_repair            INTEGER NOT NULL DEFAULT 0,
    state                TEXT NOT NULL,
    reserved_at          TEXT NOT NULL,
    started_at           TEXT,
    settled_at           TEXT,
    outcome              TEXT,
    root_used_at_reservation INTEGER NOT NULL DEFAULT 0,
    authorization_used_at_reservation INTEGER NOT NULL DEFAULT 0,
    detail               TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_invocations_run ON invocations (run_id, reserved_at);
CREATE INDEX IF NOT EXISTS ix_invocations_root ON invocations (root_id, state);
CREATE INDEX IF NOT EXISTS ix_invocations_attempt ON invocations (attempt_id);
"""

_V2_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("authorizations", "root_id", "TEXT NOT NULL DEFAULT ''"),
    ("authorizations", "root_budget_json", "TEXT NOT NULL DEFAULT ''"),
    ("authorizations", "root_limits_json", "TEXT NOT NULL DEFAULT ''"),
    ("attempts", "root_id", "TEXT NOT NULL DEFAULT ''"),
)

# --------------------------------------------------------------------------
# v3 (E1 review finding 4): a requested launch and a created process are different facts
# --------------------------------------------------------------------------

_V3_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("invocations", "launch_requested_at", "TEXT"),
    ("authorizations", "origin", "TEXT NOT NULL DEFAULT 'user_artifact'"),
)

# --------------------------------------------------------------------------
# v4 (E1 review, round two): process facts recorded separately from launch state
# --------------------------------------------------------------------------

_V4_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("invocations", "process_started_at", "TEXT"),
    ("invocations", "process_pid", "INTEGER"),
    ("invocations", "spawn_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
)


def _statements(script: str) -> list[str]:
    """Split a DDL script into statements without ``executescript``.

    ``sqlite3.executescript`` issues an implicit COMMIT first, so it cannot be used inside a
    migration transaction: a failure halfway through would leave the earlier statements
    committed. Executing one statement at a time keeps the whole migration atomic.
    """
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def recorded_version(conn: sqlite3.Connection) -> int | None:
    """The storage version recorded in the file, or ``None`` if it predates versioning.

    A database with no ``schema_meta`` row is either brand new (no tables at all, so it is
    treated as 0 and created) or a v1 file whose bootstrap never wrote the version. The
    ``runs`` table is what tells those apart.
    """
    if not _has_table(conn, "schema_meta"):
        return None
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'storage_version'").fetchone()
    if row is not None:
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise MigrationError(
                f"the recorded storage_version is not a number: {row[0]!r}"
            ) from exc
    return None


def _effective_version(conn: sqlite3.Connection) -> int:
    """The version to *read* this file as.

    A recorded version is authoritative. Without one the shape decides, which matters for a file
    whose bootstrap never stamped a version: it could be v1 *or* v2, and treating a v2 file as v1
    would try to create tables that already exist (harmless) and then try to re-add columns that
    already exist (not harmless - ``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``).
    """
    version = recorded_version(conn)
    if version is not None:
        return version
    if not _has_table(conn, "runs"):
        return 0
    if _columns(conn, "invocations") >= {"process_started_at", "spawn_kind"}:
        return 4
    if _columns(conn, "invocations") >= {"launch_requested_at", "started_at"}:
        return 3
    if _has_table(conn, "invocations") and _has_table(conn, "root_budgets"):
        return 2
    return 1


def backup_path_for(database: Path, from_version: int) -> Path:
    """Where the pre-migration snapshot of ``database`` lives.

    Versioned by the *file's* version, so a later migration writes its own snapshot instead of
    overwriting the copy that restores the earliest state.
    """
    return database.with_name(database.name + MIGRATION_BACKUP_SUFFIX.format(version=from_version))


def _backup(conn: sqlite3.Connection, database: Path, from_version: int) -> Path | None:
    """Copy the database with the SQLite backup API, once.

    Returns ``None`` for an in-memory database, which has no file to restore.
    """
    if str(database) == ":memory:":
        return None
    target = backup_path_for(database, from_version)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return target


def _migrate_to_v1(conn: sqlite3.Connection, step: StepHook) -> None:
    for statement in _statements(_V1_SCHEMA):
        conn.execute(statement)
    step("v1:base-schema")


def _migrate_to_v2(conn: sqlite3.Connection, step: StepHook) -> None:
    for statement in _statements(_V2_SCHEMA):
        conn.execute(statement)
    step("v2:root-and-invocation-tables")
    for table, column, definition in _V2_COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        step(f"v2:alter:{table}.{column}")


def _migrate_to_v3(conn: sqlite3.Connection, step: StepHook) -> None:
    """Split "a launch was requested" from "a launch happened" (E1 review finding 4).

    v2 recorded one ``started_at`` that the controller wrote *before* asking the driver, so a
    suppressed launch looked started. v3 adds ``launch_requested_at`` and makes ``started_at``
    mean what its name says. Every v2 row that got past ``reserved`` is backfilled, not only the
    ``started`` ones: v2 wrote that timestamp before asking the driver in *every* state that
    followed, so a ``settled`` or ``unknown`` row carries a request time too, and leaving it in
    ``started_at`` would promote it to a process fact. The row keeps its state and its outcome -
    v3 does not decide what happened, it only stops claiming a time it never observed.
    """
    for table, column, definition in _V3_COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        step(f"v3:alter:{table}.{column}")
    conn.execute(
        """
        UPDATE invocations
           SET launch_requested_at = COALESCE(launch_requested_at, started_at, reserved_at),
               started_at = NULL,
               detail = CASE WHEN detail = '' THEN ? ELSE detail END
         WHERE state <> 'reserved'
        """,
        (
            "migrated from storage v2: the recorded timestamp was written before the driver was "
            "asked, so it is a launch *request*, not an observed launch. The row keeps its state "
            "and outcome; started_at is left unset because v2 never recorded a driver report.",
        ),
    )
    step("v3:backfill-launch-requested")


def _migrate_to_v4(conn: sqlite3.Connection, step: StepHook) -> None:
    """Record *process* facts separately from launch state (E1 review, round two).

    v3 conflated three things in ``started_at`` and derived a process count from result states, so
    an offline run with no child process reported "2 started" and an unknown result with no spawn
    report reported "1". v4 adds the process facts the driver actually reports
    (``process_started_at``, ``process_pid``, ``spawn_kind``) and leaves them NULL/``unknown`` for
    every earlier row: v3 kept no driver process report, so inventing one here would be exactly
    the fabrication this migration exists to stop.

    A v3 row that reached ``started`` or beyond did have a launch happen - that much v3 recorded -
    but whether it created a child is unknown, which is what ``spawn_kind = 'unknown'`` says.

    It also repairs rows the *first* v3 migration left behind. That version backfilled only
    ``state = 'started'``, so a v2 row that was already ``settled`` or ``unknown`` kept its
    pre-launch request timestamp in ``started_at`` with no ``launch_requested_at`` - and because
    v3 stamped its version, the corrected v3 step never runs for that file again. Fixing the old
    migration step alone would therefore leave every already-upgraded database wrong, so the
    repair lives here, where it reaches both kinds of file.

    It is deliberately narrow: only a row with ``started_at`` set and ``launch_requested_at``
    NULL is touched, which is exactly the old v3 shape. In v3 and later, ``started_at`` is only
    ever written together with ``launch_requested_at``, so a correctly migrated row is never
    rewritten and a genuinely recorded launch keeps its timestamp.
    """
    for table, column, definition in _V4_COLUMNS:
        if column in _columns(conn, table):
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        step(f"v4:alter:{table}.{column}")
    conn.execute(
        """
        UPDATE invocations
           SET spawn_kind = 'unknown'
         WHERE spawn_kind IS NULL OR spawn_kind = ''
        """
    )
    step("v4:backfill-spawn-kind")
    repaired = conn.execute(
        """
        UPDATE invocations
           SET launch_requested_at = started_at,
               started_at = NULL,
               detail = CASE WHEN detail = '' THEN ? ELSE detail END
         WHERE started_at IS NOT NULL AND launch_requested_at IS NULL
        """,
        (
            "repaired during storage v4: this timestamp was written before the driver was asked, "
            "so it is a launch *request*. The state and outcome are unchanged, and no launch or "
            "process is claimed, because the build that wrote it recorded neither.",
        ),
    )
    step(f"v4:repair-legacy-request-timestamp:{repaired.rowcount}")


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection, StepHook], None]] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
}


def migrate(
    conn: sqlite3.Connection,
    database: Path,
    *,
    on_step: StepHook | None = None,
    supported: int = SUPPORTED_STORAGE_VERSION,
) -> tuple[int, int, Path | None]:
    """Bring ``conn`` up to ``supported``.

    Returns ``(version_before, version_now, backup_path)``. ``version_before`` is the *effective*
    version the file was read as - including the inferred 1 of a pre-E1 file that never recorded
    one - so a caller can say truthfully what a file was migrated from instead of reporting
    "unknown" for the shape that actually exists in the wild.

    The version check happens before the backup and before any write, so an unknown future file
    is refused with nothing touched - including no snapshot, because there is nothing to protect
    it from yet.

    ``conn`` is expected to be in autocommit mode (``isolation_level=None``); the transaction
    is opened here so that every migration statement, including DDL, is one unit.
    """
    step: StepHook = on_step or (lambda _name: None)
    version = _effective_version(conn)
    if version > supported:
        raise MigrationError(
            f"{database} records storage version {version}, but this build understands at most "
            f"{supported}. Refusing to open it: a build that does not know this layout cannot "
            "know which of its rows it would be misreading. Use a build that supports version "
            f"{version}, or restore a pre-migration backup."
        )
    if version >= supported:
        return version, version, None

    # A file that has never been initialised has no previous state to protect, so the snapshot
    # is skipped: it would be an empty database that restores nothing.
    backup = _backup(conn, Path(database), version) if version > 0 else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = version
        while current < supported:
            migration = _MIGRATIONS.get(current + 1)
            if migration is None:
                raise MigrationError(
                    f"no migration is defined from storage version {current} to {current + 1}"
                )
            migration(conn, step)
            current += 1
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('storage_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(supported),),
        )
        step(f"storage_version={supported}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return version, supported, backup


def effective_version(conn: sqlite3.Connection) -> int:
    """The version a file is *read as*, inferring v1 for a pre-versioning database.

    Public because two callers need the same answer: the migration itself, and the store, which
    reports what a file was migrated from.
    """
    return _effective_version(conn)


def migration_steps() -> Iterable[str]:
    """The version pairs this build can migrate between. Used by tests and diagnostics."""
    return tuple(
        f"{source}->{source + 1}"
        for source in sorted(_MIGRATIONS)
        if source + 1 <= SUPPORTED_STORAGE_VERSION
    )
