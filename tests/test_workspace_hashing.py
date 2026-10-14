"""Streaming hashes preserve the existing candidate identity and unreadable facts."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from hflow.contracts import Scope, digest_of
from hflow.workspace import candidate_fingerprint, manifest


def _entry(path: str, payload: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size": len(payload),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


class _TargetFds:
    """Watch the low-level reads of one file: candidate hashing opens it with ``os.open``
    (non-blocking, regular files only) and reads it with ``os.read`` in bounded chunks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
        self.target = target
        self.fds: set[int] = set()
        self.closed: set[int] = set()
        self.reads: list[int] = []
        self._open, self._read, self._close = os.open, os.read, os.close
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "read", self.read)
        monkeypatch.setattr(os, "close", self.close)

    def opens_target(self, path) -> bool:
        return Path(path) == self.target

    def open(self, path, flags, *args, **kwargs):
        fd = self._open(path, flags, *args, **kwargs)
        if self.opens_target(path):
            self.fds.add(fd)
        return fd

    def read(self, fd: int, count: int) -> bytes:
        if fd in self.fds:
            assert 0 < count <= 65536, "a candidate file must never be read all at once"
            self.reads.append(count)
        return self._read(fd, count)

    def close(self, fd: int) -> None:
        if fd in self.fds:
            self.closed.add(fd)
        self._close(fd)


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 131072, 196613])
def test_hashes_match_the_original_bytes_without_whole_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    target = tmp_path / "binary.dat"
    target.write_bytes(payload)
    expected = _entry("binary.dat", payload)

    def refuse_read_bytes(_path):
        raise AssertionError("candidate hashing must use bounded binary reads")

    watch = _TargetFds(monkeypatch, target)
    monkeypatch.setattr(Path, "read_bytes", refuse_read_bytes)

    assert manifest(tmp_path) == {"binary.dat": expected["digest"]}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["binary.dat"])) == digest_of([expected])
    assert watch.reads, "the resource bound must be exercised, including for an empty file"
    assert watch.fds and watch.closed == watch.fds


def test_streaming_keeps_scope_sorting_deduplication_deny_and_missing_paths(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    payloads = {"src/z.bin": b"\x00\xff", "src/nested/a.bin": b"nested", "src/deny.bin": b"denied"}
    for name, payload in payloads.items():
        (tmp_path / name).write_bytes(payload)
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
    scope = Scope(write_allow=["src", "src/nested", "src/z.bin", "missing"], write_deny=["src/deny.bin"])
    expected = [_entry(name, payloads[name]) for name in ("src/nested/a.bin", "src/z.bin")]
    assert candidate_fingerprint(tmp_path, scope) == digest_of(expected)
    assert manifest(tmp_path) == {name: _entry(name, data)["digest"] for name, data in payloads.items()}
    (tmp_path / "src" / "z.bin").unlink()
    assert candidate_fingerprint(tmp_path, scope) == digest_of(expected[:1])


def test_hash_size_counts_short_reads_instead_of_the_files_prior_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"prior size")
    payload = bytes(range(256)) * 4
    watch = _TargetFds(monkeypatch, target)
    streams: dict[int, io.BytesIO] = {}

    def fresh_open(path, flags, *args, **kwargs):
        fd = watch.open(path, flags, *args, **kwargs)
        if fd in watch.fds:
            streams[fd] = io.BytesIO(payload)  # a reused fd number starts a new stream
        return fd

    def short_read(fd: int, count: int) -> bytes:
        if fd in watch.fds:
            assert 0 < count <= 65536
            return streams[fd].read(min(count, 7))
        return watch._read(fd, count)

    monkeypatch.setattr(os, "open", fresh_open)
    monkeypatch.setattr(os, "read", short_read)
    expected = _entry("file.bin", payload)
    assert manifest(tmp_path) == {"file.bin": expected["digest"]}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["file.bin"])) == digest_of([expected])


@pytest.mark.parametrize("failure_stage", ["open", "read"])
def test_io_failure_keeps_the_whole_file_unreadable_and_closes_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"x" * 65537)
    watch = _TargetFds(monkeypatch, target)
    reads: dict[int, int] = {}

    def failing_open(path, flags, *args, **kwargs):
        if watch.opens_target(path) and failure_stage == "open":
            raise PermissionError("offline injected open failure")
        return watch.open(path, flags, *args, **kwargs)

    def failing_read(fd: int, count: int) -> bytes:
        if fd in watch.fds:
            reads[fd] = reads.get(fd, 0) + 1
            if reads[fd] > 1:
                raise OSError("offline injected read failure")
        return watch._read(fd, count)

    monkeypatch.setattr(os, "open", failing_open)
    monkeypatch.setattr(os, "read", failing_read)
    assert manifest(tmp_path) == {"file.bin": "unreadable"}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["file.bin"])) == digest_of(
        [{"path": "file.bin", "size": None, "digest": "unreadable"}]
    )
    assert watch.closed == watch.fds
    assert bool(watch.fds) is (failure_stage == "read")
