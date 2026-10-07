"""Batch E1 acceptance: the numbered storage migration of an existing ledger.

``docs/batch-e-plan.md`` §4.4/§7 asks for a numbered migration, not a schema rebuild, because a
rebuild would drop the rows the design treats as facts. Because it is numbered, it is also
something a test can interrupt, re-open and compare - which is what this file does:

* a genuine v1 file (built here with raw ``sqlite3`` from the v1 DDL the module owns, so the test
  cannot drift from the schema a real file has) opens at ``migrate.STORAGE_VERSION`` - never a
  hardcoded number, so adding a migration does not silently rewrite these assertions - keeps
  every old row readable, and gets a ``.pre-v1.bak`` snapshot next to it;
* re-opening a migrated file is idempotent: same version, no second snapshot, no rewrite of the
  first one;
* a file that records a *newer* storage version than this build understands is refused before
  anything is written to it;
* a migration interrupted by the step hook rolls back completely - version, tables and columns -
  and a later normal open still migrates the file.

"Unchanged" is asserted on the logical content (every row of every table, digested), not on file
bytes: SQLite may touch a header on open, and a refused open must not be called a write because
a WAL side file appeared.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hflow import migrate
from hflow.contracts import TaskSpec, TaskState
from hflow.store import Store, StoreError

#: The v1 DDL is read from the module rather than copied: a copy would drift from the layout a
#: real v1 file actually has, and migrating *that* file is the behaviour under test.
V1_SCHEMA = migrate._V1_SCHEMA  # noqa: SLF001

RUN_ID = "R-v1-legacy"
ATTEMPT_ID = "A-v1-legacy"
AUTHORIZATION_ID = "AUTH-v1-legacy"
LEGACY_USER_TEXT = "I approved one legacy run before the root ledger existed."
#: The digest of the v1 approval. The migration must carry it over untouched: an already-consumed
#: approval whose digest changed would look unused again.
LEGACY_BINDING_DIGEST = "sha256:" + "a" * 64
V1_CREATED_AT = "2026-09-01T00:00:00Z"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _build_v1_database(
    path: Path,
    spec: TaskSpec,
    *,
    record_version: bool = False,
    authorization_used: int = 1,
    authorization_max: int = 2,
) -> None:
    """Write a v1-shaped database: the v1 DDL, one run, one attempt, one consumed approval.

    ``record_version`` writes ``storage_version = 1`` into ``schema_meta``. Without it the file is
    a *version-less* v1 file, which is what the pre-E1 build actually produced: it created
    ``schema_meta`` and never recorded a storage version in it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(V1_SCHEMA)
        connection.execute(
            """
            INSERT INTO runs (
                run_id, project_id, task_id, schema_version, spec_digest, task_spec_json,
                task_revision, task_state, controller_build, checks_digest,
                turn_limit, repair_limit, turns_reserved, repairs_used, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                "demo-project",
                spec.task_id,
                spec.schema_version,
                spec.spec_digest(),
                json.dumps(spec.model_dump(mode="json")),
                spec.revision,
                TaskState.BLOCKED.value,
                "v1-build",
                "checks-v1",
                4,
                0,
                1,
                0,
                V1_CREATED_AT,
                V1_CREATED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO attempts (
                attempt_id, run_id, task_revision, role, state, reservation_id,
                reserved_agent_turns, reserved_expires_at, invocation_id, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ATTEMPT_ID,
                RUN_ID,
                spec.revision,
                "implementer",
                "FAILED",
                "B-v1-legacy",
                1,
                "2026-09-01T01:00:00Z",
                "I-v1-legacy",
                "failed",
                V1_CREATED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO authorizations (
                authorization_id, mode, binding_digest, user_text, provided_by,
                authorized_at, max_top_level_submissions, used_top_level_submissions, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                AUTHORIZATION_ID,
                "m2-live-change",
                LEGACY_BINDING_DIGEST,
                LEGACY_USER_TEXT,
                "user",
                V1_CREATED_AT,
                authorization_max,
                authorization_used,
                V1_CREATED_AT,
            ),
        )
        if record_version:
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('storage_version', '1')"
            )
        connection.commit()
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _stored_version(path: Path) -> int | None:
    connection = sqlite3.connect(str(path))
    try:
        return migrate.recorded_version(connection)
    finally:
        connection.close()


def _content_digest(path: Path) -> dict[str, str]:
    """Every row of every user table, digested: logical content, not file bytes."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        digest: dict[str, str] = {}
        for table in sorted(_tables(path)):
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            payload = json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
            digest[table] = hashlib.sha256(payload).hexdigest()
        return digest
    finally:
        connection.close()


def _file_fingerprint(path: Path) -> tuple[int, int, str]:
    return (
        path.stat().st_mtime_ns,
        path.stat().st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _backups_next_to(path: Path) -> list[str]:
    return sorted(
        entry.name for entry in path.parent.iterdir() if ".pre-v" in entry.name
    )


def _assert_legacy_rows_are_readable(store: Store, spec: TaskSpec) -> None:
    run = store.get_run(RUN_ID)
    assert run["task_id"] == spec.task_id
    assert run["task_revision"] == spec.revision
    assert run["task_state"] == TaskState.BLOCKED.value
    assert run["spec_digest"] == spec.spec_digest()
    assert json.loads(run["task_spec_json"])["task_id"] == spec.task_id
    assert run["turns_reserved"] == 1

    authorization = store.authorization_state(AUTHORIZATION_ID)
    assert authorization is not None
    assert authorization["binding_digest"] == LEGACY_BINDING_DIGEST, (
        "a stored approval digest is a fact: the migration must not recompute it"
    )
    assert authorization["user_text"] == LEGACY_USER_TEXT
    assert authorization["used_top_level_submissions"] == 1
    assert authorization["max_top_level_submissions"] == 2
    assert authorization["root_id"] == "", "the new column is empty for a legacy approval"

    attempts = store.attempts_for(RUN_ID)
    assert [row["attempt_id"] for row in attempts] == [ATTEMPT_ID]
    assert attempts[0]["invocation_id"] == "I-v1-legacy"
    assert attempts[0]["root_id"] == "", "the new column is empty for a legacy attempt"


# --------------------------------------------------------------------------
# 1. a v1 file opens as v2 with its history intact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("records_version", [False, True])
def test_a_v1_file_migrates_to_v2_and_keeps_its_old_rows(
    tmp_path: Path, task_spec: TaskSpec, records_version: bool
) -> None:
    """The migration adds the ledger; it does not touch what the v1 file recorded.

    Both v1 shapes are covered: one that never recorded a storage version (what the pre-E1 build
    produced) and one that names itself v1. The backup's *name* is the evidence of how the file
    was read: ``.pre-v1.bak`` is only produced for a file the migrator treated as version 1.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec, record_version=records_version)

    assert _stored_version(path) == (1 if records_version else None)
    backup = migrate.backup_path_for(path, 1)
    assert backup == Path(str(path) + ".pre-v1.bak")
    assert not backup.exists(), "no snapshot may exist before the migration runs"

    store = Store(path)
    try:
        assert store.storage_version == migrate.STORAGE_VERSION >= 2
        assert store.migrated_from == 1, (
            "the file was read as v1 whether or not it recorded that version: the backup's "
            ".pre-v1.bak name and this field say the same thing"
        )
        assert store.migration_backup == backup
        assert backup.exists()

        _assert_legacy_rows_are_readable(store, task_spec)

        # The v2 objects exist and are empty: the migration adds, it never back-fills.
        assert store.root_budget_row("root-anything") is None
        assert store.invocations_for(RUN_ID) == []
        assert {"root_budgets", "invocations"} <= _tables(path)
        assert {"root_id", "root_budget_json", "root_limits_json"} <= _columns(
            path, "authorizations"
        )
        assert "root_id" in _columns(path, "attempts")
    finally:
        store.close()

    # The snapshot is the pre-migration file: it has no v2 table and no v2 column, so restoring
    # it is a real rollback rather than a copy of the upgraded schema.
    assert "root_budgets" not in _tables(backup)
    assert "invocations" not in _tables(backup)
    assert "root_id" not in _columns(backup, "authorizations")
    assert "root_id" not in _columns(backup, "attempts")
    assert _stored_version(backup) == (1 if records_version else None)


def test_reopening_a_migrated_file_is_idempotent(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """A second open writes no second snapshot and rewrites nothing."""
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec, record_version=True)

    first = Store(path)
    try:
        assert first.migrated_from == 1
    finally:
        first.close()

    backup = migrate.backup_path_for(path, 1)
    assert backup.exists()
    fingerprint_before = _file_fingerprint(backup)

    second = Store(path)
    try:
        assert second.storage_version == migrate.STORAGE_VERSION
        assert second.migrated_from is None, "the file was already v2 when this connection opened"
        assert second.migration_backup is None, "a file that needs no migration gets no snapshot"
        _assert_legacy_rows_are_readable(second, task_spec)
    finally:
        second.close()

    third = Store(path)
    third.close()

    assert _file_fingerprint(backup) == fingerprint_before, "the snapshot is never overwritten"
    assert _backups_next_to(path) == [backup.name], "exactly one pre-migration snapshot exists"


# --------------------------------------------------------------------------
# 2. a newer storage version is refused before anything is written
# --------------------------------------------------------------------------


def test_a_newer_recorded_storage_version_is_refused_without_touching_the_file(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """An unknown future layout is refused with nothing read, written or copied."""
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec, record_version=True)
    upgraded = Store(path)  # a genuine current-version file, relabelled below as a future one
    upgraded.close()

    # One past what this build understands, whatever that number is: hardcoding it would make
    # this test pass for the wrong reason the next time a migration is added.
    future = migrate.STORAGE_VERSION + 1
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'storage_version'", (str(future),)
        )
        connection.commit()
    finally:
        connection.close()

    before = _content_digest(path)
    assert _stored_version(path) == future
    future_backup = migrate.backup_path_for(path, future)
    assert not future_backup.exists()

    with pytest.raises(StoreError) as refused:
        Store(path)

    message = str(refused.value)
    assert f"records storage version {future}" in message
    assert "at most" in message
    assert "Refusing to open it" in message

    assert _stored_version(path) == future, "the refusal must not rewrite the recorded version"
    assert _content_digest(path) == before, "a refused open must not modify a row"
    assert not future_backup.exists(), "a refused file gets no snapshot either"


# --------------------------------------------------------------------------
# 3. an interrupted migration rolls back completely
# --------------------------------------------------------------------------


def test_a_migration_interrupted_at_any_step_rolls_back_and_a_later_open_succeeds(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """Interrupt every step in turn, then migrate normally.

    The step list is discovered from a real migration instead of hard-coded, so this covers
    whichever steps the migration actually has - including the one that stamps the version. Each
    interruption must leave the file exactly as v1: no version recorded, no v2 table, no v2
    column, the old rows intact, and a later normal open still able to migrate it.
    """
    reference = tmp_path / "reference" / "hflow.sqlite"
    _build_v1_database(reference, task_spec)
    # Storage v6 adds columns to ``runs`` itself, so the migrated reference no longer has the v1
    # row shape; an interrupted file is compared with an untouched v1 build instead.
    pristine = tmp_path / "pristine" / "hflow.sqlite"
    _build_v1_database(pristine, task_spec)
    pristine_runs = _content_digest(pristine)["runs"]
    steps: list[str] = []
    migrated = Store(reference, on_migration_step=steps.append)
    migrated.close()
    assert steps, "the hook must report the steps of a successful migration"

    for index, interrupted_at in enumerate(steps):
        path = tmp_path / f"interrupted-{index}" / "hflow.sqlite"
        _build_v1_database(path, task_spec)
        seen: list[str] = []

        def hook(step: str, *, _seen: list[str] = seen, _stop: str = interrupted_at) -> None:
            _seen.append(step)
            if step == _stop:
                raise migrate.MigrationError(f"test interruption after {step}")

        with pytest.raises(StoreError) as refused:
            Store(path, on_migration_step=hook)

        assert f"test interruption after {interrupted_at}" in str(refused.value)
        assert seen[-1] == interrupted_at

        # Nothing survived the interruption: not the version stamp, not the tables, not the
        # columns, and not a single row of the new shape.
        assert _stored_version(path) is None, "the version stamp is inside the same transaction"
        assert "root_budgets" not in _tables(path)
        assert "invocations" not in _tables(path)
        assert "root_id" not in _columns(path, "authorizations")
        assert "root_id" not in _columns(path, "attempts")
        assert "owner_token" not in _columns(path, "runs")
        assert "invocation_settlements" not in _tables(path)
        assert "integrations" not in _tables(path)
        assert _content_digest(path)["runs"] == pristine_runs

        # The snapshot is taken before the transaction by design, so it is there to restore
        # from even though the migration itself rolled back.
        backup = migrate.backup_path_for(path, 1)
        assert backup.exists()

        later = Store(path)
        try:
            assert later.storage_version == migrate.STORAGE_VERSION
            assert later.migrated_from == 1, "the interrupted file was still v1"
            _assert_legacy_rows_are_readable(later, task_spec)
            assert {"root_budgets", "invocations", "integrations"} <= _tables(path)
            assert "root_id" in _columns(path, "attempts")
        finally:
            later.close()


def test_a_hook_raising_an_unexpected_error_still_rolls_back_and_a_later_open_succeeds(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """A hook that fails in an unexpected way keeps its own type; the rollback is unchanged.

    The hook exists so a test can raise at a chosen point, which means a bug in one must not
    leave the file half-migrated *or* a half-open handle behind: the next open is exactly what an
    operator would try. ``MigrationError`` is reported as the store's own error; anything else
    keeps its own type after the cleanup, and either way nothing of the migration survives.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec)

    def hook(step: str) -> None:
        raise RuntimeError(f"hook bug after {step}")

    with pytest.raises(RuntimeError, match="hook bug after"):
        Store(path, on_migration_step=hook)

    assert _stored_version(path) is None
    assert "root_budgets" not in _tables(path)
    assert "invocations" not in _tables(path)
    assert "root_id" not in _columns(path, "authorizations")
    assert "root_id" not in _columns(path, "attempts")

    later = Store(path)
    try:
        assert later.storage_version == migrate.STORAGE_VERSION
        assert later.migrated_from == 1
        _assert_legacy_rows_are_readable(later, task_spec)
    finally:
        later.close()


# --------------------------------------------------------------------------
# 4. only a whole, verified copy is ever trusted as the pre-migration snapshot
# --------------------------------------------------------------------------


class _BackupFailsPartway:
    """A ledger connection whose backup copies one page and then fails, as a full disk would.

    Everything but ``backup`` is the real connection, so the migration under test is the real one;
    only the failure is staged, and it happens *after* SQLite has written into the destination.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)

    def backup(self, target: sqlite3.Connection, **_kwargs: object) -> None:
        def fail(_status: int, _remaining: int, _total: int) -> None:
            raise OSError("test: no space left on device during the snapshot")

        self._connection.backup(target, pages=1, progress=fail)


def _assert_valid_snapshot(backup: Path, version: int) -> None:
    assert migrate._snapshot_defect(backup, version) is None  # noqa: SLF001
    connection = sqlite3.connect(str(backup))
    try:
        assert connection.execute("SELECT run_id FROM runs").fetchall() == [(RUN_ID,)]
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()


def _assert_ledger_untouched(path: Path, before: dict[str, str]) -> None:
    assert _stored_version(path) is None, "a refused open must not stamp a version"
    assert "root_budgets" not in _tables(path)
    assert _content_digest(path) == before


def test_a_backup_that_fails_partway_leaves_no_snapshot_and_a_later_open_makes_a_valid_one(
    tmp_path: Path, task_spec: TaskSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted copy never sits at the name a later open trusts as the restore point."""
    import hflow.store as store_module

    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec)
    before = _content_digest(path)
    backup = migrate.backup_path_for(path, 1)
    real_migrate = store_module.migrate

    def migrate_with_failing_backup(connection, database, **kwargs):  # type: ignore[no-untyped-def]
        return real_migrate(_BackupFailsPartway(connection), database, **kwargs)

    monkeypatch.setattr(store_module, "migrate", migrate_with_failing_backup)
    with pytest.raises(OSError, match="no space left on device"):
        Store(path)
    monkeypatch.undo()

    assert not backup.exists(), "a failed copy must not be left at the snapshot's name"
    assert _backups_next_to(path) == [], "no partial copy or its journal is left behind either"
    _assert_ledger_untouched(path, before)

    store = Store(path)
    try:
        assert store.migrated_from == 1
        assert store.migration_backup == backup
        _assert_legacy_rows_are_readable(store, task_spec)
    finally:
        store.close()
    _assert_valid_snapshot(backup, 1)
    assert _backups_next_to(path) == [backup.name]


def test_a_stale_partial_copy_and_its_journal_do_not_leak_into_the_new_snapshot(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """A temp file (and a hot journal beside it) left by a killed process is discarded first."""
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec)
    backup = migrate.backup_path_for(path, 1)
    partial = backup.with_name(backup.name + ".partial")
    partial.write_bytes(b"half a database")
    partial.with_name(partial.name + "-journal").write_bytes(b"a journal from a killed process")

    store = Store(path)
    try:
        assert store.migration_backup == backup
    finally:
        store.close()
    _assert_valid_snapshot(backup, 1)
    assert _backups_next_to(path) == [backup.name]


def _plant_empty(path: Path, backup: Path) -> None:
    backup.write_bytes(b"")


def _plant_truncated(path: Path, backup: Path) -> None:
    source = sqlite3.connect(str(path))
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    data = backup.read_bytes()
    assert len(data) > 4096, "the fixture ledger must span more than one page"
    backup.write_bytes(data[: len(data) // 2])


def _plant_wrong_version(path: Path, backup: Path) -> None:
    other = backup.parent / "unrelated" / "hflow.sqlite"
    Store(other).close()  # a whole, healthy database - of the wrong version
    source = sqlite3.connect(str(other))
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


@pytest.mark.parametrize(
    ("plant", "reason"),
    [
        (_plant_empty, "it is empty (0 bytes)"),
        (_plant_truncated, "it cannot be read as an HFlow database"),
        (_plant_wrong_version, f"reads as storage version {migrate.STORAGE_VERSION}"),
    ],
    ids=["empty", "truncated", "wrong-version"],
)
def test_a_defective_file_at_the_snapshot_name_refuses_the_migration(
    tmp_path: Path, task_spec: TaskSpec, plant: object, reason: str
) -> None:
    """A leftover file is never reported as the restore point, and never overwritten."""
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec)
    before = _content_digest(path)
    backup = migrate.backup_path_for(path, 1)
    plant(path, backup)  # type: ignore[operator]
    planted = backup.read_bytes()

    with pytest.raises(StoreError) as refused:
        Store(path)

    message = str(refused.value)
    assert str(backup) in message
    assert reason in message
    assert "Move it aside" in message
    assert "The ledger was not changed" in message
    _assert_ledger_untouched(path, before)
    assert backup.read_bytes() == planted, "the earliest snapshot's name is never overwritten"
    assert _backups_next_to(path) == [backup.name], "the check leaves no side file behind"

    # The documented way forward: move the bad file aside and open again.
    backup.rename(backup.with_name("set-aside.bak"))
    store = Store(path)
    try:
        assert store.migration_backup == backup
        _assert_legacy_rows_are_readable(store, task_spec)
    finally:
        store.close()
    _assert_valid_snapshot(backup, 1)


def test_a_valid_wal_flagged_snapshot_from_an_earlier_build_is_trusted_and_left_clean(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """Snapshots written before this fix kept the ledger's WAL flag; they are still restorable.

    Checking one read-only creates ``-wal``/``-shm`` files beside it; the check removes the ones it
    made, so the snapshot stays one file and the bytes an operator restores are unchanged.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(path, task_spec)
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        backup = migrate.backup_path_for(path, 1)
        destination = sqlite3.connect(str(backup))
        try:
            connection.backup(destination)
        finally:
            destination.close()
    finally:
        connection.close()
    snapshot = backup.read_bytes()

    store = Store(path)
    try:
        assert store.migrated_from == 1
        assert store.migration_backup == backup
    finally:
        store.close()
    assert backup.read_bytes() == snapshot
    assert _backups_next_to(path) == [backup.name]


# --------------------------------------------------------------------------
# the snapshot check opens the copy on a network path too (review G, R11)
# --------------------------------------------------------------------------


def _admin_share_path(path: Path) -> Path | None:
    r"""``path`` addressed as ``\\localhost\<drive>$\...``, or ``None`` if that is unreachable."""
    resolved = path.resolve()
    drive = resolved.drive
    if len(drive) != 2 or drive[1] != ":":
        return None
    unc = Path(r"\\localhost" + "\\" + drive[0] + "$\\" + str(resolved)[3:])
    try:
        return unc if unc.exists() else None
    except OSError:
        return None


@pytest.mark.skipif(os.name != "nt", reason="UNC paths and admin shares are Windows-only")
def test_a_ledger_on_a_unc_path_migrates_and_its_snapshot_verifies(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    r"""A ledger on ``\\server\share\...`` is backed up, verified and migrated like a local one.

    The read-only check used to open the copy through ``Path.resolve().as_uri()``, which gives
    ``file://localhost/C%24/...`` - a URI SQLite will not open - so every open of an old-version
    ledger on a share was refused with a snapshot "failed verification" that was not true.
    """
    local = tmp_path / "data" / "hflow.sqlite"
    _build_v1_database(local, task_spec)
    path = _admin_share_path(local)
    if path is None:
        pytest.skip(r"the \\localhost\<drive>$ admin share is not reachable here")

    store = Store(path)
    try:
        assert store.migrated_from == 1
        backup = store.migration_backup
        assert backup == migrate.backup_path_for(path, 1)
        _assert_legacy_rows_are_readable(store, task_spec)
    finally:
        store.close()
    _assert_valid_snapshot(backup, 1)
    assert _backups_next_to(local) == [backup.name], "no partial copy or side file is left behind"

    # An existing snapshot at the final name is verified through the same open, and trusted.
    assert migrate._snapshot_defect(backup, 1) is None  # noqa: SLF001


@pytest.mark.skipif(os.name != "nt", reason="drive letters and UNC paths are Windows-only")
def test_the_read_only_uri_keeps_a_drive_or_unc_path_unresolved_and_escaped() -> None:
    """A mapped drive stays a drive (``resolve()`` would rewrite it to UNC), a UNC path gets the
    four-slash form SQLite reads, and ``#``/``%``/space stay part of the file name."""
    uri = migrate._readonly_uri  # noqa: SLF001
    assert uri(Path(r"Z:\ledgers\hflow.sqlite")) == "file:///Z:/ledgers/hflow.sqlite?mode=ro"
    assert (
        uri(Path(r"\\server\share\a b\h#1%.sqlite"))
        == "file:////server/share/a%20b/h%231%25.sqlite?mode=ro"
    )
    assert (
        uri(Path(r"\\?\UNC\server\share\hflow.sqlite"))
        == "file:////server/share/hflow.sqlite?mode=ro"
    )
    assert uri(Path(r"\\?\C:\data\hflow.sqlite")) == "file:///C:/data/hflow.sqlite?mode=ro"


def test_a_snapshot_whose_path_has_uri_special_characters_verifies(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """``#``, ``%`` and spaces in the data directory are path characters, not URI syntax."""
    path = tmp_path / "data #1 100%" / "hflow.sqlite"
    _build_v1_database(path, task_spec)
    store = Store(path)
    try:
        assert store.migrated_from == 1
        backup = store.migration_backup
    finally:
        store.close()
    _assert_valid_snapshot(backup, 1)
