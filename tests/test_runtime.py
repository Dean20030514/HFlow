"""The controller build id: a git sha only from HFlow's own source checkout."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import hflow.runtime as runtime


@pytest.fixture()
def fresh_build_id(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """``controller_build`` uncached and without an override; yields the roots git was asked in."""
    asked: list[Path] = []

    def git_sha(repo_root: Path) -> str:
        asked.append(repo_root)
        return "abc1234"

    monkeypatch.delenv(runtime.ENV_BUILD_ID, raising=False)
    monkeypatch.setattr(runtime, "_git_sha", git_sha)
    runtime.controller_build.cache_clear()
    yield asked
    runtime.controller_build.cache_clear()


def test_a_source_checkout_records_its_git_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_build_id: list[Path]
) -> None:
    module = tmp_path / "checkout" / "src" / "hflow" / "runtime.py"
    monkeypatch.setattr(runtime, "__file__", str(module))

    assert runtime.controller_build() == f"hflow/{runtime.PACKAGE_VERSION}+abc1234"
    assert fresh_build_id == [(tmp_path / "checkout").resolve()]


def test_an_installed_copy_never_records_the_surrounding_repository_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_build_id: list[Path]
) -> None:
    module = tmp_path / "project" / ".venv" / "Lib" / "site-packages" / "hflow" / "runtime.py"
    monkeypatch.setattr(runtime, "__file__", str(module))

    assert runtime.controller_build() == f"hflow/{runtime.PACKAGE_VERSION}"
    assert fresh_build_id == [], "git is not asked about a repository that is not HFlow's"


def test_the_override_wins_without_asking_git(
    monkeypatch: pytest.MonkeyPatch, fresh_build_id: list[Path]
) -> None:
    monkeypatch.setenv(runtime.ENV_BUILD_ID, "hflow/pinned")

    assert runtime.controller_build() == "hflow/pinned"
    assert fresh_build_id == []
