"""Windows Job Object ownership for one invocation's process boundary.

**Net-benefit note (required before adding a mechanism).** The controller needs a
*process boundary* it can close, not a machine-wide process hunt. The OS already provides
it: a Job Object groups processes, enforces limits, and is reaped by the kernel. Without
it, stopping a tree means enumerating descendants by parent PID, which is racy (PID reuse,
re-parenting) and cannot cover grandchildren reliably. With it:

* the boundary exists **before the target runs** - the process is created suspended, the
  boundary is a property of the process from its first instruction, and only then is it
  resumed, so there is no "start it then try to catch up" window;
* `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` means the controller crashing or being killed also
  tears the group down, because the last handle goes with the process;
* the handle is created non-inheritable, so we do not hold the group open by accident and
  nothing else can close it on our behalf.

What it is **not**: a filesystem sandbox, a proof against a deliberately escaping
descendant, or a statement that remote billing stopped. Those stay out of scope.

This module is intentionally small and uses only ``ctypes`` (standard library) to call the
documented Win32 API; there is no general-purpose process-management framework here.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

# -- Win32 constants used by this module ------------------------------------
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class JobBoundaryError(RuntimeError):
    """The process boundary could not be established. Callers must not ignore this."""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", wintypes.LARGE_INTEGER),
        ("TotalKernelTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _kernel32() -> object:
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


class ProcessBoundary:
    """A group of processes that can be observed and torn down as one unit.

    On non-Windows platforms this degrades to "the direct child process only" and says so
    through :attr:`kind`, rather than pretending the same guarantee exists.
    """

    def __init__(self) -> None:
        self.kind = "none"
        self.handle: int | None = None
        self._api = None

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> ProcessBoundary:
        """Create the boundary. Must be called before spawning anything into it."""
        if not IS_WINDOWS:
            self.kind = "direct_child_only"
            return self
        api = _kernel32()
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        # lpJobAttributes=None => handle is NOT inheritable.
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise JobBoundaryError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")  # type: ignore[attr-defined]
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = api.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            api.CloseHandle(handle)
            raise JobBoundaryError(f"SetInformationJobObject failed: {error}")
        self.handle = int(handle)
        self._api = api
        self.kind = "windows_job_kill_on_close"
        return self

    def close(self) -> None:
        """Release the boundary. With KILL_ON_JOB_CLOSE this also reaps the group."""
        if self.handle and self._api is not None:
            self._api.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self) -> ProcessBoundary:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- membership ----------------------------------------------------------

    def assign(self, pid: int) -> None:
        """Put an already-created (suspended) process inside the boundary."""
        if self.handle is None:
            return  # non-Windows: nothing to assign
        api = self._api
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        rights = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        process = api.OpenProcess(rights, False, pid)
        if not process:
            raise JobBoundaryError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")  # type: ignore[attr-defined]
        try:
            api.AssignProcessToJobObject.restype = wintypes.BOOL
            api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            if not api.AssignProcessToJobObject(self.handle, process):
                raise JobBoundaryError(
                    f"AssignProcessToJobObject({pid}) failed: {ctypes.get_last_error()}"  # type: ignore[attr-defined]
                )
        finally:
            api.CloseHandle(process)

    # -- observation and teardown -------------------------------------------

    def active_processes(self) -> int | None:
        """How many processes are inside the boundary right now (``None`` if unknown)."""
        if self.handle is None or self._api is None:
            return None
        info = _BasicAccountingInformation()
        ok = self._api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        )
        return int(info.ActiveProcesses) if ok else None

    def contains(self, pid: int) -> bool | None:
        """Is this exact process inside *this* boundary?

        ``IsProcessInJob`` with a concrete job handle answers that question. Passing NULL
        would only answer "is it in *some* job", which is not the same claim, so the handle
        is always supplied here. Returns ``None`` on platforms without job objects or when
        the query cannot be answered.
        """
        if self.handle is None or self._api is None:
            return None
        api = self._api
        api.OpenProcess.restype = wintypes.HANDLE
        process = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            result = wintypes.BOOL()
            api.IsProcessInJob.restype = wintypes.BOOL
            api.IsProcessInJob.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            ]
            if not api.IsProcessInJob(process, self.handle, ctypes.byref(result)):
                return None
            return bool(result.value)
        finally:
            api.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> bool:
        """Ask the kernel to kill every process in the boundary."""
        if self.handle is None or self._api is None:
            return False
        return bool(self._api.TerminateJobObject(self.handle, exit_code))

    def wait_empty(self, timeout_seconds: float) -> bool:
        """Poll until the boundary reports no active process, or the deadline passes."""
        deadline = timeout_seconds
        step = 0.05
        waited = 0.0
        while waited <= deadline:
            active = self.active_processes()
            if active == 0:
                return True
            if active is None:
                return False
            import time as _time

            _time.sleep(step)
            waited += step
        return self.active_processes() == 0


def suspending_flags() -> int:
    """Creation flags for a boundary-owned child: suspended + own console group."""
    if not IS_WINDOWS:
        return 0
    return CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP


def resume(pid: int) -> None:
    """Resume a process created with CREATE_SUSPENDED.

    We do not hold the primary thread handle (``Popen`` does not expose it), so this uses
    the documented thread snapshot path. That is deliberate: it keeps the boundary logic in
    the standard library, and the window it closes - "boundary assigned before the target
    runs" - is already closed by CREATE_SUSPENDED itself.
    """
    if not IS_WINDOWS:
        return
    api = _kernel32()
    TH32CS_SNAPTHREAD = 0x00000004

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    snapshot = api.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise JobBoundaryError("CreateToolhelp32Snapshot failed for resume")
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(_ThreadEntry32)
    resumed = False
    try:
        if api.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == pid:
                    thread = api.OpenThread(0x0002, False, entry.th32ThreadID)  # SUSPEND_RESUME
                    if thread:
                        api.ResumeThread(thread)
                        api.CloseHandle(thread)
                        resumed = True
                        break
                if not api.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        api.CloseHandle(snapshot)
    if not resumed:
        raise JobBoundaryError(f"could not resume suspended process {pid}")


def popen_in_boundary(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    boundary: ProcessBoundary,
    stdout_handle: Any,
    stderr_handle: Any,
) -> subprocess.Popen:
    """Spawn a process *inside* the boundary with no unprotected start window.

    Order matters and is the whole point: create the boundary, create the process
    suspended, assign it to the boundary, then resume. A process that never got resumed
    still cannot outlive the boundary.

    The caller passes already-open output files rather than paths: one handle shared by the
    child (for writing) and by the parent's reader, so no second open can hide the child's
    writes behind a stale file offset.
    """
    flags = suspending_flags()
    try:
        child = subprocess.Popen(  # noqa: S603 - argv is constructed by the driver
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=flags,
        )
    except BaseException:
        raise
    child._hflow_stdout = stdout_handle  # type: ignore[attr-defined]
    child._hflow_stderr = stderr_handle  # type: ignore[attr-defined]
    try:
        boundary.assign(child.pid)
        resume(child.pid)
    except BaseException:
        # The boundary is the safety net: closing it kills the suspended child.
        boundary.terminate()
        child.kill()
        raise
    return child


def parent_pid(pid: int) -> int | None:
    """Parent process id, or ``None`` when it cannot be read.

    Used only to attribute a helper process to the managed boundary by walking up from the
    process the harness actually started. Process-scope evidence, not a security claim.
    """
    if not IS_WINDOWS:
        return None
    api = _kernel32()
    TH32CS_SNAPPROCESS = 0x00000002

    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.CHAR * 260),
        ]

    snapshot = api.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return None
    entry = _ProcessEntry32()
    entry.dwSize = ctypes.sizeof(_ProcessEntry32)
    try:
        if not api.Process32First(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            if not api.Process32Next(snapshot, ctypes.byref(entry)):
                return None
    finally:
        api.CloseHandle(snapshot)


def process_gone(pid: int, wait_seconds: float = 0.0) -> bool:
    """Has this process exited? Best effort, and never the only evidence we record.

    ``OpenProcess`` succeeding does **not** mean the process is alive: a handle held
    elsewhere (``Popen`` keeps one) keeps the object openable after exit. The reliable
    question is whether the process object is signalled, which is what this asks.
    """
    if pid <= 0:
        return True
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False
    api = _kernel32()
    api.OpenProcess.restype = wintypes.HANDLE
    handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return True  # cannot open it at all => it does not exist
    try:
        result = api.WaitForSingleObject(handle, int(wait_seconds * 1000))
        return result == WAIT_OBJECT_0
    finally:
        api.CloseHandle(handle)


def platform_summary() -> dict[str, object]:
    return {
        "job_objects_supported": IS_WINDOWS,
        "python": sys.version.split()[0],
        "create_suspended": bool(IS_WINDOWS),
    }
