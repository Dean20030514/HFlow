"""Process lifecycle of a formal program check (T03, first slice).

``CommandCheckRunner`` must *own* the processes it launches, not merely start them. These
tests drive it against small local Python processes that really spawn children, so the
checks that matter are observations, not assertions about intentions:

* a timeout leaves the owned boundary empty;
* a direct child that exits 0 while its helper keeps running is not a pass;
* a runner that cannot establish its boundary runs nothing at all;
* a tear-down that cannot be confirmed is reported as unconfirmed.

Every fixture process here is a real child of the check process, and every test retires its
own processes in a ``finally`` block, so a failing assertion cannot leak a sleeper into the
rest of the run. No model, credential, network or HFlow run is involved.

The Windows Job Object is what makes whole-tree ownership real; on other platforms the
boundary covers the direct child only and the runner says so. The tests that need a real
tree are skipped there explicitly - never passed through a weaker substitute.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hflow.contracts import CheckDef, EvidenceStatus
from hflow.drivers import winjob
from hflow.verify import CommandCheckRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHECK_HELPER = FIXTURES / "check_helper.py"

#: A real Job Object is required to make claims about a whole process tree.
requires_job_object = pytest.mark.skipif(
    not winjob.IS_WINDOWS,
    reason=(
        "needs a Windows Job Object: on this platform ProcessBoundary.kind is "
        "'direct_child_only', so whole-tree termination cannot be observed and is not claimed"
    ),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _check(argv: list[str], *, check_id: str = "unit", timeout: int = 60) -> CheckDef:
    return CheckDef(id=check_id, kind="command", argv=argv, timeout_seconds=timeout)


def _helper_command(markers: Path, label: str, *extra: str) -> list[str]:
    """A check process whose marker directory is per-test and whose markers carry a label."""
    return [sys.executable, str(CHECK_HELPER), "--markers", str(markers), "--label", label, *extra]


def _wait_for(pattern: str, markers: Path, timeout: float = 20.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = sorted(markers.glob(pattern))
        if found:
            return found[0]
        time.sleep(0.02)
    raise AssertionError(f"fixture never produced {pattern} in {markers}")


def _helper_pid(markers: Path, label: str) -> int:
    """The helper's pid, as recorded by the fixture itself."""
    return int(_wait_for(f"{label}-*.helper", markers).read_text(encoding="utf-8").strip())


def _gone(pid: int, timeout: float = 10.0) -> bool:
    return winjob.process_gone(pid, timeout)


def _force_kill(pid: int) -> None:
    """Retire a process this test deliberately let escape its boundary."""
    subprocess.run(  # noqa: S603 - test-only cleanup of a pid this test started
        ["taskkill", "/PID", str(pid), "/F", "/T"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _retire(markers: Path, pids: list[int]) -> None:
    """Never leave a fixture process behind, whatever the assertions did."""
    (markers / "stop").write_text("stop", encoding="utf-8")
    for pid in pids:
        if pid > 0 and not _gone(pid, 0.5):
            _force_kill(pid)


# --------------------------------------------------------------------------
# 1-2: the ordinary outcomes still behave as before
# --------------------------------------------------------------------------


def test_a_successful_check_settles_and_passes(tmp_path: Path) -> None:
    outcome = CommandCheckRunner().run(
        _check([sys.executable, "-c", "print('ok')"]), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED
    assert outcome.exit_code == 0
    assert "elapsed=" in outcome.detail
    assert "boundary=" in outcome.detail
    assert outcome.command == [sys.executable, "-c", "print('ok')"]


def test_a_nonzero_check_settles_and_fails(tmp_path: Path) -> None:
    outcome = CommandCheckRunner().run(
        _check([sys.executable, "-c", "raise SystemExit(3)"]), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.FAILED
    assert outcome.exit_code == 3


def test_a_check_is_never_left_waiting_for_input(tmp_path: Path) -> None:
    """stdin is closed for the check, so a check that reads until EOF finishes on its own."""
    markers = tmp_path / "markers"
    check_id = "stdin-eof"
    outcome = CommandCheckRunner().run(
        _check([*_helper_command(markers, check_id, "--stdin")], timeout=30), tmp_path, 30
    )

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    # The fixture wrote EOF only after stdin actually reached end-of-file.
    assert list(markers.glob("*.eof")), "the check never saw EOF on stdin"


# --------------------------------------------------------------------------
# 3: a timeout must leave the owned boundary empty
# --------------------------------------------------------------------------


@requires_job_object
def test_a_timed_out_check_leaves_no_descendants_in_its_boundary(tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    label = "times-out"
    outcome = CommandCheckRunner().run(
        _check(
            [*_helper_command(markers, label, "--helper", "--wait-helper", "--sleep", "300")],
            check_id=label,
            timeout=3,
        ),
        tmp_path,
        3,
    )

    helper_pid = _helper_pid(markers, label)
    try:
        assert outcome.status is EvidenceStatus.ERROR
        assert "(timeout after 3s)" in outcome.detail
        assert "boundary=windows_job_kill_on_close" in outcome.detail
        assert "direct_child_gone=True" in outcome.detail
        assert _gone(helper_pid, 10.0), (
            "a timed-out check must not leave its helper running inside the owned boundary"
        )
    finally:
        _retire(markers, [helper_pid])


@requires_job_object
def test_an_unconfirmed_teardown_is_reported_as_unconfirmed(tmp_path: Path, monkeypatch) -> None:
    """A boundary that cannot be confirmed empty is never reported as a confirmed stop."""
    markers = tmp_path / "markers"
    check_id = "unconfirmable"
    real_open = winjob.ProcessBoundary.open
    pids: list[int] = []
    try:
        monkeypatch.setattr(
            winjob.ProcessBoundary,
            "open",
            lambda self: _blind_boundary(real_open(self)),
        )
        outcome = CommandCheckRunner().run(
            _check(
                [*_helper_command(markers, check_id, "--helper", "--wait-helper", "--sleep", "300")],
                check_id=check_id,
                timeout=3,
            ),
            tmp_path,
            3,
        )
        pids.append(_helper_pid(markers, check_id))

        assert outcome.status is EvidenceStatus.ERROR
        # The tear-down was requested and refused: the evidence says so instead of claiming a
        # confirmed whole-boundary stop.
        assert "boundary_terminated_by_this_runner=True" in outcome.detail
        assert "boundary_empty=False" in outcome.detail
    finally:
        _retire(markers, pids)


def _blind_boundary(boundary):
    """A boundary whose teardown always fails, to model an unconfirmable stop."""

    def never_empty(timeout_seconds: float) -> bool:
        return False

    def refuses_to_terminate(exit_code: int = 1) -> bool:
        return False

    boundary.wait_empty = never_empty  # type: ignore[method-assign]
    boundary.terminate = refuses_to_terminate  # type: ignore[method-assign]
    return boundary


# --------------------------------------------------------------------------
# 4: a parent's exit is not completion
# --------------------------------------------------------------------------


@requires_job_object
def test_a_parent_exit_does_not_hide_a_lingering_helper(tmp_path: Path) -> None:
    """Exit code 0 with helpers still running is a lifecycle error, not a pass."""
    markers = tmp_path / "markers"
    check_id = "leaves-a-helper"
    helper_pid = 0
    try:
        outcome = CommandCheckRunner().run(
            _check(
                [
                    *_helper_command(
                        markers, check_id, "--helper", "--wait-helper", "--linger-after-exit"
                    )
                ],
                check_id=check_id,
                timeout=60,
            ),
            tmp_path,
            60,
        )
        helper_pid = _helper_pid(markers, check_id)

        assert outcome.exit_code == 0, "the direct child itself exited successfully"
        assert outcome.status is EvidenceStatus.ERROR, (
            "a lingering descendant must not be rounded up to a clean pass"
        )
        assert "left processes running" in outcome.detail
        assert "descendants_alive_after_settle=" in outcome.detail
        assert _gone(helper_pid, 10.0), "the runner must settle the descendant it found"
    finally:
        _retire(markers, [helper_pid])


def test_a_normally_settled_check_reports_its_boundary_observation(tmp_path: Path) -> None:
    """The detail always states what was observed, so a reader can tell the cases apart."""
    markers = tmp_path / "markers"
    check_id = "settles"
    outcome = CommandCheckRunner().run(
        _check([*_helper_command(markers, check_id)], check_id=check_id, timeout=60), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED
    # The kind the runner actually established, not a fresh unopened boundary's placeholder.
    with winjob.ProcessBoundary() as boundary:
        established = boundary.kind
    assert f"boundary={established}" in outcome.detail
    assert "descendants=" not in outcome.detail, "this check left no descendant to report"


def test_the_non_windows_limitation_is_stated_rather_than_implied(tmp_path: Path, monkeypatch) -> None:
    """Where the boundary covers one process, the detail says so instead of implying a tree."""
    # The same code path a non-Windows platform takes: ``open()`` degrades and records the
    # kind without creating a job, so launching, waiting and settling all still run.
    monkeypatch.setattr(winjob, "IS_WINDOWS", False)
    outcome = CommandCheckRunner().run(
        _check([sys.executable, "-c", "print('ok')"]), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED
    assert "boundary=direct_child_only" in outcome.detail
    assert "boundary_ownership=single_process_only_on_this_platform" in outcome.detail
    assert "boundary_empty" not in outcome.detail, (
        "a boundary that covers one process must not claim a whole-tree observation"
    )


@requires_job_object
def test_an_unanswered_windows_boundary_query_is_not_a_clean_pass(tmp_path: Path) -> None:
    """``None`` means unknown, not "no job object": a failed query cannot pass a check.

    ``ProcessBoundary.active_processes()`` returns ``None`` both where no job exists and where
    the query itself failed on a real Job Object - verified against the shared helper, whose
    last line is ``int(info.ActiveProcesses) if ok else None``, and reproduced by querying a
    boundary whose handle was closed (``GetLastError`` = 6, invalid handle). Only the boundary's
    ``kind`` separates those two cases, so this test keeps a real Windows boundary - created,
    assigned and released by the runner - and fails the observation. The check itself is
    ordinary and exits 0, which is exactly why a pass here would be wrong.
    """
    markers = tmp_path / "markers"
    label = "unobservable"

    def observation(boundary: winjob.ProcessBoundary) -> int | None:
        assert boundary.kind == "windows_job_kill_on_close", boundary.kind
        return None

    started = time.monotonic()
    outcome = CommandCheckRunner(_observation_override=observation).run(
        _check(
            [*_helper_command(markers, label, "--stdin")], check_id=label, timeout=30
        ),
        tmp_path,
        30,
    )
    elapsed = time.monotonic() - started

    assert outcome.exit_code == 0, "the direct child itself exited successfully"
    assert outcome.status is EvidenceStatus.ERROR, (
        "an unanswered boundary observation must not become a clean pass"
    )
    assert "boundary_observation=unknown" in outcome.detail
    assert "settlement is unconfirmed" in outcome.detail
    assert "boundary_empty" not in outcome.detail, "no empty boundary was observed"
    assert "direct_child_only" not in outcome.detail, (
        "this ran on a real Job Object and must not be described as the weaker platform"
    )
    assert "left processes running" not in outcome.detail, (
        "no lingering process was observed, so the detail must not claim one was"
    )
    # Cleanup still ran, and this is a failed observation of a real run rather than a check that
    # never started: the fixture writes its EOF marker only after the check body read stdin.
    assert elapsed < 20.0, elapsed
    assert list(markers.glob(f"{label}-*.eof")), "the check never ran"


# --------------------------------------------------------------------------
# 5: no boundary means no execution
# --------------------------------------------------------------------------


def test_a_boundary_failure_cannot_fall_back_to_an_unmanaged_launch(
    tmp_path: Path, monkeypatch
) -> None:
    """If ownership cannot be established, the approved argv is not run at all."""
    markers = tmp_path / "markers"
    markers.mkdir()
    check_id = "must-not-run"

    def refuse(self):
        raise winjob.JobBoundaryError("no job object for this test")

    monkeypatch.setattr(winjob.ProcessBoundary, "open", refuse)
    outcome = CommandCheckRunner().run(
        _check([*_helper_command(markers, check_id)], check_id=check_id), tmp_path, 30
    )

    assert outcome.status is EvidenceStatus.ERROR
    assert "boundary could not be established" in outcome.detail
    assert outcome.exit_code is None
    assert list(markers.iterdir()) == [], "nothing may be executed without an owned boundary"


def test_a_launch_failure_inside_the_boundary_executes_nothing_unmanaged(
    tmp_path: Path, monkeypatch
) -> None:
    """Establishing the boundary is not enough: a failed assignment must not run the check."""
    markers = tmp_path / "markers"
    markers.mkdir()
    check_id = "assignment-fails"

    def refuse_assignment(self, pid: int) -> None:
        raise winjob.JobBoundaryError("AssignProcessToJobObject refused for this test")

    monkeypatch.setattr(winjob.ProcessBoundary, "assign", refuse_assignment)
    outcome = CommandCheckRunner().run(
        _check([*_helper_command(markers, check_id)], check_id=check_id), tmp_path, 30
    )

    assert outcome.status is EvidenceStatus.ERROR
    assert "could not be started" in outcome.detail
    assert outcome.exit_code is None
    # The check never got to run its body: the boundary is assigned before the child resumes.
    assert not list(markers.glob("*.helper"))


def test_a_check_that_cannot_start_is_an_error_not_a_pass(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-program-xyz"
    outcome = CommandCheckRunner().run(_check([str(missing)]), tmp_path, 30)

    assert outcome.status is EvidenceStatus.ERROR
    assert "could not be started" in outcome.detail
    assert outcome.exit_code is None


def test_no_scratch_directory_is_an_error_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    """Without somewhere to capture output there is no evidence, so the check is not run."""

    def refuse(*args: object, **kwargs: object) -> str:
        raise OSError("no temporary directory available")

    monkeypatch.setattr("hflow.verify.tempfile.mkdtemp", refuse)
    outcome = CommandCheckRunner().run(
        _check([sys.executable, "-c", "print('must not run')"]), tmp_path, 30
    )

    assert outcome.status is EvidenceStatus.ERROR
    assert "no writable scratch directory" in outcome.detail
    assert outcome.command == [sys.executable, "-c", "print('must not run')"]
