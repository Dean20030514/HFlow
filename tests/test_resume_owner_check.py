"""Batch J lane A: ``hflow resume`` waits for an owner that may be alive, and takes one attestation.

Two rules, pinned end to end offline:

* **An ``outcome_unknown`` block does not prove the owner stopped.** A cross-process
  ``hflow cancel`` holds no handle to the owner's agent, so it blocks the run ``outcome_unknown``
  while the owning controller and its child keep running. ``resume`` used to reconcile such a run
  at once - marking the in-flight entry ``unknown``, recording a reconcile and saying "reconciled
  an interrupted attempt" about work still going on. It now applies the owner half
  ``ledger settle`` applies (``ended_run_owner_blocker``) to a run whose owner this build recorded:
  while that owner may be alive (lock held, identity ``matching`` or ``unknown``) or is this very
  controller, it writes nothing and ``hflow resume`` exits ``5``; once the owner is provably gone
  it reconciles as before. A run with no owner token is reconciled as before (``cancel`` +
  ``resume`` is its documented way out), and ``owner_lost`` is untouched. Every other refusal on
  the owner rule - the closure of an ended run's open entries - also exits ``5``, never the run's
  own ``3`` or ``0``.
* **``resume --legacy-owner-gone --attest``** mirrors ``ledger settle``'s flag: a pre-v6 ended run
  whose attempts recorded a controller pid (no host, never probed) left entries open that nothing
  could close. On the operator's attestation ``resume`` closes them, recording the attestation
  verbatim as an attestation, never as an observation. It passes exactly that blocker: an owner
  this build recorded is judged by the takeover rule whatever an operator attests, and a flag that
  decided nothing says so and records nothing.

Offline only: ``FakeDriver`` subclasses, a real ``Store`` under ``tmp_path``, the CLI in-process.
A dead owner is simulated as in ``tests/owner_exit.py``.
"""

from __future__ import annotations

import getpass
import json
import sqlite3
from pathlib import Path

import pytest

from hflow.cli import EXIT_BLOCKED, EXIT_IN_PROGRESS, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main
from hflow.contracts import InvocationStartState, RefusalCode, RunRequest, TaskSpec, TaskState
from hflow.controller import (
    NOTE_DISPATCH,
    NOTE_OPERATOR_ATTESTATION,
    LegacyOwnerAttestation,
    inspect_run,
)
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.report import status_text
from hflow.store import ATTESTATION_MAX_CHARS, Store, StoreError

from .owner_exit import owner_exits, windows_only
from .test_batch_e_dispatch import FAKE_WRITE_PLAN, RunningTask
from .test_batch_e_ledger import _binding as _ledger_binding
from .test_batch_e_ledger import _register_authorization, _reserve, _seed_ready_run
from .test_batch_e_dispatch import _limits
from .test_open_entry_recovery import (
    ATTEST,
    HeldDriver,
    RaisingDriver,
    _frozen,
    _legacy_ended_run,
    _record_controller_pid,
    _root_run_controller,
    _states,
)
from .test_owner_lease import _controller

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _cli_resume(store: Store, project_root: Path, run_id: str, *extra: str) -> int:
    return main([
        "resume", run_id, "--project-root", str(project_root),
        "--data-dir", str(store.path.parent), *extra,
    ])


def _recorded(store: Store, run_id: str) -> dict[str, object]:
    """Everything a refused ``resume`` must leave alone - ``_frozen`` plus the ledger and notes."""
    return {
        **_frozen(store, run_id),
        "reconciles": [a["reconcile_json"] for a in store.attempts_for(run_id)],
        "entries": [entry.model_dump(mode="json") for entry in store.invocations_for(run_id)],
        "notes": store.notes_for(run_id),
    }


def _os_user() -> str:
    try:
        return getpass.getuser().strip() or "unknown"
    except Exception:  # noqa: BLE001 - the CLI's own fallback
        return "unknown"


# --------------------------------------------------------------------------
# 1. outcome_unknown: resume waits for an owner that may be alive
# --------------------------------------------------------------------------


@windows_only  # asserts what the real probe reads of the live owner (identity=matching)
def test_resume_after_a_cross_process_cancel_waits_for_the_owner_then_reconciles(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audited hazard: cancel from another process, then resume while the agent still runs.

    The owner's implementer is held inside its driver with a launch reported. ``hflow cancel``
    (another process: no handle) blocks the run ``outcome_unknown``; ``hflow resume`` must then
    write nothing and exit 5 - while the agent runs, after the owner's late result, and after the
    owner merely released its lock (its identity still reads ``matching``). Only once the owner is
    provably gone does ``resume`` reconcile: the entry becomes ``unknown`` and nothing re-dispatches.
    """
    driver = HeldDriver(project_root, report_launch=True)
    owner, root_id = _root_run_controller(store, task_spec, project_root, tmp_path, driver)

    with RunningTask(owner, run_request) as running:
        assert driver.entered.wait(timeout=30), "the implementer never entered its driver"
        run_id = str(store.list_runs()[0]["run_id"])
        capsys.readouterr()
        assert main(["cancel", run_id, "--json", "--data-dir", str(store.path.parent)]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["status"] == "unknown"
        row = store.get_run(run_id)
        assert (row["task_state"], row["block_code"]) == (
            TaskState.BLOCKED.value, RefusalCode.OUTCOME_UNKNOWN.value,
        ), row["block_reason"]
        (entry,) = store.invocations_for(run_id)
        assert entry.state is InvocationStartState.STARTED, "the agent is still running"
        before = _recorded(store, run_id)

        code = _cli_resume(store, project_root, run_id)
        out = capsys.readouterr().out

        assert code == EXIT_IN_PROGRESS, out
        assert "is blocked outcome_unknown and was not reconciled" in out, out
        assert "the run's owner may still be alive" in out and "lock=held" in out, out
        assert f"then run `hflow resume {run_id}` again" in out, out
        assert "reconciled an interrupted attempt" not in out
        assert _recorded(store, run_id) == before, "a refused resume writes nothing at all"
        assert driver.reconciled == [] and driver.cancelled == []
        driver.released.set()
    assert running.error is None, f"the run raised: {running.error!r}"

    # The late result is only a note; the owner is still alive (its controller object lives).
    assert any(n.startswith("late_result") for n in store.notes_for(run_id))
    assert store.invocation(entry.invocation_id).state is InvocationStartState.STARTED
    before = _recorded(store, run_id)
    assert _cli_resume(store, project_root, run_id) == EXIT_IN_PROGRESS
    assert _recorded(store, run_id) == before

    # A released lock alone is not death: this process still runs under the recorded identity.
    owner.close()
    capsys.readouterr()
    assert _cli_resume(store, project_root, run_id) == EXIT_IN_PROGRESS
    assert "identity=matching" in capsys.readouterr().out
    assert _recorded(store, run_id) == before

    owner_exits(store, run_id, owner, monkeypatch)
    started = len(driver.started)
    code = _cli_resume(store, project_root, run_id, "--json")
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_BLOCKED, payload
    (said,) = [n for n in payload["notes"] if "reconciled an interrupted attempt" in n]
    assert "once its owner was proven gone (lock" in said and "identity gone)" in said, said
    reconciled = store.invocation(entry.invocation_id)
    assert reconciled is not None and reconciled.state is InvocationStartState.UNKNOWN
    assert store.open_attempt(run_id)["reconcile_json"] is not None
    assert len(driver.started) == started, "resume never re-dispatches"
    assert [e.invocation_id for e in store.pending_invocations(root_id)] == [entry.invocation_id]
    assert store.get_run(run_id)["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value


def test_the_owners_own_resume_of_an_outcome_unknown_run_writes_nothing(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path,
) -> None:
    """This controller owns the run and is still running: its own ``resume`` refuses, like the
    closure of an ended run's entries does. No probe is needed to know that."""
    driver = FakeDriver(
        project_root, FakeScript(write_plan=FAKE_WRITE_PLAN, unknown_invocations=1)
    )
    owner, _root = _root_run_controller(store, task_spec, project_root, tmp_path, driver)
    outcome = owner.run_task(run_request)
    run_id = outcome.run_id
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    before = _recorded(store, run_id)

    refused = owner.resume(run_id)

    assert refused.waiting_on_owner
    assert refused.block_code is RefusalCode.OUTCOME_UNKNOWN
    (note,) = refused.notes
    assert "this controller owns the run and is still running" in note, note
    assert f"Run `hflow resume {run_id}` from another process once this controller" in note
    assert "Nothing was written" in note and "no reconcile was recorded" in note
    assert _recorded(store, run_id) == before
    assert driver.reconciled == [] and len(driver.started) == 1
    owner.close()


@pytest.mark.parametrize("legacy_pid", [False, True], ids=["no-pid", "pre-v6-pid"])
def test_an_outcome_unknown_run_with_no_owner_token_is_reconciled_as_before(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str], legacy_pid: bool,
) -> None:
    """No owner token: no owner can be proven gone, and ``cancel`` + ``resume`` is the documented
    way out for such a run - so it is reconciled without an owner check, exit 3."""
    run_id = _legacy_ended_run(store, project, task_spec, project_root, RefusalCode.OUTCOME_UNKNOWN)
    if legacy_pid:
        _record_controller_pid(store, run_id)

    code = _cli_resume(store, project_root, run_id)
    out = capsys.readouterr().out

    assert code == EXIT_BLOCKED, out
    assert "reconciled an interrupted attempt; this build does not re-dispatch" in out, out
    assert store.invocation("I-legacy").state is InvocationStartState.UNKNOWN


# --------------------------------------------------------------------------
# 2. the closure of an ended run's entries: a refusal is exit 5 too
# --------------------------------------------------------------------------


def test_a_closure_refused_because_the_owner_may_be_alive_exits_5_not_the_runs_3(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _root = _root_run_controller(
        store, task_spec, project_root, tmp_path, RaisingDriver(project_root)
    )
    run_id = owner.run_task(run_request).run_id
    assert store.get_run(run_id)["block_code"] == RefusalCode.INTERNAL_ERROR.value
    before = _recorded(store, run_id)
    capsys.readouterr()

    code = _cli_resume(store, project_root, run_id)
    out = capsys.readouterr().out

    assert code == EXIT_IN_PROGRESS, out
    assert "Not closed: the run's owner may still be alive" in out and "Nothing was written" in out
    assert _recorded(store, run_id) == before

    owner_exits(store, run_id, owner, monkeypatch)
    assert _cli_resume(store, project_root, run_id) == EXIT_BLOCKED, "closed: the run's own code"
    assert "resume closed the 1 ledger entry" in capsys.readouterr().out


def test_a_pre_v6_closure_refused_without_an_attestation_exits_5(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)
    before = _recorded(store, run_id)

    code = _cli_resume(store, project_root, run_id)
    out = capsys.readouterr().out

    assert code == EXIT_IN_PROGRESS, out
    assert "carries no host" in out and "--legacy-owner-gone --attest" in out, out
    assert _recorded(store, run_id) == before


# --------------------------------------------------------------------------
# 3. resume --legacy-owner-gone --attest
# --------------------------------------------------------------------------


def test_legacy_owner_gone_closes_a_pre_v6_runs_entries_and_records_the_attestation(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The attestation is what closes them, it is recorded verbatim, and it is never an
    observation. ``ledger settle`` still asks for its own ``--legacy-owner-gone`` afterwards."""
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)
    frozen = _frozen(store, run_id)
    # Longer than a default note: the operator's words are kept whole.
    statement = (ATTEST + " The pre-v6 controller's console window was closed by hand. ") * 12
    statement = statement[:1500]
    assert len(statement) == 1500

    code = _cli_resume(
        store, project_root, run_id, "--legacy-owner-gone", "--attest", statement, "--json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_BLOCKED, payload  # the run's own outcome: it ended internal_error
    assert _frozen(store, run_id) == frozen, "state, block, receipt, attempts and rows unchanged"
    entry = store.invocation("I-legacy")
    assert entry is not None and entry.state is InvocationStartState.UNKNOWN, entry
    assert "the operator attested that its pre-v6 controller has exited" in entry.detail
    assert f"attested_by {_os_user()}" in entry.detail
    assert "an attestation, not an observation" in entry.detail, entry.detail
    assert "proven gone" not in entry.detail
    (attested,) = [n for n in store.notes_for(run_id)
                   if n.startswith(f"{NOTE_OPERATOR_ATTESTATION}:")]
    assert attested.endswith(f"Their statement: {statement}"), "recorded verbatim, not cut"
    assert f"attested_by {_os_user()} (OS user; recorded, not authenticated)" in attested
    assert "an operator attestation, not an observation" in attested
    assert "HFlow did not and cannot check it" in attested
    (closure,) = [n for n in store.notes_for(run_id)
                  if n.startswith(f"{NOTE_DISPATCH}: resume closed")]
    assert "I-legacy started->unknown" in closure, closure
    assert "`hflow ledger settle <invocation_id> --legacy-owner-gone`" in closure, closure
    assert attested in payload["notes"] and closure in payload["notes"]
    assert not any("was not used" in n for n in payload["notes"])
    assert "open entries" not in status_text(inspect_run(store, run_id))

    # A second resume finds nothing open and records nothing more.
    notes = store.notes_for(run_id)
    assert _cli_resume(
        store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST
    ) == EXIT_BLOCKED
    assert "was not used" in capsys.readouterr().out
    assert store.notes_for(run_id) == notes

    # Settling is its own attestation: without the flag it still refuses the pre-v6 owner.
    data = str(store.path.parent)
    settle = ["--data-dir", data, "ledger", "settle", "I-legacy", "--attest", ATTEST]
    assert main(settle) == EXIT_REFUSED
    assert "--legacy-owner-gone" in capsys.readouterr().err
    assert main([*settle, "--legacy-owner-gone"]) == EXIT_OK, capsys.readouterr().err
    assert store.invocation("I-legacy").state is InvocationStartState.OPERATOR_SETTLED


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        (["--legacy-owner-gone"], "--legacy-owner-gone needs --attest"),
        (["--attest", ATTEST], "--attest is read only with --legacy-owner-gone"),
        (["--legacy-owner-gone", "--attest", "   "], "the attestation is blank"),
        (
            ["--legacy-owner-gone", "--attest", "x" * (ATTESTATION_MAX_CHARS + 1)],
            f"the bound is {ATTESTATION_MAX_CHARS}",
        ),
        (["--legacy-owner-gone", "--attest", "a\x00b"], "NUL"),
    ],
    ids=["no-attest", "attest-alone", "blank", "oversized", "nul"],
)
def test_an_unusable_attestation_is_a_usage_error_and_nothing_is_read_or_written(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str], extra: list[str], said: str,
) -> None:
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)
    before = _recorded(store, run_id)

    assert _cli_resume(store, project_root, run_id, *extra) == EXIT_USAGE
    captured = capsys.readouterr()
    assert said in captured.err and "nothing was done" in captured.err, captured.err
    assert captured.out == ""
    assert _recorded(store, run_id) == before


def test_the_attestation_type_applies_the_settle_bounds() -> None:
    with pytest.raises(ValueError, match="blank"):
        LegacyOwnerAttestation(attested_by="operator", attestation=" ")
    with pytest.raises(ValueError, match="the bound is"):
        LegacyOwnerAttestation(attested_by="operator", attestation="x" * (ATTESTATION_MAX_CHARS + 1))
    assert LegacyOwnerAttestation(attested_by="operator", attestation=ATTEST).attestation == ATTEST


def test_legacy_owner_gone_never_passes_an_owner_this_build_recorded(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A live owner with a token is judged by the takeover rule, whatever the operator attests -
    for the closure of an ended run's entries and for the reconcile of an outcome_unknown run."""
    owner, _root = _root_run_controller(
        store, task_spec, project_root, tmp_path, RaisingDriver(project_root)
    )
    run_id = owner.run_task(run_request).run_id
    before = _recorded(store, run_id)

    code = _cli_resume(store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST)
    out = capsys.readouterr().out

    assert code == EXIT_IN_PROGRESS, out
    assert "the run's owner may still be alive" in out
    assert "--legacy-owner-gone was not used and nothing of it was recorded" in out, out
    assert _recorded(store, run_id) == before

    with store.transaction() as conn:  # seeded: the same run, as an unconfirmed stop leaves it
        conn.execute(
            "UPDATE runs SET block_code = ? WHERE run_id = ?",
            (RefusalCode.OUTCOME_UNKNOWN.value, run_id),
        )
    before = _recorded(store, run_id)
    code = _cli_resume(store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST)
    out = capsys.readouterr().out
    assert code == EXIT_IN_PROGRESS, out
    assert "was not reconciled" in out and "--legacy-owner-gone was not used" in out, out
    assert _recorded(store, run_id) == before
    owner.close()


def _live_label_only_run(store: Store, project, task_spec: TaskSpec, project_root: Path) -> str:
    """A RUNNING, label-only claimed run with a launched implementer: no token, work dispatched."""
    binding = _ledger_binding(
        store, project_id=project.project_id, repo_path=project_root, task_id=task_spec.task_id
    )
    limits = _limits()
    store.register_root_budget(binding, limits)
    _register_authorization(store, max_submissions=4)
    run_id = _seed_ready_run(store, run_id="R-live-legacy", project_id=project.project_id,
                             spec=task_spec)
    assert _reserve(store, run_id=run_id, invocation_id="I-live-legacy", binding=binding,
                    limits=limits).is_new
    assert store.get_run(run_id)["task_state"] == TaskState.RUNNING.value
    return run_id


@pytest.mark.parametrize(
    ("shape", "expected_code"),
    [
        ("ended-not-recorded", EXIT_BLOCKED),
        ("outcome-unknown-no-token", EXIT_BLOCKED),
        ("live-no-token", EXIT_IN_PROGRESS),
    ],
)
def test_legacy_owner_gone_that_decides_nothing_says_so_and_records_nothing(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str], shape: str, expected_code: int,
) -> None:
    """Given where it is not the reason anything happens: the command does what it does without
    it, says the flag was not used, and no attestation note is written."""
    if shape == "live-no-token":
        run_id = _live_label_only_run(store, project, task_spec, project_root)
    else:
        block = (
            RefusalCode.OUTCOME_UNKNOWN if shape == "outcome-unknown-no-token"
            else RefusalCode.INTERNAL_ERROR
        )
        run_id = _legacy_ended_run(store, project, task_spec, project_root, block)
    states = _states(store, run_id)

    code = _cli_resume(store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST)
    out = capsys.readouterr().out

    assert code == expected_code, out
    assert "--legacy-owner-gone was not used and nothing of it was recorded" in out, out
    assert not any(n.startswith(f"{NOTE_OPERATOR_ATTESTATION}:") for n in store.notes_for(run_id))
    assert not any(ATTEST in n for n in store.notes_for(run_id))
    if shape == "live-no-token":
        assert _states(store, run_id) == states, "a live run is not taken over on an attestation"
        assert "nothing was changed" in out
    else:
        assert set(_states(store, run_id).values()) == {InvocationStartState.UNKNOWN}
        assert "attested" not in store.invocation(next(iter(states))).detail


def test_a_successor_controller_object_gets_the_same_answer_as_the_cli(
    store: Store, project, task_spec: TaskSpec, project_root: Path, fake_script: FakeScript,
) -> None:
    """The controller API takes the attestation as an object; nothing about it is CLI-only."""
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)

    resumed = _controller(store, project_root, fake_script).resume(
        run_id,
        legacy_owner_gone=LegacyOwnerAttestation(attested_by="operator", attestation=ATTEST),
    )

    assert not resumed.waiting_on_owner
    assert store.invocation("I-legacy").state is InvocationStartState.UNKNOWN
    assert any(n.startswith(f"{NOTE_OPERATOR_ATTESTATION}:") and n.endswith(ATTEST)
               for n in resumed.notes), resumed.notes


# --------------------------------------------------------------------------
# 4. what a resume writes: the attestation with its closure, and no note on a refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [sqlite3.OperationalError("database is locked"), StoreError("injected note-write failure")],
    ids=["sqlite", "store"],
)
def test_a_failed_attestation_note_write_closes_no_entry_and_exits_5(
    store: Store, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    """The closure names the attestation, so it commits only together with the operator's words.

    A note write that fails after the entries were closed used to leave them closed "on an
    attestation" whose text was never recorded, and the CLI exited with a traceback. The closure
    and the attestation note are one transaction now: the failure rolls the closure back, the CLI
    says so on stderr and exits 5 (never 3), and the same command succeeds once the store does.
    """
    run_id = _legacy_ended_run(store, project, task_spec, project_root)
    _record_controller_pid(store, run_id)
    before = _recorded(store, run_id)
    real = Store._record_note_locked

    def failing(self, conn, run_id, note, *, limit=1000):  # noqa: ANN001, ANN202
        if note.startswith(f"{NOTE_OPERATOR_ATTESTATION}:"):
            raise failure
        return real(self, conn, run_id, note, limit=limit)

    monkeypatch.setattr(Store, "_record_note_locked", failing)
    code = _cli_resume(store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST)
    captured = capsys.readouterr()

    assert code == EXIT_IN_PROGRESS, captured
    assert "Traceback" not in captured.err and captured.out == ""
    assert "resume failed on a store error" in captured.err, captured.err
    assert str(failure) in captured.err and "rolled back whole" in captured.err
    assert _recorded(store, run_id) == before, "no entry closed, no note, nothing at all"
    assert store.invocation("I-legacy").state is InvocationStartState.STARTED

    monkeypatch.setattr(Store, "_record_note_locked", real)
    assert _cli_resume(
        store, project_root, run_id, "--legacy-owner-gone", "--attest", ATTEST
    ) == EXIT_BLOCKED
    assert store.invocation("I-legacy").state is InvocationStartState.UNKNOWN
    assert any(n.startswith(f"{NOTE_OPERATOR_ATTESTATION}:") and n.endswith(ATTEST)
               for n in store.notes_for(run_id))


@pytest.mark.parametrize("shape", ["closure", "reconcile"])
def test_a_refused_resume_of_a_run_whose_config_is_unreadable_writes_no_note(
    store: Store, task_spec: TaskSpec, project_root: Path, run_request: RunRequest,
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """"Nothing was written" holds even when the run's stored effective config is unreadable.

    The observer used to record its ``stored_config_unreadable`` note before ``resume`` decided
    anything, so a refusal on the owner rule that said "Nothing was written" had just written
    that note. It is recorded now only once the resume wrote something itself - and once.
    """
    owner, _root = _root_run_controller(
        store, task_spec, project_root, tmp_path, RaisingDriver(project_root)
    )
    run_id = owner.run_task(run_request).run_id
    if shape == "reconcile":
        with store.transaction() as conn:  # seeded: as an unconfirmed cross-process stop leaves it
            conn.execute(
                "UPDATE runs SET block_code = ? WHERE run_id = ?",
                (RefusalCode.OUTCOME_UNKNOWN.value, run_id),
            )
    unreadable = "effective_config: {not json"
    store.record_note(run_id, unreadable, limit=len(unreadable))
    before = _recorded(store, run_id)
    capsys.readouterr()

    code = _cli_resume(store, project_root, run_id)
    out = capsys.readouterr().out

    assert code == EXIT_IN_PROGRESS, out
    assert "Nothing was written" in out, out
    assert _recorded(store, run_id) == before, "the refusal wrote no note either"

    owner_exits(store, run_id, owner, monkeypatch)
    assert _cli_resume(store, project_root, run_id) == EXIT_BLOCKED
    marked = [n for n in store.notes_for(run_id) if n.startswith("stored_config_unreadable:")]
    assert len(marked) == 1, "recorded once the resume acted"
    (entry,) = store.invocations_for(run_id)
    assert entry.state is InvocationStartState.LAUNCH_UNKNOWN, "closed: no spawn was reported"

    notes = store.notes_for(run_id)
    capsys.readouterr()
    _cli_resume(store, project_root, run_id)
    assert store.notes_for(run_id) == notes, "a resume that wrote nothing adds no note"
