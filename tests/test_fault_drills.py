"""Real-process fault drills: a hard-killed ``hflow`` process, recovered by a fresh one.

Each drill runs a real CLI process that dies at one precise point and then recovers with new
``python -m hflow`` processes, asserting only what ``status --json`` / ``integrate show --json``
and Git report afterwards. The dying process is a small launcher written to ``tmp_path``: it
imports this tree's ``hflow``, rigs one internal function and then calls ``hflow.cli.main`` - the
least invasive seam, since the product has no test hook and gets none. Two ways to die:

* ``exit`` - the rigged function calls ``os._exit(9)``: no ``finally``, no handler, no flush;
* ``kill`` - it writes a marker and blocks; the test kills the process from outside
  (``TerminateProcess`` on Windows, ``SIGKILL`` elsewhere) once the marker exists.

Drills (contract: ``docs/batch-i-integration-plan.md`` section 2.3, and "Recovering from a
controller crash" in ``docs/operations.md``):

a. ``integrate apply`` dies after its ``applying`` intent is committed and before
   ``git update-ref``: ``reconcile`` finds the target still at ``T`` and returns the record to
   ``ready``; the following ``apply`` moves the branch once;
b. ``integrate apply`` dies right after ``update-ref`` moved the branch and before the receipt is
   written: ``reconcile`` observes the moved ref and records ``integrated``
   (``observed_after_interruption``) without running the update again;
c. ``run`` dies right after the fake driver reported its launch (implementer or reviewer):
   ``resume`` takes the run over from the dead owner, blocks it ``owner_lost`` and never
   re-dispatches; ``ledger settle`` then closes the ``unknown`` entry by attestation.

No drill re-runs a check or an update. The owner probe is the real one: on Windows it reads the
dead process ``gone``; elsewhere this build reads no process creation time, so it answers
``unknown`` - integrate reconcile then needs ``--owner-gone --attest`` and ``resume`` refuses to
take a run over (both documented), which the POSIX branches pin.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hflow.ownership import winjob

from .test_e2e_cli_loop import (  # noqa: F401 - cli_project registers the shared fixture
    SUBPROCESS_TIMEOUT,
    CliProject,
    cli_project,
    git,
    hflow_env,
)

IS_WINDOWS = winjob.IS_WINDOWS
DRILL_EXIT_CODE = 9
ATTEST = "drill: the test killed the applying process and waited for it to exit"
SETTLE_ATTEST = "drill: the controller was killed by the test; the fake driver reaches no model"

LAUNCHER = '''"""Fault-drill launcher: ``hflow.cli.main``, one internal function rigged to die."""
import os
import sys
import time
from pathlib import Path

POINT = os.environ["HFLOW_DRILL_POINT"]
MODE = os.environ["HFLOW_DRILL_MODE"]
MARKER = Path(os.environ["HFLOW_DRILL_MARKER"])


def die():
    MARKER.write_text(str(os.getpid()), encoding="utf-8")
    if MODE == "exit":
        os._exit(%(code)d)
    while True:  # the test kills this process from outside once the marker exists
        time.sleep(1)


if POINT in ("before-update-ref", "after-update-ref"):
    from hflow.gitworkspace import GitRepo

    real_update = GitRepo.update_ref_cas

    def update_ref_cas(self, *args, **kwargs):
        if POINT == "before-update-ref":
            die()
        real_update(self, *args, **kwargs)
        die()

    GitRepo.update_ref_cas = update_ref_cas
elif POINT in ("implementer-launched", "reviewer-launched"):
    from hflow.drivers.fake import FakeDriver

    ROLE = POINT.split("-")[0]
    real_report = FakeDriver._report_spawn

    def _report_spawn(self, request, *, created, pid=None, detail=""):
        real_report(self, request, created=created, pid=pid, detail=detail)
        if created and request.role == ROLE:
            die()

    FakeDriver._report_spawn = _report_spawn
else:
    raise SystemExit("unknown drill point %%r" %% POINT)

from hflow.cli import main

sys.exit(main())
''' % {"code": DRILL_EXIT_CODE}


def _spawn(
    project: CliProject, point: str, mode: str, args: tuple[str, ...], out, err  # noqa: ANN001
) -> tuple[subprocess.Popen, Path]:
    launcher = project.tmp_path / "drill_launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    marker = project.tmp_path / f"drill-{point}-{mode}.marker"
    argv = [sys.executable, str(launcher), *args, "--data-dir", str(project.data_dir)]
    env = hflow_env(
        {"HFLOW_DRILL_POINT": point, "HFLOW_DRILL_MODE": mode, "HFLOW_DRILL_MARKER": str(marker)}
    )
    process = subprocess.Popen(  # noqa: S603 - the interpreter running this suite
        argv, cwd=str(project.tmp_path), stdout=out, stderr=err, env=env
    )
    return process, marker


def _wait_for_marker(process: subprocess.Popen, marker: Path) -> None:
    deadline = time.monotonic() + SUBPROCESS_TIMEOUT
    while not marker.exists():
        assert process.poll() is None, "the process ended before the drill point"
        assert time.monotonic() < deadline, "the drill point was never reached"
        time.sleep(0.05)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=60)


def start_until(project: CliProject, point: str, *args: str) -> subprocess.Popen:
    """Start ``hflow <args>`` rigged to block at ``point``; return it, alive, once it is there."""
    process, marker = _spawn(project, point, "kill", args, subprocess.DEVNULL, subprocess.DEVNULL)
    try:
        _wait_for_marker(process, marker)
    except BaseException:
        _stop(process)
        raise
    return process


def die_at(project: CliProject, point: str, mode: str, *args: str) -> None:
    """Run ``hflow <args>`` in a process that dies at ``point``; return once it is gone."""
    out_path = project.tmp_path / f"drill-{point}.out"
    err_path = project.tmp_path / f"drill-{point}.err"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        process, marker = _spawn(project, point, mode, args, out, err)
        try:
            if mode == "exit":
                code = process.wait(timeout=SUBPROCESS_TIMEOUT)
            else:
                _wait_for_marker(process, marker)
                process.kill()  # TerminateProcess on Windows, SIGKILL elsewhere
                code = process.wait(timeout=60)
        finally:
            _stop(process)
    detail = (
        f"exit {code}\nstdout: {out_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        f"\nstderr: {err_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
    )
    assert marker.exists(), f"the process died somewhere other than {point}: {detail}"
    if mode == "exit":
        assert code == DRILL_EXIT_CODE, detail
    else:
        assert code != 0, detail


def show(project: CliProject, integration_id: str) -> dict:
    call = project.hflow("integrate", "show", integration_id, "--json")
    assert call.returncode == 0, call
    return call.json()


def hflow_reflog(project: CliProject, branch: str = "main") -> list[str]:
    """The branch's reflog entries written by an integration's ``update-ref``."""
    subjects = git(project.repo, "reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    return [line for line in subjects.splitlines() if line.startswith("hflow integrate ")]


def reconcile_after_death(project: CliProject, integration_id: str) -> dict:
    """``integrate reconcile`` in a fresh process, with the real owner probe."""
    call = project.hflow("integrate", "reconcile", integration_id, "--json")
    if not IS_WINDOWS:
        # No creation time is recorded off Windows, so the dead owner reads unknown: refused
        # with nothing written until the operator attests it has exited.
        assert call.returncode == 5, call
        assert "--owner-gone" in call.stderr, call
        call = project.hflow(
            "integrate", "reconcile", integration_id, "--owner-gone", "--attest", ATTEST, "--json"
        )
    assert call.returncode == 0, call
    return call.json()


@pytest.fixture()
def accepted(cli_project: CliProject) -> tuple[CliProject, str, dict]:  # noqa: F811
    """An ACCEPTED run and a ready integration of it into ``main`` (not checked out)."""
    run_id = cli_project.accept()["run_id"]
    prepared = cli_project.integrate_prepare(run_id)
    return cli_project, run_id, prepared["integration"]


# --------------------------------------------------------------------------
# a. dead after the intent, before update-ref
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["exit", "kill"])
def test_an_apply_killed_before_update_ref_reconciles_to_ready_and_applies_once(
    accepted: tuple[CliProject, str, dict], mode: str
) -> None:
    project, run_id, integration = accepted
    integration_id = integration["integration_id"]
    evidence_before = project.integration_evidence(run_id)
    user_before = project.user_state()

    die_at(
        project, "before-update-ref", mode,
        "integrate", "apply", integration_id, "--expect-target", project.base,
    )

    record = show(project, integration_id)["integration"]
    assert record["state"] == "applying", "the intent was committed before the ref update"
    assert record["apply_intent_at"]
    assert project.tip() == project.base, "the branch never moved"
    assert hflow_reflog(project) == []

    settled = reconcile_after_death(project, integration_id)
    assert settled["integration"]["state"] == "ready"
    assert settled["receipt"] is None
    assert settled["handoff"] == [
        project.printed("integrate", "apply", integration_id, "--expect-target", project.base)
    ]
    # Settled from what Git shows: no check ran again and nothing was written to the branch.
    assert project.integration_evidence(run_id) == evidence_before
    assert project.tip() == project.base
    assert hflow_reflog(project) == []

    applied = project.hflow(
        "integrate", "apply", integration_id, "--expect-target", project.base, "--json"
    )
    assert applied.returncode == 0, applied
    assert applied.json()["integration"]["basis"] == "hflow_ref_update"
    assert project.tip() == integration["integration_commit"]
    assert len(hflow_reflog(project)) == 1, "the branch moved exactly once"
    assert project.integration_evidence(run_id) == evidence_before
    assert project.user_state() == user_before


def test_a_running_applier_is_never_reconciled_over(
    accepted: tuple[CliProject, str, dict],
) -> None:
    """While the applying process still runs, reconcile refuses and writes nothing (exit 5)."""
    project, _run_id, integration = accepted
    integration_id = integration["integration_id"]
    process = start_until(
        project, "before-update-ref",
        "integrate", "apply", integration_id, "--expect-target", project.base,
    )
    try:
        busy = project.hflow("integrate", "reconcile", integration_id)
        assert busy.returncode == 5, busy
        assert "nothing was written" in busy.stderr, busy
        if IS_WINDOWS:
            # A process HFlow sees running is never overridden, not even by an attestation.
            attested = project.hflow(
                "integrate", "reconcile", integration_id, "--owner-gone", "--attest", ATTEST
            )
            assert attested.returncode == 5, attested
            assert "is still running" in attested.stderr, attested
        assert show(project, integration_id)["integration"]["state"] == "applying"
    finally:
        process.kill()
        process.wait(timeout=60)
    assert project.tip() == project.base


# --------------------------------------------------------------------------
# b. dead right after update-ref, before the receipt
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["exit", "kill"])
def test_an_apply_killed_after_update_ref_reconciles_to_integrated_without_moving_again(
    accepted: tuple[CliProject, str, dict], mode: str
) -> None:
    project, run_id, integration = accepted
    integration_id = integration["integration_id"]
    commit = integration["integration_commit"]
    evidence_before = project.integration_evidence(run_id)

    die_at(
        project, "after-update-ref", mode,
        "integrate", "apply", integration_id, "--expect-target", project.base,
    )

    shown = show(project, integration_id)
    assert shown["integration"]["state"] == "applying", "the receipt was never written"
    assert shown["receipt"] is None
    assert project.tip() == commit, "update-ref had moved the branch"
    assert len(hflow_reflog(project)) == 1

    settled = reconcile_after_death(project, integration_id)
    assert settled["integration"]["state"] == "integrated"
    assert settled["integration"]["basis"] == "observed_after_interruption"
    receipt = settled["receipt"]
    assert receipt["delivery_state"] == "INTEGRATED"
    assert receipt["integration_commit"] == commit
    assert receipt["target_tip_before"] == project.base
    assert any("did not finish cleanly" in item for item in receipt["limitations"])
    # Observed, not repeated: one ref update in the reflog, the same evidence, the same tip.
    assert len(hflow_reflog(project)) == 1
    assert project.integration_evidence(run_id) == evidence_before
    assert project.tip() == commit

    again = project.hflow(
        "integrate", "apply", integration_id, "--expect-target", project.base, "--json"
    )
    assert again.returncode == 0, again
    assert again.json()["notes"] == ["already integrated; nothing was written"]
    assert len(hflow_reflog(project)) == 1
    status = project.status_json(run_id)
    assert [(item["state"], item["basis"]) for item in status["integrations"]] == [
        ("integrated", "observed_after_interruption")
    ]
    assert status["run"]["delivery_state"] == "LOCAL_CANDIDATE", "the run receipt is unchanged"


# --------------------------------------------------------------------------
# c. a run killed mid-dispatch
# --------------------------------------------------------------------------


def _ledger(status: dict) -> list[tuple[str, str, str]]:
    return [
        (item["invocation_id"], item["role"], item["state"]) for item in status["invocations"]
    ]


def _wait_for_free_owner_lock(project: CliProject, run_id: str) -> dict:
    """The OS releases a dead process's file lock as part of its teardown; wait for that."""
    deadline = time.monotonic() + 30
    while True:
        status = project.status_json(run_id)
        if status["owner"]["lock"] != "held" or time.monotonic() > deadline:
            return status
        time.sleep(0.2)


@pytest.mark.parametrize(
    ("role", "mode"),
    # One way to die per role: both ways are drilled on the integration path above, and what
    # they leave behind - committed rows, a released lock, a dead identity - is the same.
    [("implementer", "exit"), ("reviewer", "kill")],
)
def test_a_run_killed_mid_dispatch_is_taken_over_never_redispatched_and_settled(
    cli_project: CliProject, role: str, mode: str  # noqa: F811 - pytest fixture
) -> None:
    project = cli_project
    project.authorize(project.prepare_task())

    die_at(project, f"{role}-launched", mode, *project.run_argv())

    run_id = _the_only_run(project)
    status = _wait_for_free_owner_lock(project, run_id)
    # Nothing was recorded at the time: the run is still live, as the killed owner left it (the
    # reviewer is dispatched in the checking phase).
    live_state = "RUNNING" if role == "implementer" else "CHECKING"
    assert status["run"]["task_state"] == live_state, status["run"]
    expected_roles = ["implementer"] if role == "implementer" else ["implementer", "reviewer"]
    ledger = _ledger(status)
    assert [entry[1] for entry in ledger] == expected_roles
    assert ledger[-1][2] == "started", "the launch was reported before the process died"
    attempts_before = len(status["attempts"])
    assert attempts_before == 1

    resumed = project.hflow("resume", run_id, "--project-root", str(project.repo), "--json")
    if not IS_WINDOWS:
        # Off Windows the dead owner reads unknown, so it is never proven gone: nothing changes.
        assert resumed.returncode == 5, resumed
        assert any("owner may be alive" in note for note in resumed.json()["notes"])
        after = project.status_json(run_id)
        assert after["run"]["task_state"] == live_state
        assert _ledger(after) == ledger
        assert len(after["attempts"]) == attempts_before
        return

    assert status["owner"]["liveness"] == "gone", status["owner"]
    assert resumed.returncode == 3, resumed
    outcome = resumed.json()
    assert outcome["task_state"] == "BLOCKED"
    assert outcome["block_code"] == "owner_lost"
    assert outcome["receipt"] is None
    assert any("nothing was re-dispatched" in note for note in outcome["notes"]), outcome
    after = project.status_json(run_id)
    # Never re-dispatched: the same attempt, the same invocations - the killed one now unknown.
    assert len(after["attempts"]) == attempts_before
    assert [entry[:2] for entry in _ledger(after)] == [entry[:2] for entry in ledger]
    killed = after["invocations"][-1]
    assert killed["state"] == "unknown", killed
    if role == "reviewer":
        assert after["invocations"][0]["state"] == "settled"
        assert after["receipt"] is None, "a review that was never observed accepts nothing"

    settle = project.hflow(
        "ledger", "settle", killed["invocation_id"], "--attest", SETTLE_ATTEST, "--json"
    )
    assert settle.returncode == 0, settle
    settlement = settle.json()["settlement"]
    assert settlement["settled_as"] == "consumed"
    assert settlement["prior_state"] == "unknown"

    # A second resume reconciles the blocked run and still dispatches nothing.
    again = project.hflow("resume", run_id, "--project-root", str(project.repo), "--json")
    assert again.returncode == 3, again
    final = project.status_json(run_id)
    assert final["run"]["task_state"] == "BLOCKED"
    assert final["run"]["block_code"] == "owner_lost"
    assert len(final["attempts"]) == attempts_before
    assert [entry[:2] for entry in _ledger(final)] == [entry[:2] for entry in ledger]
    assert final["invocations"][-1]["state"] == "operator_settled"
    assert [item["invocation_id"] for item in final["invocation_settlements"]] == [
        killed["invocation_id"]
    ]
    assert project.tip() == project.base, "a run never moves a branch"


def _the_only_run(project: CliProject) -> str:
    """The run id, read (read-only) from the ledger the killed process wrote.

    The killed ``run`` printed nothing and the CLI has no command that lists runs; every fact
    asserted about the run is then read through ``status --json``.
    """
    database = project.data_dir / "hflow.sqlite"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT run_id FROM runs").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, rows
    return str(rows[0][0])
