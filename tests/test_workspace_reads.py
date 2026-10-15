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
from hflow.workspace import (
    candidate_fingerprint,
    changed_paths,
    expand_scope,
    hash_regular_file,
    manifest,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs and /dev are POSIX")
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")


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
    assert hash_regular_file(tmp_path / "src" / "a.py") == (len(data), digest)
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
        hash_regular_file(tmp_path)


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
        hash_regular_file(path)


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


def test_a_limit_refuses_a_larger_file_without_changing_the_facts_of_a_smaller_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 100)
    digest = "sha256:" + hashlib.sha256(b"x" * 100).hexdigest()
    assert hash_regular_file(path, limit=100) == hash_regular_file(path) == (100, digest)
    with pytest.raises(OSError, match="larger than 99 bytes"):
        hash_regular_file(path, limit=99)


# --------------------------------------------------------------------------
# directory links and junctions: listed as themselves, never entered
# --------------------------------------------------------------------------


def _junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _unlink_all(*links: Path) -> None:
    """Remove the links themselves (``os.rmdir`` on a junction never touches its target)."""
    for link in links:
        if os.path.lexists(link):
            try:
                os.rmdir(link)
            except OSError:
                os.unlink(link)


@WINDOWS_ONLY
def test_junctions_back_to_their_parent_are_listed_not_walked(tmp_path: Path) -> None:
    """Two junctions pointing back at ``src`` made ``os.walk`` descend without end.

    ``os.walk(followlinks=False)`` stops at a symbolic link, but a junction is not
    ``is_symlink()``, so the walk entered each junction, found both again, and doubled at every
    level. Every read that walks a worker's tree - the scoped fingerprint, ``expand_scope``, the
    manifest a run diffs, ``status``/``report`` drift - would hang the controller.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_bytes(b"x = 1\n")
    j1, j2 = src / "j1", src / "j2"
    _junction(src, j1)
    _junction(src, j2)
    try:
        entries = _bounded(lambda: manifest(tmp_path))
        scoped = _bounded(lambda: expand_scope(tmp_path, Scope(write_allow=["src"])))
        fingerprint = _bounded(lambda: candidate_fingerprint(tmp_path, Scope(write_allow=["src"])))
        j1_target = os.readlink(j1)
    finally:
        _unlink_all(j1, j2)

    a_digest = "sha256:" + hashlib.sha256(b"x = 1\n").hexdigest()
    assert set(entries) == {"src/a.py", "src/j1", "src/j2"}, sorted(entries)
    assert entries["src/a.py"] == a_digest
    # The link itself is the entry, recorded by where it points.
    assert entries["src/j1"] == "link:" + j1_target
    assert j1_target.endswith(os.path.join(tmp_path.name, "src"))
    root = Path(os.path.realpath(tmp_path))
    assert [p.relative_to(root).as_posix() for p in scoped] == ["src/a.py", "src/j1", "src/j2"]
    assert fingerprint == digest_of(
        [
            {"path": "src/a.py", "size": 6, "digest": a_digest},
            {"path": "src/j1", "size": None, "digest": entries["src/j1"]},
            {"path": "src/j2", "size": None, "digest": entries["src/j2"]},
        ]
    )
    assert (src / "a.py").read_bytes() == b"x = 1\n", "removing the links left the target alone"


@WINDOWS_ONLY
def test_a_retargeted_junction_changes_the_fingerprint_and_the_manifest(tmp_path: Path) -> None:
    """Not entering a link must not hide it: pointing it elsewhere is a change (AGENTS rule 3)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"x = 1\n")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "src" / "data"
    scope = Scope(write_allow=["src"])
    _junction(first, link)
    try:
        before_fp = candidate_fingerprint(tmp_path, scope)
        before = manifest(tmp_path)
        _unlink_all(link)
        _junction(second, link)
        after_fp = candidate_fingerprint(tmp_path, scope)
        after = manifest(tmp_path)
    finally:
        _unlink_all(link)

    assert before_fp != after_fp
    assert changed_paths(before, after) == ["src/data"]


def test_a_directory_symlink_is_listed_not_walked(tmp_path: Path) -> None:
    """A symbolic link to a directory used to be skipped silently; it is now one recorded entry."""
    (tmp_path / "src").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "inside.txt").write_bytes(b"behind the link\n")
    link = tmp_path / "src" / "linked"
    try:
        link.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    try:
        entries = _bounded(lambda: manifest(tmp_path))
        scoped = expand_scope(tmp_path, Scope(write_allow=["src"]))
        target = os.readlink(link)
    finally:
        _unlink_all(link)

    assert entries["src/linked"] == "link:" + target
    assert "src/linked/inside.txt" not in entries
    assert entries["elsewhere/inside.txt"].startswith("sha256:")
    assert [p.name for p in scoped] == ["linked"]


def test_a_skipped_directory_name_is_skipped_but_a_file_of_that_name_is_not(tmp_path: Path) -> None:
    """``.git`` as a directory is VCS metadata; a worktree's ``.git`` *file* is an ordinary entry."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (tmp_path / "worktree").mkdir()
    (tmp_path / "worktree" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (tmp_path / "worktree" / "__pycache__").mkdir()
    (tmp_path / "worktree" / "__pycache__" / "m.pyc").write_bytes(b"\0")

    assert sorted(manifest(tmp_path)) == ["worktree/.git"]
