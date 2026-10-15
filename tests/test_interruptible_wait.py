"""A console Ctrl+C reaches the controller while it waits for a child (``wait_interruptibly``)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hflow.drivers import winjob
from hflow.drivers.winjob import WAIT_SLICE_SECONDS, wait_interruptibly


class _SlowChild:
    """Records each blocking wait; exits after ``exit_after`` seconds of wall-clock time."""

    def __init__(self, exit_after: float | None, code: int = 3) -> None:
        self.args = ["stub"]
        self.code = code
        self.started = time.monotonic()
        self.exit_after = exit_after
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        if self.exit_after is None:
            return None
        return self.code if time.monotonic() - self.started >= self.exit_after else None

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.poll() is not None:
            return self.code
        time.sleep(timeout or 0.0)
        if self.poll() is not None:
            return self.code
        raise subprocess.TimeoutExpired(self.args, timeout or 0.0)


def test_returns_the_exit_code_and_never_blocks_longer_than_a_slice() -> None:
    child = _SlowChild(exit_after=0.35)
    assert wait_interruptibly(child, 5.0) == 3  # type: ignore[arg-type]
    assert len(child.waits) >= 2
    assert all(t is not None and t <= WAIT_SLICE_SECONDS for t in child.waits)


def test_times_out_like_popen_wait() -> None:
    child = _SlowChild(exit_after=None)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        wait_interruptibly(child, 0.3)  # type: ignore[arg-type]
    assert time.monotonic() - started < 2.0
    assert all(t is not None and t <= WAIT_SLICE_SECONDS for t in child.waits)


def test_an_exit_at_the_deadline_is_not_a_timeout() -> None:
    child = _SlowChild(exit_after=0.0, code=0)
    assert wait_interruptibly(child, 0.0) == 0  # type: ignore[arg-type]


def test_no_timeout_waits_in_slices_until_exit() -> None:
    child = _SlowChild(exit_after=0.25)
    assert wait_interruptibly(child, None) == 3  # type: ignore[arg-type]
    assert all(t == WAIT_SLICE_SECONDS for t in child.waits)


_INNER = textwrap.dedent(
    """
    import ctypes, subprocess, sys, threading, time
    sys.path.insert(0, sys.argv[2])
    from hflow.drivers.winjob import wait_interruptibly
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler(None, False)  # this console may start with Ctrl+C ignored
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], creationflags=0x00000200
    )

    def fire():
        time.sleep(1.0)
        kernel32.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT to this console

    threading.Thread(target=fire, daemon=True).start()
    started = time.monotonic()
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        try:
            try:
                wait_interruptibly(child, 20.0)
                out.write("returned")
            except subprocess.TimeoutExpired:
                out.write("timeout")
        except KeyboardInterrupt:
            out.write(f"interrupted {time.monotonic() - started:.2f}")
        finally:
            child.kill()
    """
)


@pytest.mark.skipif(not winjob.IS_WINDOWS, reason="the blocking wait is a Windows behaviour")
def test_a_console_ctrl_c_interrupts_the_wait_promptly(tmp_path: Path) -> None:
    inner = tmp_path / "inner.py"
    inner.write_text(_INNER, encoding="utf-8")
    result = tmp_path / "result.txt"
    src = Path(__file__).resolve().parents[1] / "src"
    # CREATE_NO_WINDOW: the inner process gets a hidden console of its own, so the Ctrl+C it
    # sends never reaches this test runner's console.
    outer = subprocess.run(
        [sys.executable, "-I", str(inner), str(result), str(src)],
        creationflags=0x08000000,
        timeout=60,
        check=False,
    )
    text = result.read_text(encoding="utf-8") if result.exists() else f"no result ({outer.returncode})"
    assert text.startswith("interrupted"), text
    assert float(text.split()[1]) < 3.0, text


# --------------------------------------------------------------------------
# the driver: a Ctrl+C while collect waits stops the agent's tree at once
# --------------------------------------------------------------------------


@pytest.mark.skipif(not winjob.IS_WINDOWS, reason="needs a Windows Job Object")
def test_a_ctrl_c_while_collect_waits_stops_the_tree_at_once_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interrupt is re-raised for the controller to record; the tree does not outlive it.

    Without the teardown the stubborn agent and its helper would run until this process exited
    and the kill-on-close boundary went with it. No receipt, result or boundary record is written:
    an interrupted wait is not a confirmed stop.
    """
    from hflow.drivers import acpx_dsh
    from hflow.drivers.winjob import process_gone

    from .test_driver_acpx_dsh import DriverHarness

    harness = DriverHarness(tmp_path, "stubborn")
    handle, _ = harness.start()
    driver = harness.driver
    invocation_id = handle.invocation_id
    try:
        helper_pid = int(harness.wait_for_stub_marker("helper").read_text(encoding="utf-8"))
        assert process_gone(helper_pid, 0.5) is False, "the helper must be alive first"
        waited: list[float | None] = []

        def interrupted(child: subprocess.Popen, timeout: float | None) -> int:
            waited.append(timeout)
            raise KeyboardInterrupt

        monkeypatch.setattr(acpx_dsh, "wait_interruptibly", interrupted)
        with pytest.raises(KeyboardInterrupt):
            driver.collect(handle)
        monkeypatch.undo()

        assert waited, "collect must wait through the interruptible wait"
        assert process_gone(handle.pid, 5.0) is True, "the client must be gone"
        assert process_gone(helper_pid, 5.0) is True, "the managed descendant must be gone too"
        assert invocation_id not in driver._receipts
        assert invocation_id not in driver._results
        assert invocation_id not in driver._exit_boundaries
        assert driver._boundaries[invocation_id].handle is not None, "terminated, not closed"
    finally:
        monkeypatch.undo()
        driver.release(invocation_id)
