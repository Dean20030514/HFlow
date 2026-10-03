"""Owner lease (user ruling, 2026-10-03): a run is owned by a controller *process*.

The claim records a random per-controller token with the owning process's pid, creation time
(``GetProcessTimes``) and host, and the controller holds an OS file lock for its lifetime. These
tests pin the rule end to end, offline, with real ctypes calls on Windows:

* a live owner keeps a second controller off the run (it returns the run as it stands, exit 5);
* a successor takes over only when the owner is *proven* gone - lock free **and** identity gone -
  and the takeover blocks the run ``owner_lost``, turns the open ledger entries unknown, never
  re-dispatches and never reports a confirmed stop;
* pid reuse (a different creation time) reads ``gone``; access denied reads ``unknown`` and refuses;
  a held lock refuses;
* a superseded ("zombie") owner's guarded writes fail;
* storage v5 -> v6 keeps every row and records no owner for an old one.

No sleeps: a dead owner is a short Python child that has exited, and its identity was read while
it ran.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import hflow.ownership as ownership
from hflow import migrate
from hflow.cli import EXIT_BLOCKED, EXIT_IN_PROGRESS, _outcome_exit_code, main
from hflow.contracts import (
    AttemptState,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RefusedError,
    RunRequest,
    SpawnFact,
    SpawnKind,
    TaskSpec,
    TaskState,
)
from hflow.controller import Controller, RunOutcome, inspect_run
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.ids import utc_now
from hflow.ownership import (
    OwnerFence,
    OwnerLock,
    ProcessIdentity,
    current_identity,
    identity_of,
    lock_is_free,
    lock_path_for,
    new_owner_token,
    probe,
)
from hflow.report import report_json, status_text
from hflow.store import OwnerLostError, Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

from .test_batch_e_dispatch import _authorization, _binding, _limits

windows_only = pytest.mark.skipif(
    not ownership.winjob.IS_WINDOWS, reason="the identity probe reads Windows process handles"
)

LABEL = "owner-lease-test"
EXPIRES_AT = "2999-01-01T00:00:00Z"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _controller(store: Store, project_root: Path, fake_script: FakeScript) -> Controller:
    runner = FakeCheckRunner()
    return Controller(
        store,
        FakeDriver(project_root, fake_script),
        controller_build="test-build",
        runners=CheckRunners({"fake": runner, "command": runner}),
    )


def _start_idle_child() -> subprocess.Popen:
    """A Python child that waits on stdin: alive until the test closes its stdin."""
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dead_identity() -> ProcessIdentity:
    """The identity of a process that existed and has exited: read while it ran, then ended."""
    child = _start_idle_child()
    try:
        identity = identity_of(child.pid)
        assert identity is not None, "the child's identity must be readable while it runs"
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(30)
    return identity


def _create_run(store: Store, run_request: RunRequest, run_id: str = "R-owner") -> str:
    spec = run_request.task
    return store.create_run(
        run_id=run_id,
        project_id=run_request.project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="test-build",
        checks_digest=run_request.project.checks_digest(),
        turn_limit=spec.budget.max_agent_turns,
        repair_limit=0,
    )["run_id"]


def _seed_owned_dispatch(
    store: Store,
    *,
    run_request: RunRequest,
    project_root: Path,
    token: str,
    identity: ProcessIdentity,
    child_pid: int | None,
    launched: bool,
) -> tuple[str, str]:
    """A RUNNING root run owned by ``identity`` with one implementer invocation in flight.

    ``launched`` records a spawn report (a process existed: ``started``); otherwise the launch is
    only requested. Returns ``(run_id, invocation_id)``.
    """
    spec: TaskSpec = run_request.task
    binding = _binding(store, spec, project_root)
    limits = _limits()
    store.register_root_budget(binding, limits)
    authorization = _authorization(
        spec=spec, binding=binding, limits=limits, project_root=project_root
    )
    store.register_authorization(authorization.as_store_record())
    run_id = _create_run(store, run_request)
    generation = store.claim_run_owned(run_id, LABEL, token=token, identity=identity)
    assert generation == 1
    reservation = store.reserve_dispatch(
        run_id=run_id,
        controller_id=LABEL,
        invocation_id="I-owner-impl",
        role="implementer",
        reservation_id="B-owner-impl",
        reserved_turns=1,
        reservation_expires_at=EXPIRES_AT,
        root_binding=binding,
        root_limits=limits,
        authorization_id=authorization.authorization_id,
        authorization_max=4,
        fence=OwnerFence(token, generation),
    )
    assert store.get_run(run_id)["task_state"] == TaskState.RUNNING.value
    invocation_id = reservation.invocation.invocation_id
    assert store.mark_invocation_launch_requested(invocation_id)
    if launched:
        store.record_invocation_spawn(
            SpawnFact(
                invocation_id=invocation_id,
                created=True,
                pid=child_pid,
                spawn_kind=SpawnKind.PROCESS,
                detail="test spawn report",
            )
        )
    return run_id, invocation_id


def _snapshot(store: Store, run_id: str) -> dict[str, object]:
    row = store.get_run(run_id)
    return {
        "state": row["task_state"],
        "block": row["block_code"],
        "token": row["owner_token"],
        "generation": row["claim_generation"],
        "invocations": [(i.invocation_id, i.state.value) for i in store.invocations_for(run_id)],
        "attempts": [(a["attempt_id"], a["state"]) for a in store.attempts_for(run_id)],
        "turns": row["turns_reserved"],
    }


# --------------------------------------------------------------------------
# 1. identity probe: matching, gone (exited), gone (pid reused), unknown
# --------------------------------------------------------------------------


@windows_only
def test_this_process_reads_matching_and_a_reused_pid_reads_gone() -> None:
    me = current_identity()
    assert me.created is not None
    assert probe(me).verdict == "matching"

    reused = dataclasses.replace(me, created=me.created - 1)
    observed = probe(reused)
    assert observed.verdict == "gone"
    assert "reused" in observed.detail


@windows_only
def test_an_exited_process_reads_gone_even_while_a_handle_keeps_it_openable() -> None:
    child = _start_idle_child()
    identity = identity_of(child.pid)
    assert identity is not None
    assert probe(identity).verdict == "matching"
    assert child.stdin is not None
    child.stdin.close()
    child.wait(30)
    # ``Popen`` still holds a handle, so OpenProcess succeeds; the signalled object says "exited".
    observed = probe(identity)
    assert observed.verdict == "gone", observed.detail


@windows_only
def test_access_denied_and_another_host_read_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    me = current_identity()
    other_host = dataclasses.replace(me, host=me.host + "-elsewhere")
    assert probe(other_host).verdict == "unknown"

    monkeypatch.setattr(
        ownership, "_open_process", lambda pid: (None, ownership.ERROR_ACCESS_DENIED)
    )
    observed = probe(me)
    assert observed.verdict == "unknown"
    assert "not proof of absence" in observed.detail


def test_the_owner_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    path = lock_path_for(tmp_path, new_owner_token())
    assert lock_is_free(path) == "absent"
    lock = OwnerLock(path)
    lock.acquire()
    try:
        assert lock_is_free(path) == "held"
        with pytest.raises(ownership.OwnerLockError):
            OwnerLock(path).acquire()
    finally:
        lock.release()
    assert lock_is_free(path) in {"absent", "free"}


# --------------------------------------------------------------------------
# 2. a live owner keeps a second controller off the run
# --------------------------------------------------------------------------


@windows_only
def test_a_live_owner_makes_a_second_controller_return_the_run_without_driving_it(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    first = _controller(store, project_root, fake_script)
    second = _controller(store, project_root, fake_script)
    run_id = _create_run(store, run_request)
    assert first._claim(run_id)

    outcome = second.run_task(run_request)

    assert outcome.run_id == run_id
    assert outcome.task_state is TaskState.DRAFT
    assert outcome.block_code is None
    notes = " ".join(outcome.notes)
    assert "claimed by another controller" in notes, notes
    assert f"pid={os.getpid()}" in notes and "may be alive" in notes, notes
    assert second.driver.started == []  # type: ignore[attr-defined]
    assert store.attempts_for(run_id) == []
    assert store.get_run(run_id)["owner_token"] == first.owner_token
    assert _outcome_exit_code(outcome) == EXIT_IN_PROGRESS

    # resume from a third controller refuses too: the owner (this process) is alive.
    before = _snapshot(store, run_id)
    resumed = _controller(store, project_root, fake_script).resume(run_id)
    assert any("owner may be alive" in note for note in resumed.notes), resumed.notes
    assert _snapshot(store, run_id) == before

    text = status_text(inspect_run(store, run_id))
    assert f"owner         pid={os.getpid()}" in text
    assert "liveness    matching (lock held" in text
    assert report_json(inspect_run(store, run_id))["owner"]["liveness"] == "matching"


# --------------------------------------------------------------------------
# 3. a dead owner: resume takes over, blocks owner_lost, never re-dispatches
# --------------------------------------------------------------------------


@windows_only
@pytest.mark.parametrize("launched", [True, False], ids=["started", "requested"])
def test_a_dead_owner_is_taken_over_and_the_run_blocks_owner_lost(
    store: Store,
    project_root: Path,
    fake_script: FakeScript,
    run_request: RunRequest,
    launched: bool,
) -> None:
    dead = _dead_identity()
    token = new_owner_token()
    # A lock file left behind by the dead owner: it exists, and nobody holds it.
    lock_path = lock_path_for(store.path.parent, token)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")
    child = _start_idle_child()  # the run's recorded child: still running
    try:
        run_id, invocation_id = _seed_owned_dispatch(
            store,
            run_request=run_request,
            project_root=project_root,
            token=token,
            identity=dead,
            child_pid=child.pid,
            launched=launched,
        )
        before = _snapshot(store, run_id)
        successor = _controller(store, project_root, fake_script)

        outcome = successor.resume(run_id)

        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.block_code is RefusalCode.OWNER_LOST
        assert _outcome_exit_code(outcome) == EXIT_BLOCKED
        row = store.get_run(run_id)
        assert row["owner_token"] == successor.owner_token
        assert row["owner_pid"] == os.getpid()
        assert row["claim_generation"] == 2
        assert row["cancel_receipt_json"] is None, "a takeover never reports a stop"
        intent = store.invocation(invocation_id)
        assert intent is not None
        expected = (
            InvocationStartState.UNKNOWN if launched else InvocationStartState.LAUNCH_UNKNOWN
        )
        assert intent.state is expected
        # Nothing was bought or started: same invocations, same attempt, same reserved turns.
        assert [i for i, _ in _snapshot(store, run_id)["invocations"]] == [invocation_id]
        assert len(store.attempts_for(run_id)) == len(before["attempts"]) == 1
        assert store.attempts_for(run_id)[0]["state"] == AttemptState.OUTCOME_UNKNOWN.value
        assert row["turns_reserved"] == before["turns"]
        assert successor.driver.started == []  # type: ignore[attr-defined]
        notes = store.notes_for(run_id)
        assert any(note.startswith("owner_lost: the controller") for note in notes), notes
        joined = " ".join(outcome.notes)
        assert "confirmed_stopped" not in joined
        if launched:
            assert f"child process pid={child.pid}" in joined, joined
            assert "may still be running; nothing was stopped" in joined, joined

        # resume again: still blocked, no second takeover, nothing re-dispatched.
        again = _controller(store, project_root, fake_script).resume(run_id)
        assert again.task_state is TaskState.BLOCKED
        assert again.block_code is RefusalCode.OWNER_LOST
        assert store.get_run(run_id)["claim_generation"] == 2
        assert [i for i, _ in _snapshot(store, run_id)["invocations"]] == [invocation_id]
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(30)


@windows_only
def test_access_denied_refuses_the_takeover_and_changes_nothing(
    store: Store,
    project_root: Path,
    fake_script: FakeScript,
    run_request: RunRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = _dead_identity()
    run_id, _ = _seed_owned_dispatch(
        store,
        run_request=run_request,
        project_root=project_root,
        token=new_owner_token(),
        identity=dead,
        child_pid=None,
        launched=False,
    )
    before = _snapshot(store, run_id)
    monkeypatch.setattr(
        ownership, "_open_process", lambda pid: (None, ownership.ERROR_ACCESS_DENIED)
    )

    outcome = _controller(store, project_root, fake_script).resume(run_id)

    assert outcome.task_state is TaskState.RUNNING
    assert _outcome_exit_code(outcome) == EXIT_IN_PROGRESS
    assert any("owner may be alive" in note and "unknown" in note for note in outcome.notes)
    assert _snapshot(store, run_id) == before


@windows_only
def test_a_held_lock_refuses_the_takeover_even_when_the_identity_reads_gone(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    dead = _dead_identity()
    token = new_owner_token()
    run_id, _ = _seed_owned_dispatch(
        store,
        run_request=run_request,
        project_root=project_root,
        token=token,
        identity=dead,
        child_pid=None,
        launched=False,
    )
    held = OwnerLock(lock_path_for(store.path.parent, token))
    held.acquire()
    try:
        before = _snapshot(store, run_id)
        outcome = _controller(store, project_root, fake_script).resume(run_id)
        assert outcome.task_state is TaskState.RUNNING
        assert any("lock=held" in note for note in outcome.notes), outcome.notes
        assert _snapshot(store, run_id) == before
    finally:
        held.release()

    taken = _controller(store, project_root, fake_script).resume(run_id)
    assert taken.block_code is RefusalCode.OWNER_LOST


# --------------------------------------------------------------------------
# 4. a superseded ("zombie") owner's guarded writes fail
# --------------------------------------------------------------------------


@windows_only
def test_a_zombie_owners_reserve_dispatch_is_refused_after_a_takeover(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    zombie = _controller(store, project_root, fake_script)
    run_id = _create_run(store, run_request)
    assert zombie._claim(run_id)
    fence = zombie._fence(run_id)
    assert fence is not None and fence.generation == 1
    successor = _controller(store, project_root, fake_script)
    # The store's compare-and-set, exactly as resume runs it once the owner was proven gone.
    taken = store.take_over_run(
        run_id,
        expected_token=zombie.owner_token,
        expected_generation=1,
        controller_id=LABEL,
        token=successor.owner_token,
        identity=successor.owner_identity,
        reason="owner_lost: test takeover",
        detail="test",
        note="owner_lost: test takeover",
    )
    assert taken is not None and taken[0] == 2

    with pytest.raises(OwnerLostError, match="owner_lost"):
        store.reserve_dispatch(
            run_id=run_id,
            controller_id=zombie.controller_id,
            invocation_id="I-zombie",
            role="implementer",
            reservation_id="B-zombie",
            reserved_turns=1,
            reservation_expires_at=EXPIRES_AT,
            fence=fence,
        )
    assert store.attempts_for(run_id) == []
    assert store.get_run(run_id)["turns_reserved"] == 0
    assert zombie._dispatch_refusal_code(
        "owner_lost: run R is owned by pid=1"
    ) is RefusalCode.OWNER_LOST
    # A second takeover from the stale generation is a no-op, not a second block.
    assert store.take_over_run(
        run_id,
        expected_token=zombie.owner_token,
        expected_generation=1,
        controller_id=LABEL,
        token=new_owner_token(),
        identity=successor.owner_identity,
        reason="x",
        detail="x",
        note="x",
    ) is None


@windows_only
def test_a_zombie_owners_result_is_not_applied_after_a_takeover(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """The owner was presumed dead while its implementer ran: its late result changes nothing."""
    zombie = _controller(store, project_root, fake_script)
    successor = _controller(store, project_root, fake_script)
    original_start = zombie.driver.start

    def start(request):  # noqa: ANN001 - Protocol shape
        row = store.get_run(request.run_id)
        assert store.take_over_run(
            request.run_id,
            expected_token=row["owner_token"],
            expected_generation=int(row["claim_generation"]),
            controller_id=LABEL,
            token=successor.owner_token,
            identity=successor.owner_identity,
            reason="owner_lost: taken over while the implementer ran",
            detail="test",
            note="owner_lost: taken over while the implementer ran",
        ) is not None
        return original_start(request)

    zombie.driver.start = start  # type: ignore[method-assign]
    outcome = zombie.run_task(run_request)

    row = store.get_run(outcome.run_id)
    assert row["task_state"] == TaskState.BLOCKED.value
    assert row["block_code"] == RefusalCode.OWNER_LOST.value
    assert row["receipt_json"] is None
    attempt = store.attempts_for(outcome.run_id)[0]
    assert attempt["state"] == AttemptState.OUTCOME_UNKNOWN.value
    assert attempt["outcome"] == InvocationOutcome.OUTCOME_UNKNOWN.value
    notes = store.notes_for(outcome.run_id)
    assert any(note.startswith("late_result") and "owner_lost" in note for note in notes), notes


# --------------------------------------------------------------------------
# 5. a run recorded before owner identity (storage v5) and the v5 -> v6 migration
# --------------------------------------------------------------------------


def _v5_database(
    path: Path,
    task_spec: TaskSpec,
    *,
    attempt_pid: int | None,
    started_at: str | None,
    with_invocation: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        migrate.migrate(conn, path, supported=5)
        now = "2026-10-01T00:00:00Z"
        conn.execute(
            """
            INSERT INTO runs (run_id, project_id, task_id, schema_version, spec_digest,
                task_spec_json, task_revision, task_state, controller_build, checks_digest,
                turn_limit, repair_limit, claimed_by, claimed_at, created_at, updated_at)
            VALUES ('R-legacy', 'demo-project', ?, 1, ?, ?, 1, 'RUNNING', 'v5-build',
                'sha256:checks', 4, 0, 'local-controller', ?, ?, ?)
            """,
            (
                task_spec.task_id,
                task_spec.spec_digest(),
                task_spec.model_dump_json(),
                now,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO attempts (attempt_id, run_id, task_revision, role, state, process_id,
                process_started_at, process_identity, created_at)
            VALUES ('A-legacy', 'R-legacy', 1, 'implementer', 'ACTIVE', ?, ?, 'legacy', ?)
            """,
            (attempt_pid, started_at, now),
        )
        conn.execute(
            "INSERT INTO run_notes (note_id, run_id, note, created_at) "
            "VALUES ('N-legacy', 'R-legacy', 'a v5 note', ?)",
            (now,),
        )
        if with_invocation:
            # The implementer the legacy controller launched: started, never settled.
            conn.execute(
                "INSERT INTO invocations (invocation_id, run_id, attempt_id, role, state, "
                "reserved_at, started_at) VALUES ('I-legacy', 'R-legacy', 'A-legacy', "
                "'implementer', 'started', ?, ?)",
                (now, now),
            )
            conn.execute("UPDATE attempts SET invocation_id = 'I-legacy' WHERE attempt_id = 'A-legacy'")
    finally:
        conn.close()


def _rows(path: Path, table: str, columns: list[str]) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return list(conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1"))
    finally:
        conn.close()


def test_migration_v5_to_v6_keeps_every_row_and_records_no_owner(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=1, started_at="2026-10-01T00:00:00Z")
    conn = sqlite3.connect(str(path))
    try:
        before = {
            table: (
                [row[1] for row in conn.execute(f"PRAGMA table_info({table})")],
                None,
            )
            for table in ("runs", "attempts", "run_notes")
        }
    finally:
        conn.close()
    before_rows = {
        table: _rows(path, table, columns) for table, (columns, _) in before.items()
    }

    store = Store(path)
    try:
        assert store.migrated_from == 5
        # v6 adds the owner columns; later versions keep them.
        assert store.storage_version == migrate.STORAGE_VERSION >= 6
        assert migrate.backup_path_for(path, 5).exists()
        row = store.get_run("R-legacy")
        assert row["owner_token"] is None and row["owner_pid"] is None
        assert row["owner_created"] is None and row["owner_host"] is None
        assert row["claim_generation"] == 0
        assert row["claimed_by"] == "local-controller"
    finally:
        store.close()
    for table, (columns, _) in before.items():
        assert _rows(path, table, columns) == before_rows[table], table


@windows_only
def test_a_legacy_run_whose_recorded_controller_pid_is_absent_here_is_not_taken_over(
    tmp_path: Path, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript
) -> None:
    """A pre-v6 controller pid carries no host: absent here is not proof it is gone (H2).

    A ledger shared between hosts could hold a pre-v6 run whose controller still runs on another
    machine; a local lookup of its pid would read "no such process". So the probe reads
    ``unknown`` and resume changes nothing. The way out is ``hflow cancel`` (which blocks the run
    ``outcome_unknown``) and then ``hflow resume``, which reconciles it - never a re-dispatch.
    """
    dead = _dead_identity()
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(
        path, task_spec, attempt_pid=dead.pid, started_at="2000-01-01T00:00:00Z",
        with_invocation=True,
    )
    store = Store(path)
    try:
        before = _snapshot(store, "R-legacy")
        outcome = _controller(store, project_root, fake_script).resume("R-legacy")
        assert outcome.task_state is TaskState.RUNNING, outcome.notes
        assert outcome.block_code is None
        joined = " ".join(outcome.notes)
        assert "owner may be alive" in joined and "no host" in joined, joined
        assert "`hflow cancel`" in joined, joined
        assert _snapshot(store, "R-legacy") == before

        text = status_text(inspect_run(store, "R-legacy"))
        assert "liveness    unknown" in text, text
        assert "decided by rule, nothing was probed" in text, text
        assert "observed by this command" not in text, text

        receipt = _controller(store, project_root, fake_script).cancel("R-legacy")
        assert receipt.status != "confirmed_stopped", receipt
        row = store.get_run("R-legacy")
        assert row["task_state"] == TaskState.BLOCKED.value
        assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, row["block_reason"]

        resumed = _controller(store, project_root, fake_script).resume("R-legacy")
        assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN
        assert any("reconciled" in note for note in resumed.notes), resumed.notes
        assert len(store.attempts_for("R-legacy")) == 1, "nothing was re-dispatched"
        assert [i.invocation_id for i in store.invocations_for("R-legacy")] == ["I-legacy"]
    finally:
        store.close()


def test_a_stop_of_a_legacy_attempt_with_a_process_but_no_invocation_is_never_confirmed(
    tmp_path: Path, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript
) -> None:
    """A pre-invocation attempt that recorded a controller process may have launched something.

    No driver can be asked about it, so the stop is ``unknown`` and the run blocks
    ``outcome_unknown`` (rule 8) - never ``confirmed_stopped`` / ``cancelled_by_operator``.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=4194300, started_at="2026-10-01T00:00:00Z")
    store = Store(path)
    try:
        receipt = _controller(store, project_root, fake_script).cancel("R-legacy")
        assert receipt.status == "unknown", receipt
        assert receipt.local_process_stopped is False
        row = store.get_run("R-legacy")
        assert row["task_state"] == TaskState.BLOCKED.value
        assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, row["block_reason"]
        assert len(store.attempts_for("R-legacy")) == 1, "nothing was dispatched"
    finally:
        store.close()


def test_a_stop_of_an_ended_run_with_such_a_legacy_attempt_is_never_confirmed_either(
    tmp_path: Path, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=4194300, started_at="2026-10-01T00:00:00Z")
    store = Store(path)
    try:
        store.set_blocked("R-legacy", RefusalCode.OUTCOME_UNKNOWN, "a pre-v6 crash, reconciled")
        receipt = _controller(store, project_root, fake_script).cancel("R-legacy")
        assert receipt.status == "unknown", receipt
        assert receipt.local_process_stopped is False and receipt.run_already_ended is True
        row = store.get_run("R-legacy")
        assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, "the block is not relabelled"
    finally:
        store.close()


def test_a_terminal_runs_owner_line_never_claims_an_observation(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=4194300, started_at="2026-10-01T00:00:00Z")
    store = Store(path)
    try:
        store.set_blocked("R-legacy", RefusalCode.OUTCOME_UNKNOWN, "ended")
        text = status_text(inspect_run(store, "R-legacy"))
        assert "liveness    not_probed" in text, text
        assert "not probed: the run is terminal" in text, text
        assert "observed by this command" not in text, text
    finally:
        store.close()


def test_a_legacy_controller_pid_reads_unknown_without_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``assess_owner`` never looks a host-less pre-v6 pid up locally (H2), on any platform."""

    def no_lookup(pid: int):  # noqa: ANN202
        pytest.fail("a pre-v6 controller pid must not be looked up on this host")

    monkeypatch.setattr(ownership, "_open_process", no_lookup)
    assessment = ownership.assess_owner(
        ledger_dir=None,
        owner_token=None,
        owner_pid=None,
        owner_created=None,
        owner_host=None,
        recorded_controllers=[(4194300, "2000-01-01T00:00:00Z")],
    )
    assert assessment.gone is False
    assert assessment.probe.verdict == "unknown"
    assert "no host" in assessment.probe.detail


def test_no_recorded_owner_reads_not_recorded_never_gone() -> None:
    """No token and no recorded controller: nothing was observed, so it is not ``gone`` (H1)."""
    assessment = ownership.assess_owner(
        ledger_dir=None,
        owner_token=None,
        owner_pid=None,
        owner_created=None,
        owner_host=None,
        recorded_controllers=[],
    )
    assert assessment.gone is False
    assert assessment.probe.verdict == "not_recorded"
    assert "not proven gone" in assessment.probe.detail


def test_a_never_claimed_run_with_nothing_dispatched_is_taken_over_as_not_recorded(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """resume may block a not_recorded run only because nothing was in flight; never "gone"."""
    run_id = _create_run(store, run_request)
    text = status_text(inspect_run(store, run_id))
    assert "liveness    not_recorded" in text, text
    assert "liveness    gone" not in text

    outcome = _controller(store, project_root, fake_script).resume(run_id)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OWNER_LOST
    reason = outcome.block_reason or ""
    assert "no owner was recorded" in reason, reason
    assert "provably gone" not in reason, reason
    assert "nothing had been dispatched" in reason, reason
    assert store.get_run(run_id)["claim_generation"] == 1
    assert store.attempts_for(run_id) == [] and store.invocations_for(run_id) == []


def test_a_not_recorded_run_with_dispatched_work_is_not_taken_over(
    tmp_path: Path, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript
) -> None:
    """A pre-v6 run whose attempt recorded no controller pid: not_recorded, and work exists."""
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=None, started_at=None)
    store = Store(path)
    try:
        assert ownership.assess_owner(
            ledger_dir=None, owner_token=None, owner_pid=None, owner_created=None,
            owner_host=None, recorded_controllers=[],
        ).probe.verdict == "not_recorded"
        before = _snapshot(store, "R-legacy")
        outcome = _controller(store, project_root, fake_script).resume("R-legacy")
        assert outcome.task_state is TaskState.RUNNING
        joined = " ".join(outcome.notes)
        assert "no owner was recorded" in joined and "not taken over" in joined, joined
        assert _snapshot(store, "R-legacy") == before
        # The store's own compare-and-set refuses it too, whatever the caller decided.
        row = store.get_run("R-legacy")
        assert store.take_over_run(
            "R-legacy",
            expected_token=None,
            expected_generation=int(row["claim_generation"]),
            controller_id=LABEL,
            token=new_owner_token(),
            identity=current_identity(),
            reason="x",
            detail="x",
            note="x",
            require_nothing_dispatched=True,
        ) is None
        assert _snapshot(store, "R-legacy") == before
    finally:
        store.close()


@windows_only
def test_a_legacy_run_whose_recorded_controller_may_be_running_is_not_taken_over(
    tmp_path: Path, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript
) -> None:
    # This process was created before "now", so its pid with a start recorded now reads matching.
    path = tmp_path / "data" / "hflow.sqlite"
    _v5_database(path, task_spec, attempt_pid=os.getpid(), started_at=utc_now())
    store = Store(path)
    try:
        before = _snapshot(store, "R-legacy")
        outcome = _controller(store, project_root, fake_script).resume("R-legacy")
        assert outcome.task_state is TaskState.RUNNING
        assert any("owner may be alive" in note for note in outcome.notes), outcome.notes
        assert _snapshot(store, "R-legacy") == before
    finally:
        store.close()


# --------------------------------------------------------------------------
# 6. the CLI: resume exits 5 while the owner may be alive, 3 after a takeover
# --------------------------------------------------------------------------


@windows_only
def test_cli_resume_exit_codes_follow_the_owner(
    tmp_path: Path, run_request: RunRequest, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "cli-data"
    store = Store(data_dir / "hflow.sqlite")
    try:
        live = _create_run(store, run_request, run_id="R-live")
        owner = Controller(
            store, FakeDriver(Path(run_request.project_root)), controller_build="test-build"
        )
        assert owner._claim(live)
        spec2 = run_request.task.model_copy(update={"revision": 2})
        dead_run = store.create_run(
            run_id="R-dead",
            project_id=run_request.project.project_id,
            spec=spec2,
            spec_digest=spec2.spec_digest(),
            controller_build="test-build",
            checks_digest=run_request.project.checks_digest(),
            turn_limit=4,
            repair_limit=0,
        )["run_id"]
        assert store.claim_run_owned(
            dead_run, LABEL, token=new_owner_token(), identity=_dead_identity()
        ) == 1
    finally:
        store.close()

    root = str(run_request.project_root)
    assert main(["resume", "R-live", "--project-root", root, "--data-dir", str(data_dir)]) == (
        EXIT_IN_PROGRESS
    )
    assert "owner may be alive" in capsys.readouterr().out
    assert main(["resume", "R-dead", "--project-root", root, "--data-dir", str(data_dir)]) == (
        EXIT_BLOCKED
    )
    assert "owner_lost" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 7. the owner is written with the run; a dead owner's never-dispatched run is adopted
# --------------------------------------------------------------------------


def _owned_by_dead(store: Store, run_request: RunRequest) -> tuple[str, str, ProcessIdentity]:
    """A DRAFT run created by a controller that has exited, its lock file left behind, free."""
    dead = _dead_identity()
    token = new_owner_token()
    lock_path = lock_path_for(store.path.parent, token)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")
    spec = run_request.task
    row = store.create_run(
        run_id="R-dead-owner",
        project_id=run_request.project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="test-build",
        checks_digest=run_request.project.checks_digest(),
        turn_limit=spec.budget.max_agent_turns,
        repair_limit=0,
        controller_id=LABEL,
        owner_token=token,
        owner_identity=dead,
    )
    return str(row["run_id"]), token, dead


def test_create_run_writes_the_owner_in_the_same_insert(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """There is no instant in which a run this build created reads as "no owner recorded" (H1).

    While the creator is between the insert and its first dispatch, a successor's resume finds the
    creator's own token, its held lock and its matching identity - and changes nothing.
    """
    controller = _controller(store, project_root, fake_script)
    original = store.create_run
    seen: dict[str, object] = {}

    def create_run(**kwargs):  # noqa: ANN003
        row = original(**kwargs)
        seen["row"] = dict(row)
        before = _snapshot(store, row["run_id"])
        seen["resume"] = _controller(store, project_root, fake_script).resume(row["run_id"])
        seen["unchanged"] = _snapshot(store, row["run_id"]) == before
        return row

    store.create_run = create_run  # type: ignore[method-assign]
    outcome = controller.run_task(run_request)

    row = seen["row"]
    assert isinstance(row, dict)
    assert row["owner_token"] == controller.owner_token
    assert row["owner_pid"] == os.getpid() and row["owner_host"]
    assert row["claimed_by"] == controller.controller_id
    assert row["claim_generation"] == 1
    assert seen["unchanged"] is True
    resumed = seen["resume"]
    assert isinstance(resumed, RunOutcome)
    assert resumed.block_code is None
    if ownership.winjob.IS_WINDOWS:
        assert any("owner may be alive" in note for note in resumed.notes), resumed.notes
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert store.get_run(outcome.run_id)["claim_generation"] == 1


def test_an_owned_create_run_needs_the_owner_identity(
    store: Store, run_request: RunRequest
) -> None:
    spec = run_request.task
    with pytest.raises(StoreError, match="owner identity"):
        store.create_run(
            run_id="R-half-owned",
            project_id=run_request.project.project_id,
            spec=spec,
            spec_digest=spec.spec_digest(),
            controller_build="test-build",
            checks_digest=run_request.project.checks_digest(),
            turn_limit=4,
            repair_limit=0,
            owner_token=new_owner_token(),
        )
    assert store.find_run_by_spec_digest(run_request.project.project_id, spec.spec_digest()) is None


@windows_only
def test_a_dead_owners_never_dispatched_run_is_adopted_and_continued(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """Resubmitting the TaskSpec of a dead owner's DRAFT run continues that run (H0)."""
    run_id, token, dead = _owned_by_dead(store, run_request)
    successor = _controller(store, project_root, fake_script)

    outcome = successor.run_task(run_request)

    assert outcome.run_id == run_id, "the existing run is continued, never a second one"
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert outcome.implementer_invocations == 1
    row = store.get_run(run_id)
    assert row["owner_token"] == successor.owner_token != token
    assert row["owner_pid"] == os.getpid()
    assert row["claim_generation"] == 2
    notes = store.notes_for(run_id)
    assert any(
        note.startswith("adopted from a controller that is provably gone")
        and f"pid={dead.pid}" in note
        and "nothing had been dispatched" in note
        for note in notes
    ), notes


@windows_only
def test_a_live_owners_never_dispatched_run_is_not_adopted(
    store: Store, project_root: Path, fake_script: FakeScript, run_request: RunRequest
) -> None:
    """The same resubmission against a live owner (this process, lock held) changes nothing."""
    owner = _controller(store, project_root, fake_script)
    owner._hold_owner_lock()
    spec = run_request.task
    run_id = store.create_run(
        run_id="R-live-owner",
        project_id=run_request.project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="test-build",
        checks_digest=run_request.project.checks_digest(),
        turn_limit=spec.budget.max_agent_turns,
        repair_limit=0,
        controller_id=LABEL,
        owner_token=owner.owner_token,
        owner_identity=owner.owner_identity,
    )["run_id"]
    before = _snapshot(store, run_id)
    second = _controller(store, project_root, fake_script)

    outcome = second.run_task(run_request)

    assert outcome.run_id == run_id and outcome.task_state is TaskState.DRAFT
    assert any("may be alive" in note for note in outcome.notes), outcome.notes
    assert _outcome_exit_code(outcome) == EXIT_IN_PROGRESS
    assert second.driver.started == []  # type: ignore[attr-defined]
    assert _snapshot(store, run_id) == before
    assert not any(note.startswith("adopted") for note in store.notes_for(run_id))
    owner.close()


def test_adopt_run_refuses_a_run_with_anything_dispatched_or_a_stale_generation(
    store: Store, project_root: Path, run_request: RunRequest
) -> None:
    """The store's compare-and-set re-checks owner, generation, state and "nothing dispatched"."""
    token = new_owner_token()
    me = current_identity()
    run_id, _ = _seed_owned_dispatch(
        store,
        run_request=run_request,
        project_root=project_root,
        token=token,
        identity=me,
        child_pid=None,
        launched=False,
    )
    # Put the run back to READY so only the "attempt/invocation exists" guard can refuse it.
    with store.transaction() as conn:
        conn.execute("UPDATE runs SET task_state = 'READY' WHERE run_id = ?", (run_id,))
    before = _snapshot(store, run_id)
    successor = new_owner_token()
    for generation in (1, 0):
        assert store.adopt_run(
            run_id,
            expected_token=token,
            expected_generation=generation,
            controller_id=LABEL,
            token=successor,
            identity=me,
            note="adopted (test)",
        ) is None
    assert _snapshot(store, run_id) == before
    assert "adopted (test)" not in store.notes_for(run_id)


@windows_only
@pytest.mark.parametrize("owner", ["unclaimed", "dead"])
def test_an_allowance_refusal_leaves_the_existing_run_as_it_was(
    store: Store,
    project_root: Path,
    fake_script: FakeScript,
    run_request: RunRequest,
    owner: str,
) -> None:
    """The read-only allowance check runs before any claim or adoption (H12).

    Its refusal advises a new authorization for the same TaskSpec; that advice only works if the
    refused process left no claim behind for the next process to trip over.
    """
    if owner == "unclaimed":
        run_id = _create_run(store, run_request)
    else:
        run_id, _, _ = _owned_by_dead(store, run_request)
    before = _snapshot(store, run_id)
    refused = _controller(store, project_root, fake_script)

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RefusedError(RefusalCode.BUDGET_EXHAUSTED, "forced: obtain a new authorization")

    refused._assert_allowance_for = refuse  # type: ignore[method-assign]
    with pytest.raises(RefusedError):
        refused.run_task(run_request)
    assert _snapshot(store, run_id) == before
    assert not any(note.startswith("adopted") for note in store.notes_for(run_id))

    successor = _controller(store, project_root, fake_script)
    outcome = successor.run_task(run_request)
    assert outcome.run_id == run_id
    assert outcome.task_state is TaskState.ACCEPTED, (outcome.block_code, outcome.block_reason)
    assert store.get_run(run_id)["owner_token"] == successor.owner_token
