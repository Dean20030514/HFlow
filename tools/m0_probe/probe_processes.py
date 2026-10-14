"""Process facts only the M0 probe scripts read: a parent pid and a platform summary.

Kept beside the probes rather than in ``hflow.drivers.winjob``: no production path reads them.
Uses the driver's kernel32 loader, so there is still one place that binds the Win32 API.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from hflow.drivers.winjob import IS_WINDOWS, _kernel32


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


def platform_summary() -> dict[str, object]:
    return {
        "job_objects_supported": IS_WINDOWS,
        "python": sys.version.split()[0],
        "create_suspended": bool(IS_WINDOWS),
    }
