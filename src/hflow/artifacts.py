"""Bounded capture of child-process output, and the environment a check is allowed to see.

Two problems this module solves, both about *control* rather than features:

* a chatty check must not be able to grow controller memory or fill the disk. Output is drained
  continuously (so the child never blocks on a full pipe) and written to a file only up to a
  declared limit; the digest covers everything, the retained file covers only what fits, and
  the difference is recorded instead of being hidden;
* a check must not inherit the controller's provider credentials. The environment is built from
  an explicit allowlist of what a process needs to start and find its runtime, plus the variables
  a project declares as approved test variables.

Nothing here is a security boundary and nothing here claims to be one: an allowlist removes
accidental inheritance, it does not confine a process that already runs with the user's rights.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: Bytes retained per check stream by default (plan 8.2). The *first* bytes are kept and the rest
#: are read, counted and digested but not written: a true bound needs a bounded amount of memory
#: as well as a bounded file, which is why the reader never accumulates the whole stream.
DEFAULT_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
#: Bytes kept for the run's whole invocation budget. Reserved for the driver's own logs, which
#: are kept separately; it exists here so the two limits are declared in one place.
DEFAULT_INVOCATION_LIMIT_BYTES = 32 * 1024 * 1024
#: How much of a stream's head is quoted into an evidence row's detail text.
DEFAULT_EXCERPT_BYTES = 2000

_READ_CHUNK = 64 * 1024


@dataclass
class StreamCapture:
    """One captured stream: where it is, how big it was, and whether it was cut.

    ``digest`` covers the *readable* bytes (the retained head), not the whole stream: when a
    stream is cut, a hash of bytes nobody can obtain could not be checked by a reader. The
    discarded tail is not lost from the record - ``total_bytes`` and ``truncated`` say exactly
    how much was not kept.
    """

    path: Path
    total_bytes: int
    retained_bytes: int
    digest: str
    truncated: bool
    head: str = ""
    #: Set when the capture itself failed (a failed write, flush or close). A stream whose capture
    #: failed is incomplete whatever the exit code was, and a caller must not report a pass for it.
    failed: bool = False
    failure_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "total_bytes": self.total_bytes,
            "retained_bytes": self.retained_bytes,
            "digest": self.digest,
            "truncated": self.truncated,
            "failed": self.failed,
            "failure_reason": self.failure_reason,
            "note": (
                f"the digest covers the retained head ({self.retained_bytes} of "
                f"{self.total_bytes} bytes); the discarded tail is counted but not kept"
                if self.truncated
                else "the whole stream is on disk and the digest covers all of it"
            ),
        }


class BoundedTextSink:
    """Drain a binary stream into a bounded file while digesting everything read.

    The file is truncated at ``limit``; nothing else about the stream is dropped silently - the
    total byte count, the digest and a truncation flag describe what was actually seen, so a
    reader can tell "this is all of it" from "this is the head of it".
    """

    def __init__(self, path: Path, *, limit: int = DEFAULT_STREAM_LIMIT_BYTES) -> None:
        self.path = Path(path)
        self.limit = max(0, int(limit))
        self.total_bytes = 0
        self.retained_bytes = 0
        self._digest = hashlib.sha256()
        self._retained_digest = hashlib.sha256()
        self._handle: IO[bytes] | None = None
        self._excerpt = bytearray()
        self._excerpt_limit = DEFAULT_EXCERPT_BYTES
        #: Set when this capture failed. A caller must treat that as "incomplete", not as "done":
        #: the bytes already digested are real, but the stream they came from was not read out.
        self.failed = False
        self.failure_reason = ""

    def __enter__(self) -> BoundedTextSink:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write(self, chunk: bytes) -> None:
        """Count and digest everything; retain the head. Never raises, never lies about failing."""
        if not chunk:
            return
        self._digest.update(chunk)
        self.total_bytes += len(chunk)
        if len(self._excerpt) < self._excerpt_limit:
            room = self._excerpt_limit - len(self._excerpt)
            self._excerpt += chunk[:room]
        if self.failed or self._handle is None or self.retained_bytes >= self.limit:
            return
        room = self.limit - self.retained_bytes
        kept = chunk[:room]
        try:
            self._handle.write(kept)
            self._handle.flush()
        except (OSError, ValueError) as exc:
            # Recorded, not swallowed: the caller has to know that the retained output is
            # incomplete. Whatever digest covers the bytes read so far is still true.
            self.failed = True
            self.failure_reason = f"{type(exc).__name__}: {exc}"
            return
        self._retained_digest.update(kept)
        self.retained_bytes += len(kept)

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.flush()
            self._handle.close()
        except (OSError, ValueError) as exc:
            self.failed = True
            self.failure_reason = self.failure_reason or f"{type(exc).__name__}: {exc}"
        finally:
            self._handle = None

    @property
    def truncated(self) -> bool:
        """Has anything been read that was not retained?"""
        return self.total_bytes > self.retained_bytes

    @property
    def exhausted(self) -> bool:
        """Is the retention budget used up? Checked before writing another retained record."""
        return self.retained_bytes >= self.limit

    def excerpt(self, limit: int | None = None) -> str:
        """The head of the stream, decoded for an evidence row (never the whole thing)."""
        text = bytes(self._excerpt[: limit or self._excerpt_limit]).decode("utf-8", "replace")
        suffix = "… (truncated)" if self.truncated else ""
        return text + suffix

    def capture(self) -> StreamCapture:
        self.close()
        return StreamCapture(
            path=self.path,
            total_bytes=self.total_bytes,
            retained_bytes=self.retained_bytes,
            digest="sha256:" + self._retained_digest.hexdigest(),
            truncated=self.truncated,
            head=bytes(self._excerpt).decode("utf-8", "replace"),
            failed=self.failed,
            failure_reason=self.failure_reason,
        )


def drain(stream: IO[bytes] | None, sink: BoundedTextSink) -> str:
    """Read a stream to EOF in chunks, feeding the sink. Bounded memory, no deadlock.

    Returns a short outcome word rather than nothing, so the caller can tell a clean end of stream
    from a capture that failed:

    * ``"eof"`` - the stream ended and everything read was accounted for;
    * ``"capture_failed"`` - the sink could not retain what it read (a write/flush failure). The
      digest still describes the bytes read, but the capture is incomplete;
    * ``"read_failed: ..."`` - the stream itself could not be read (the pipe went away).

    A read that fails after a capture failure keeps the capture failure as the reason: both mean
    the same thing to a caller, and the first failure is the more specific one.
    """
    if stream is None:
        return "no_stream"
    while True:
        try:
            chunk = stream.read(_READ_CHUNK)
        except (OSError, ValueError) as exc:
            return (
                "capture_failed"
                if sink.failed
                else f"read_failed: {type(exc).__name__}: {exc}"
            )
        if not chunk:
            return "capture_failed" if sink.failed else "eof"
        try:
            sink.write(chunk)
        except (OSError, ValueError) as exc:
            # A sink implementation that raises instead of recording is treated exactly like one
            # that records: the capture failed, and the caller must not see a clean end of stream.
            sink.failed = True
            sink.failure_reason = sink.failure_reason or f"{type(exc).__name__}: {exc}"
            continue
        if sink.failed:
            # Keep draining rather than returning here: a child that is still writing must not
            # block on a full pipe while this thread walks away. The failure is recorded and the
            # caller decides what it means.
            continue


#: Environment variables a child process needs in order to start and to find its runtime. This is
#: an allowlist on purpose: "delete the keys I can name" would silently pass through every
#: variable nobody thought of, which is exactly how a credential reaches a check.
_BASE_ALLOWED_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    # Read by Unity's package manager, which fails to resolve packages without it. Inherited
    # from the environment like every other system path here; never synthesized. Found by a real
    # check run whose cold Unity project could not resolve packages (run R-uzc5wk2wdf).
    "ALLUSERSPROFILE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "OS",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "NODE_PATH",
    "NODE_OPTIONS",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "SHELL",
    "TERM",
    "NO_COLOR",
    "CI",
    "TZ",
)

#: Name fragments that never reach a child, whatever else says otherwise. Checked case
#: insensitively and applied *after* the allowlist, so an approved variable that happens to look
#: like a secret is still dropped rather than passed through by accident.
_FORBIDDEN_FRAGMENTS = (
    "API_KEY",
    "APIKEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
    "SESSION_KEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)


def is_secret_like(name: str) -> bool:
    """Does this variable's name say "credential"? Used to keep one out of a child process."""
    upper = name.upper()
    return any(fragment in upper for fragment in _FORBIDDEN_FRAGMENTS)


def child_environment(
    *,
    extra: dict[str, str] | None = None,
    base: dict[str, str] | None = None,
    allow: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[str]]:
    """The environment a check may see, plus a list of what was withheld.

    ``extra`` are the operator/project-declared variables for this run (already approved by being
    in the project contract or by the operator passing them); they are still refused when their
    name looks like a credential, and the refusal is reported instead of being silent.
    ``base`` defaults to the controller's own environment; a test can supply its own.
    """
    source = dict(os.environ if base is None else base)
    allowed: dict[str, str] = {}
    for name in _BASE_ALLOWED_NAMES:
        value = source.get(name)
        if value is not None and not is_secret_like(name):
            allowed[name] = value
    for name in allow:
        value = source.get(name)
        if value is not None and not is_secret_like(name):
            allowed[name] = value

    withheld: list[str] = []
    for name, value in dict(extra or {}).items():
        if is_secret_like(name):
            withheld.append(name)
            continue
        allowed[name] = value

    # Anything the controller holds that looks like a credential is reported as withheld, so the
    # summary can say what was kept out rather than only what was let in.
    for name in sorted(source):
        if name not in allowed and is_secret_like(name):
            withheld.append(name)
    return allowed, sorted(set(withheld))


def environment_summary(env: dict[str, str], *, withheld: list[str] | None = None) -> str:
    """A short, non-secret description of the environment a check ran with.

    Names and counts only: no values, and no hash of a value - a hash of a low-entropy secret is
    still a disclosure, and the point here is to record *which* variables a check could see.

    ``withheld_secret_like=`` is a **comma-separated list after a single ``=``**, and its order is
    sorted so the string is reproducible. A reader must therefore treat it as a list of names
    (:func:`parse_environment_summary`), not as a substring to match: whether any one name happens
    to sit at the start of the list depends on which other names exist on the machine.
    """
    names = ", ".join(sorted(env))
    text = f"env_names={len(env)}: {names}"
    if withheld:
        text += f"; withheld_secret_like={','.join(sorted(withheld))}"
    return text


def parse_environment_summary(text: str) -> dict[str, object]:
    """Read back an :func:`environment_summary` string. Used by readers and by the tests.

    Returns ``{"names": [...], "withheld": [...], "count": int}``. A missing section yields an
    empty list rather than an error, so an older or shorter summary is read as "nothing recorded"
    instead of crashing a report.
    """
    head, _, tail = text.partition(";")
    count_text, _, names_text = head.partition(":")
    names = [name.strip() for name in names_text.split(",") if name.strip()]
    withheld: list[str] = []
    if tail.startswith(" withheld_secret_like="):
        withheld = [name.strip() for name in tail.split("=", 1)[1].split(",") if name.strip()]
    digits = "".join(character for character in count_text if character.isdigit())
    return {"names": names, "withheld": withheld, "count": int(digits) if digits else len(names)}


def write_artifact_manifest(directory: Path, payload: dict[str, object]) -> Path:
    """Write one check's artifact manifest next to its captured streams."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "artifact.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def python_interpreter() -> str:
    """The interpreter running this code, used only to describe the runtime in a manifest."""
    return sys.executable
