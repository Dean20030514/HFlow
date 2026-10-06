"""Streaming hashes preserve the existing candidate identity and unreadable facts."""

from __future__ import annotations

import hashlib
import io
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


@pytest.mark.parametrize("size", [0, 1, 65535, 65536, 65537, 131072, 196613])
def test_hashes_match_the_original_bytes_without_whole_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    target = tmp_path / "binary.dat"
    target.write_bytes(payload)
    expected = _entry("binary.dat", payload)
    original_open = Path.open
    reads: list[int] = []
    handles = []

    class BoundedReader:
        def __init__(self, source):
            self.source = source
            handles.append(source)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, count: int = -1) -> bytes:
            assert 0 < count <= 65536, "a candidate file must never be read all at once"
            reads.append(count)
            return self.source.read(count)

    def open_bounded(path, mode="r", *args, **kwargs):
        source = original_open(path, mode, *args, **kwargs)
        return BoundedReader(source) if path == target and mode == "rb" else source

    def refuse_read_bytes(_path):
        raise AssertionError("candidate hashing must use bounded binary reads")

    monkeypatch.setattr(Path, "open", open_bounded)
    monkeypatch.setattr(Path, "read_bytes", refuse_read_bytes)

    assert manifest(tmp_path) == {"binary.dat": expected["digest"]}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["binary.dat"])) == digest_of([expected])
    assert reads, "the resource bound must be exercised, including for an empty file"
    assert all(source.closed for source in handles)


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
    original_open = Path.open

    class ShortReader(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= 65536
            return super().read(min(count, 7))

    def short_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            return ShortReader(payload)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", short_open)
    expected = _entry("file.bin", payload)
    assert manifest(tmp_path) == {"file.bin": expected["digest"]}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["file.bin"])) == digest_of([expected])


@pytest.mark.parametrize("failure_stage", ["open", "read"])
def test_io_failure_keeps_the_whole_file_unreadable_and_closes_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"x" * 65537)
    original_open = Path.open
    handles = []

    class FailingReader:
        def __init__(self, source):
            self.source = source
            self.reads = 0
            handles.append(source)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, count):
            self.reads += 1
            if self.reads > 1:
                raise OSError("offline injected read failure")
            return self.source.read(count)

    def failing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            if failure_stage == "open":
                raise PermissionError("offline injected open failure")
            return FailingReader(original_open(path, mode, *args, **kwargs))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    assert manifest(tmp_path) == {"file.bin": "unreadable"}
    assert candidate_fingerprint(tmp_path, Scope(write_allow=["file.bin"])) == digest_of(
        [{"path": "file.bin", "size": None, "digest": "unreadable"}]
    )
    assert all(source.closed for source in handles)
