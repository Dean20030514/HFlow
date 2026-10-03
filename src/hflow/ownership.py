"""Who owns a run, and whether that owner is provably gone (user ruling, 2026-10-03).

A run is driven by exactly one controller *process*. Before this module the claim was a label
(``--controller-id``, ``local-controller`` by default), which every CLI process shares, so it could
not tell two processes apart and could not tell a live owner from a dead one. The ruling binds a
claim to a per-process owner instead:

* **identity** - pid, the process's creation time (``GetProcessTimes``, a FILETIME in 100 ns ticks
  since 1601, UTC) and the host. A pid alone is reused by unrelated processes; pid plus creation
  time names one process for as long as the machine runs;
* **token** - a random value per controller object (``secrets``). Exclusivity is the token; the
  label stays a human-readable name;
* **lock** - an exclusive, non-blocking OS file lock on ``<ledger dir>/owners/<token>.lock`` held
  for the controller's lifetime. The OS releases it when the process ends, however it ends.

A successor may take a run over only when the owner is *proven* dead: its lock can be taken and
its identity reads ``gone``. Every other answer - the lock is held, the identity reads
``matching``, or it reads ``unknown`` (access denied, another host, a query that failed, a platform
this build cannot ask) - means "the owner may be alive" and nothing is changed.

Traps this module avoids, from the 2026-10-03 survey:

* ``os.kill(pid, 0)`` is **never** used. On Windows CPython turns it into
  ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``: it is not a liveness probe there, it is a signal;
* a heartbeat or lease timeout alone is not a death proof (it declares a slow live owner dead), so
  there is no timeout anywhere here;
* ``OpenProcess`` succeeding does not mean the process runs - a handle held elsewhere keeps the
  object openable after exit - so the answer comes from ``WaitForSingleObject(h, 0)``.

Off Windows the identity probe answers ``unknown`` (documented, not observed): this build reads no
creation time there, so a takeover is never proven on such a host.
"""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .drivers import winjob
from .ids import parse_ts

#: ``not_recorded`` is not a liveness answer: the run carries no owner token and recorded no
#: controller process, so there is nothing to probe. It never counts as ``gone``.
Verdict = Literal["matching", "gone", "unknown", "not_recorded"]
LockState = Literal["held", "free", "absent", "not_applicable", "unknown"]

#: Win32 ``ERROR_ACCESS_DENIED``. Named only so tests and messages can say what they injected.
ERROR_ACCESS_DENIED = 5

#: How much later than a *recorded* launch time a process with the same pid may have been created
#: and still possibly be that process. A recorded ``process_started_at`` is second-precision and
#: written after the spawn, so the real creation is at most ~1 s after it; 2 s keeps a margin.
#: Anything created later cannot be the recorded process: the pid was reused after it exited.
RECORDED_START_SLACK_SECONDS = 2

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ProcessIdentity:
    """One process: pid, creation FILETIME (``None`` when it could not be read) and host."""

    pid: int
    created: int | None
    host: str

    def created_iso(self) -> str:
        return filetime_iso(self.created)


@dataclass(frozen=True)
class OwnerFence:
    """What a guarded write must still match: the writer's token and the generation it claimed.

    A takeover changes the token and increments the generation in one transaction, so every guarded
    write by the superseded owner (a "zombie" that was only presumed dead) fails afterwards.
    """

    token: str
    generation: int


@dataclass(frozen=True)
class Probe:
    """A liveness answer and the observation it rests on."""

    verdict: Verdict
    detail: str
    #: ``True`` only when the operating system was actually asked about the process; an answer
    #: decided by rule (another host, no pid, a pre-v6 record) is not an observation.
    probed: bool = True


@dataclass(frozen=True)
class OwnerAssessment:
    """Is a run's recorded owner provably gone? ``gone`` is the only answer that permits takeover."""

    gone: bool
    probe: Probe
    lock: LockState
    lock_path: str
    summary: str


def new_owner_token() -> str:
    return secrets.token_hex(16)


def valid_token(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


def current_host() -> str:
    return socket.gethostname() or "unknown-host"


def filetime_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return _FILETIME_EPOCH + timedelta(microseconds=value // 10)


def filetime_iso(value: int | None) -> str:
    moment = filetime_to_datetime(value)
    if moment is None:
        return "not recorded"
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


# -- Windows process queries -------------------------------------------------------------------


def _api() -> Any:
    from ctypes import wintypes

    api: Any = winjob._kernel32()  # noqa: SLF001 - same package, one kernel32 loader
    api.OpenProcess.restype = wintypes.HANDLE
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetCurrentProcess.argtypes = []
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.GetProcessTimes.restype = wintypes.BOOL
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.CloseHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def _open_process(pid: int) -> tuple[Any, int]:
    """``(handle, 0)`` or ``(None, win32_error)``. A seam: tests inject access denied here."""
    api = _api()
    handle = api.OpenProcess(
        winjob.PROCESS_QUERY_LIMITED_INFORMATION | winjob.SYNCHRONIZE, False, pid
    )
    if not handle:
        return None, int(ctypes.get_last_error())  # type: ignore[attr-defined]
    return handle, 0


def _close(handle: Any) -> None:
    try:
        _api().CloseHandle(handle)
    except OSError:  # pragma: no cover - closing a handle we own
        pass


def _creation_time(handle: Any) -> int | None:
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    ok = _api().GetProcessTimes(handle, *(ctypes.byref(item) for item in times))
    if not ok:
        return None
    created = times[0]
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def _exited(handle: Any) -> bool | None:
    """``True`` exited, ``False`` running, ``None`` the wait itself failed."""
    result = _api().WaitForSingleObject(handle, 0)
    if result == winjob.WAIT_OBJECT_0:
        return True
    if result == winjob.WAIT_TIMEOUT:
        return False
    return None


def current_identity() -> ProcessIdentity:
    """This process. ``created`` is ``None`` off Windows or when the query failed."""
    created: int | None = None
    if winjob.IS_WINDOWS:
        api = _api()
        created = _creation_time(api.GetCurrentProcess())
    return ProcessIdentity(pid=os.getpid(), created=created, host=current_host())


def identity_of(pid: int) -> ProcessIdentity | None:
    """Another local process's identity, read through a handle; ``None`` when unreadable."""
    if not winjob.IS_WINDOWS or pid <= 0:
        return None
    handle, _error = _open_process(pid)
    if handle is None:
        return None
    try:
        created = _creation_time(handle)
    finally:
        _close(handle)
    if created is None:
        return None
    return ProcessIdentity(pid=pid, created=created, host=current_host())


def _open_for_probe(pid: int, host: str | None) -> tuple[Any, Probe | None]:
    """Shared front half of both probes: refuse what cannot be asked, open what can."""
    here = current_host()
    if host is not None and host != here:
        return None, Probe(
            "unknown",
            f"recorded on host {host!r}; this is {here!r}, which cannot ask that host",
            probed=False,
        )
    if not winjob.IS_WINDOWS:
        return None, Probe(
            "unknown",
            "this build reads process identity only on Windows (documented, not observed here)",
            probed=False,
        )
    if pid <= 0:
        return None, Probe("unknown", f"recorded pid {pid} is not a process id", probed=False)
    handle, error = _open_process(pid)
    if handle is None:
        if error == winjob.ERROR_INVALID_PARAMETER:
            return None, Probe("gone", f"no process has pid {pid} (OpenProcess: invalid parameter)")
        return None, Probe(
            "unknown", f"pid {pid} could not be opened (Win32 error {error}); not proof of absence"
        )
    return handle, None


def probe(identity: ProcessIdentity) -> Probe:
    """``matching`` (this exact process runs), ``gone`` (proven), or ``unknown`` (anything else)."""
    if identity.created is None:
        return Probe("unknown", "the owner's creation time was not recorded")
    handle, early = _open_for_probe(identity.pid, identity.host)
    if early is not None:
        return early
    try:
        exited = _exited(handle)
        if exited is None:
            return Probe("unknown", f"waiting on pid {identity.pid} failed")
        if exited:
            return Probe("gone", f"pid {identity.pid} has exited (its process object is signalled)")
        created = _creation_time(handle)
    finally:
        _close(handle)
    if created is None:
        return Probe("unknown", f"the creation time of pid {identity.pid} could not be read")
    if created != identity.created:
        return Probe(
            "gone",
            f"pid {identity.pid} now names a process created at {filetime_iso(created)} "
            f"(FILETIME {created}), not the owner created at {identity.created_iso()} "
            f"(FILETIME {identity.created}): the pid was reused",
        )
    return Probe("matching", f"pid {identity.pid} created at {identity.created_iso()} is running")


def probe_recorded(pid: int, recorded_at: str | None, host: str | None) -> Probe:
    """Probe a process the ledger recorded by pid and a wall-clock time written after its start.

    Used for a run's child processes (``invocations.process_pid``/``process_started_at``), which
    kept no creation FILETIME. (A pre-v6 run's controller pid is *not* probed with it: that pid
    carries no host, see :func:`assess_owner`.) A process created more than ``RECORDED_START_SLACK_SECONDS`` after the
    recorded time cannot be the recorded one (the pid was reused after it exited); one created
    earlier *may* be it, so it reads ``matching`` - this probe can over-report "alive", never
    "gone". Wall-clock changes between the two readings can move that comparison (documented).
    """
    if recorded_at is None:
        return Probe("unknown", f"pid {pid} has no recorded start time to compare with")
    try:
        recorded = parse_ts(recorded_at)
    except ValueError:
        return Probe("unknown", f"pid {pid} has an unreadable recorded start {recorded_at!r}")
    handle, early = _open_for_probe(pid, host)
    if early is not None:
        return early
    try:
        exited = _exited(handle)
        if exited is None:
            return Probe("unknown", f"waiting on pid {pid} failed")
        if exited:
            return Probe("gone", f"pid {pid} has exited (its process object is signalled)")
        created = _creation_time(handle)
    finally:
        _close(handle)
    moment = filetime_to_datetime(created)
    if moment is None:
        return Probe("unknown", f"the creation time of pid {pid} could not be read")
    if moment > recorded + timedelta(seconds=RECORDED_START_SLACK_SECONDS):
        return Probe(
            "gone",
            f"pid {pid} now names a process created at {filetime_iso(created)}, after the "
            f"recorded start {recorded_at} (the pid was reused)",
        )
    return Probe(
        "matching",
        f"a process with pid {pid} created at {filetime_iso(created)} (not after the recorded "
        f"start {recorded_at}) is running; it may be the recorded process",
    )


# -- the owner lock ----------------------------------------------------------------------------


def lock_path_for(ledger_dir: Path, token: str) -> Path:
    if not valid_token(token):
        raise ValueError(f"not an owner token: {token!r}")
    return Path(ledger_dir) / "owners" / f"{token}.lock"


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    if winjob.IS_WINDOWS:
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if winjob.IS_WINDOWS:
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class OwnerLockError(RuntimeError):
    """The owner lock could not be taken; the caller must not claim anything."""


class OwnerLock:
    """An exclusive non-blocking lock on one file, held until :meth:`release` or process exit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise OwnerLockError(f"cannot open the owner lock {self.path}: {exc}") from exc
        if not _try_lock(fd):
            os.close(fd)
            raise OwnerLockError(f"the owner lock {self.path} is held by another holder")
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        os.close(fd)
        try:
            self.path.unlink()
        except OSError:
            # Another process may have it open for a probe; a leftover file holds no lock.
            pass


def lock_is_free(path: Path) -> LockState:
    """``free`` (taken and released here), ``absent`` (no file: nobody holds it) or ``held``.

    Never creates the file. Taking the lock for an instant is the only way to ask; a concurrent
    successor that asks in that instant reads ``held`` and refuses, which fails closed.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        if not _try_lock(fd):
            return "held"
        try:
            _unlock(fd)
        except OSError:
            pass
        return "free"
    finally:
        os.close(fd)


# -- the takeover rule -------------------------------------------------------------------------


def assess_owner(
    *,
    ledger_dir: Path | None,
    owner_token: str | None,
    owner_pid: int | None,
    owner_created: int | None,
    owner_host: str | None,
    recorded_controllers: list[tuple[int, str | None]],
) -> OwnerAssessment:
    """Apply the takeover rule to one run's recorded owner. Read-only.

    * An owner recorded by this build (``owner_token`` set): gone only when its lock can be taken
      (``free``, or ``absent`` - no file) **and** its identity probes ``gone``. A ledger with no
      directory (in memory) has no lock file, so only the identity decides there.
    * A run recorded before owner identity existed (``owner_token`` NULL: a pre-v6 ledger, or a
      label-only claim) has no lock to examine. The controller pids its attempts recorded
      (``attempts.process_id``) carry **no host**, so this build cannot tell whether they ran on
      this machine: a local lookup of another host's pid would read ``gone``. Each therefore reads
      ``unknown`` and the run is never proven gone (the way out is ``hflow cancel``, which blocks
      it ``outcome_unknown``, then ``hflow resume``).
    * A NULL-token run that recorded no controller process at all reads ``not_recorded``: nothing
      was observed, so it is **not** ``gone`` (``gone`` stays ``False``). Whether such a run may be
      taken over is the caller's decision (``resume`` only when nothing was dispatched).
    """
    if owner_token is not None:
        if not valid_token(owner_token):
            return OwnerAssessment(
                False, Probe("unknown", "the recorded owner token is malformed"), "unknown", "",
                "owner token is malformed; refusing to judge the owner",
            )
        lock: LockState
        lock_path = ""
        if ledger_dir is None:
            lock = "not_applicable"
        else:
            path = lock_path_for(ledger_dir, owner_token)
            lock_path = str(path)
            lock = lock_is_free(path)
        identity = ProcessIdentity(
            pid=int(owner_pid) if owner_pid is not None else 0,
            created=owner_created,
            host=owner_host or "",
        )
        result = probe(identity) if owner_pid is not None else Probe(
            "unknown", "the owner's pid was not recorded", probed=False
        )
        gone = lock in {"free", "absent", "not_applicable"} and result.verdict == "gone"
        summary = (
            f"owner pid={owner_pid} host={owner_host} lock={lock} identity={result.verdict}: "
            f"{result.detail}"
        )
        return OwnerAssessment(gone, result, lock, lock_path, summary)

    if not recorded_controllers:
        result = Probe(
            "not_recorded",
            "no owner was recorded for this run and it recorded no controller process (written "
            "before owner identity existed, or never claimed); nothing was observed, so the owner "
            "is not proven gone",
            probed=False,
        )
    else:
        result = Probe(
            "unknown",
            "; ".join(
                f"controller pid {pid} (recorded start {started or 'not recorded'}) was recorded "
                "before storage v6 with no host; this build cannot tell whether it ran on this "
                "host, so it is not probed"
                for pid, started in recorded_controllers
            ),
            probed=False,
        )
    summary = (
        f"legacy owner (no token; no lock evidence) recorded controller process(es) "
        f"{result.verdict}: {result.detail}"
    )
    return OwnerAssessment(False, result, "not_applicable", "", summary)
