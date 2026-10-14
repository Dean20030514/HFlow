"""The scoped fingerprint and the manifest read worker-written trees without ever blocking on them.

A worker controls every file under its worktree. A FIFO, a device or a link to one would hang a
blocking ``open``/``read`` (``/dev/zero`` never ends), and the controller with it. Such an entry is
recorded ``unreadable`` without being read; a regular file's facts - and so every fingerprint that
evidence was recorded against (AGENTS rule 3) - stay exactly what they were.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from hflow.contracts import Scope, digest_of
from hflow.workspace import _file_content_facts, candidate_fingerprint, manifest

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs and /dev are POSIX")


def _bounded(call, seconds: float = 10.0):  # noqa: ANN001, ANN202
    """Run ``call`` on a daemon thread; fail (instead of hanging the suite) if it blocks."""
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(call()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the read blocked"
    return result[0]


def test_a_regular_files_facts_and_fingerprint_are_unchanged(tmp_path: Path) -> None:
    data = b"value = 2\n" * 10_000 + b"\x00\xff tail"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(data)
    (tmp_path / "src" / "empty.txt").write_bytes(b"")

    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    assert _file_content_facts(tmp_path / "src" / "a.py") == (len(data), digest)
    expected = digest_of(
        [
            {"path": "src/a.py", "size": len(data), "digest": digest},
            {"path": "src/empty.txt", "size": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()},
        ]
    )
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["src"])) == expected
    assert manifest(tmp_path)["src/a.py"] == digest


def test_a_directory_is_never_read_as_a_file(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="not a regular file"):
        _file_content_facts(tmp_path)


def test_a_file_swapped_after_the_type_check_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The type is checked again on the opened handle: what was opened is what gets read."""
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    real_fstat = os.fstat

    def now_a_fifo(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result((stat.S_IFIFO | 0o644, *info[1:]))

    monkeypatch.setattr(os, "fstat", now_a_fifo)
    with pytest.raises(OSError, match="not a regular file"):
        _file_content_facts(path)


def test_a_link_to_a_regular_file_is_still_followed(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_bytes(b"linked bytes\n")
    try:
        (tmp_path / "link.txt").symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    entries = manifest(tmp_path)
    assert entries["link.txt"] == entries["real.txt"] == "sha256:" + hashlib.sha256(b"linked bytes\n").hexdigest()


@POSIX_ONLY
def test_a_fifo_in_the_scope_is_recorded_unreadable_without_blocking(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    os.mkfifo(tmp_path / "src" / "pipe")  # no writer: a blocking open would wait forever
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")

    fingerprint = _bounded(lambda: candidate_fingerprint(tmp_path, Scope(write_allow=["src"])))
    entries = _bounded(lambda: manifest(tmp_path))

    assert entries["src/pipe"] == "unreadable"
    assert fingerprint == digest_of(
        [
            {"path": "src/a.py", "size": 6, "digest": "sha256:" + hashlib.sha256(b"x = 1\n").hexdigest()},
            {"path": "src/pipe", "size": None, "digest": "unreadable"},
        ]
    )


@POSIX_ONLY
def test_a_link_to_a_device_in_the_scope_is_recorded_unreadable_without_reading_it(tmp_path: Path) -> None:
    if not os.path.exists("/dev/zero"):  # pragma: no cover - a POSIX system without /dev/zero
        pytest.skip("no /dev/zero here")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "zero").symlink_to("/dev/zero")  # reading it never ends

    entries = _bounded(lambda: manifest(tmp_path))
    fingerprint = _bounded(lambda: candidate_fingerprint(tmp_path, Scope(write_allow=["src"])))

    assert entries["src/zero"] == "unreadable"
    assert fingerprint == digest_of([{"path": "src/zero", "size": None, "digest": "unreadable"}])
