"""Which Windows answers prove a process gone - offline and model-free.

``process_gone`` is asked with kernel32 replaced by a stand-in whose ``OpenProcess`` reports its
error through ``ctypes.set_last_error``, which ``ctypes.get_last_error`` reads back exactly as it
does for the real binding (``_kernel32`` loads it with ``use_last_error=True``). Two tests ask
the real kernel instead: about pid 4, the always-running System process, and about a pid that
names no process, which is the one real answer that proves the error code is read at all.
"""

from __future__ import annotations

import ctypes
import types

import pytest

from hflow.drivers import winjob

pytestmark = pytest.mark.skipif(
    not winjob.IS_WINDOWS, reason="process_gone's OpenProcess path exists only on Windows"
)

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
#: What ``WaitForSingleObject`` returns for ``WAIT_FAILED`` (0xFFFFFFFF) through the default
#: ``c_int`` restype. Observed on an invalid handle.
WAIT_FAILED_AS_READ = -1
#: Windows pids are multiples of four and far below this; ``OpenProcess`` on it was observed to
#: answer ``ERROR_INVALID_PARAMETER``.
UNUSED_PID = 0x7FFFFFF0


def kernel32_stand_in(
    *, open_error: int | None = None, wait_result: int = winjob.WAIT_TIMEOUT
) -> tuple[types.SimpleNamespace, list[str]]:
    """A kernel32 with only what ``process_gone`` calls, and a log of the calls it received.

    The functions are plain closures, not methods, so ``process_gone`` can still set
    ``.restype`` on them.
    """
    calls: list[str] = []

    def open_process(access: int, inherit: bool, pid: int) -> int | None:
        calls.append("OpenProcess")
        if open_error is not None:
            ctypes.set_last_error(open_error)  # type: ignore[attr-defined]
            return None
        return 4242

    def wait_for_single_object(handle: int, milliseconds: int) -> int:
        calls.append("WaitForSingleObject")
        return wait_result

    def close_handle(handle: int) -> int:
        calls.append("CloseHandle")
        return 1

    api = types.SimpleNamespace(
        OpenProcess=open_process,
        WaitForSingleObject=wait_for_single_object,
        CloseHandle=close_handle,
    )
    return api, calls


def test_no_such_process_is_the_only_open_failure_that_means_gone(monkeypatch) -> None:
    api, calls = kernel32_stand_in(open_error=winjob.ERROR_INVALID_PARAMETER)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    assert winjob.process_gone(1234, 0.0) is True
    assert calls == ["OpenProcess"], "no handle was opened, so nothing is waited on or closed"


@pytest.mark.parametrize(
    "open_error",
    [ERROR_ACCESS_DENIED, ERROR_INVALID_HANDLE, 0],
    ids=["access_denied", "invalid_handle", "no_error_code"],
)
def test_an_open_failure_other_than_no_such_process_is_never_gone(
    monkeypatch, open_error: int
) -> None:
    api, _calls = kernel32_stand_in(open_error=open_error)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    assert winjob.process_gone(1234, 0.0) is None


@pytest.mark.parametrize(
    ("wait_result", "expected"),
    [(winjob.WAIT_OBJECT_0, True), (winjob.WAIT_TIMEOUT, False), (WAIT_FAILED_AS_READ, None)],
    ids=["signalled", "running", "wait_failed"],
)
def test_an_opened_process_answers_from_its_signalled_state(
    monkeypatch, wait_result: int, expected: bool | None
) -> None:
    api, calls = kernel32_stand_in(wait_result=wait_result)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    assert winjob.process_gone(1234, 0.25) is expected
    assert calls == ["OpenProcess", "WaitForSingleObject", "CloseHandle"], (
        "the handle is closed on every path"
    )


def test_the_system_process_is_never_reported_gone() -> None:
    """Real kernel. A non-elevated user is refused (``None``); an elevated one may open it."""
    assert winjob.process_gone(4, 0.0) is not True


def test_a_pid_that_names_no_process_is_gone_through_the_real_binding() -> None:
    """Real kernel: ``ERROR_INVALID_PARAMETER`` reaches ``get_last_error`` through ``_kernel32``.

    If the binding did not keep the last error (no ``use_last_error=True``), this would read 0
    and answer ``None``.
    """
    assert winjob.process_gone(UNUSED_PID, 0.0) is True


# --------------------------------------------------------------------------
# resume: a failure the kernel reports is a boundary error, never a silent success
# --------------------------------------------------------------------------


def _resume_stand_in(
    *, snapshot: int | None, resume_result: int, pid: int = 1234
) -> tuple[types.SimpleNamespace, list[str]]:
    """A kernel32 with what ``resume`` calls: one snapshot holding one thread of ``pid``."""
    calls: list[str] = []

    def create_snapshot(flags: int, owner: int) -> int | None:
        calls.append("CreateToolhelp32Snapshot")
        return snapshot

    def thread_first(handle: int, entry_ref: object) -> int:
        calls.append("Thread32First")
        entry = entry_ref._obj  # type: ignore[attr-defined] - ctypes.byref keeps its object
        entry.th32OwnerProcessID = pid
        entry.th32ThreadID = 77
        return 1

    def thread_next(handle: int, entry_ref: object) -> int:
        calls.append("Thread32Next")
        return 0

    def open_thread(access: int, inherit: bool, thread_id: int) -> int:
        calls.append("OpenThread")
        return 99

    def resume_thread(handle: int) -> int:
        calls.append("ResumeThread")
        return resume_result

    def close_handle(handle: int) -> int:
        calls.append(f"CloseHandle({handle})")
        return 1

    api = types.SimpleNamespace(
        CreateToolhelp32Snapshot=create_snapshot,
        Thread32First=thread_first,
        Thread32Next=thread_next,
        OpenThread=open_thread,
        ResumeThread=resume_thread,
        CloseHandle=close_handle,
    )
    return api, calls


def test_a_resume_the_kernel_refuses_raises_and_closes_both_handles(monkeypatch) -> None:
    api, calls = _resume_stand_in(snapshot=55, resume_result=winjob.RESUME_THREAD_FAILED)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    with pytest.raises(winjob.JobBoundaryError, match="ResumeThread failed"):
        winjob.resume(1234)
    assert "CloseHandle(99)" in calls and "CloseHandle(55)" in calls


def test_a_resumed_thread_returns_quietly(monkeypatch) -> None:
    api, calls = _resume_stand_in(snapshot=55, resume_result=1)  # the previous suspend count
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    winjob.resume(1234)
    assert calls.count("ResumeThread") == 1


@pytest.mark.parametrize("snapshot", [winjob.INVALID_HANDLE_VALUE, None], ids=["invalid", "null"])
def test_a_failed_thread_snapshot_raises(monkeypatch, snapshot: int | None) -> None:
    """``INVALID_HANDLE_VALUE`` read through a ``HANDLE`` restype is the unsigned all-ones value.

    Through the default ``c_int`` restype it read as ``-1`` and never matched, so a failed
    snapshot went on to be walked.
    """
    api, calls = _resume_stand_in(snapshot=snapshot, resume_result=1)
    monkeypatch.setattr(winjob, "_kernel32", lambda: api)

    with pytest.raises(winjob.JobBoundaryError, match="CreateToolhelp32Snapshot"):
        winjob.resume(1234)
    assert calls == ["CreateToolhelp32Snapshot"]


def test_the_kernel32_binding_is_loaded_once_with_every_signature_declared() -> None:
    from ctypes import wintypes

    api = winjob._kernel32()
    assert api is winjob._kernel32()
    for name, (restype, argtypes) in winjob._SIGNATURES.items():
        function = getattr(api, name)
        assert function.restype is restype, name
        assert list(function.argtypes) == argtypes, name
    assert api.CreateToolhelp32Snapshot.restype is wintypes.HANDLE
    assert api.ResumeThread.restype is wintypes.DWORD
