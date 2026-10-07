"""What HFlow's own git calls cannot be made to read: inherited variables and planted repositories.

Every case builds real temporary repositories with the git on PATH; nothing reads the user's
global or system configuration. A *control* step runs plain git with the same environment, to
show the variable or the planted directory does change what git reads when HFlow's environment
is not in the way - so a passing test is not a variable git simply ignores.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hflow import gitworkspace
from hflow.cleanup import git_registration_exists
from hflow.gitworkspace import GitError, GitRepo, _base_env, _forced_config


@pytest.fixture(autouse=True)
def _no_ambient_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        *gitworkspace._REPOSITORY_LOCATING_ENV,
        *gitworkspace._CONTENT_REDIRECTING_ENV,
        "GIT_GRAFT_FILE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(name, raising=False)


def _git_completed(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Plain git in this test's environment - whatever variables the test has set."""
    return subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Sample Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Sample Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )


def _git(cwd: Path, *args: str) -> str:
    completed = _git_completed(cwd, *args)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def _commit(cwd: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        (cwd / name).write_text(content, encoding="utf-8", newline="\n")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Two commits: ``HEAD~1`` (``first``) and ``HEAD`` (``second``)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "first", {"app.txt": "value = 1\n"})
    _commit(root, "second", {"app.txt": "value = 2\n"})
    return root


def _without_bare_repository_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    original = gitworkspace._forced_config
    monkeypatch.setattr(
        gitworkspace,
        "_forced_config",
        lambda: tuple(item for item in original() if item[0] != "safe.bareRepository"),
    )


# -- safe.bareRepository ---------------------------------------------------------------------


def test_explicit_bare_repositories_are_forced_in_protected_command_scope(repo: Path) -> None:
    assert ("safe.bareRepository", "explicit") in _forced_config()
    # Git honours the key only from protected configuration; ``command`` scope is protected.
    shown = GitRepo(repo).run("config", "--show-scope", "--get", "safe.bareRepository")
    assert shown == "command\texplicit\n"


def test_a_forced_key_does_not_change_the_metadata_snapshot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitRepo(repo)
    worktree = git.create_worktree("R-snapshot", git.head)
    with_key = git.metadata_snapshot(worktree)
    assert not any("barerepository" in label.casefold() for label, _ in with_key.entries)

    _without_bare_repository_setting(monkeypatch)
    assert git.metadata_snapshot(worktree) == with_key


def test_a_bare_repository_planted_in_a_worktree_root_is_never_adopted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker deletes its worktree's ``.git`` file and leaves a bare repository's layout there.

    Git would take the worktree root for an implicit bare repository and read the worker's
    ``config`` as the repository's (openai/codex PR #36924). HFlow runs git there - the freeze,
    the metadata snapshot, cleanup - so every such call must refuse instead.
    """
    git = GitRepo(repo)
    worktree = git.create_worktree("R-planted", git.head)
    (worktree / ".git").unlink()
    (worktree / "HEAD").write_text(git.head + "\n", encoding="utf-8")
    (worktree / "objects").mkdir()
    (worktree / "refs").mkdir()
    (worktree / "config").write_text(
        "[core]\n\tbare = true\n\tfsmonitor = planted-monitor\n[hflow]\n\tplanted = yes\n",
        encoding="utf-8",
        newline="\n",
    )

    with monkeypatch.context() as patch:
        # Control: without the setting git adopts the planted directory and reads its config.
        _without_bare_repository_setting(patch)
        assert git.run("config", "--get", "hflow.planted", cwd=worktree) == "yes\n"

    for call in (
        lambda: git.worktree_commit(worktree),
        lambda: git.status_report(worktree),
        lambda: git.metadata_snapshot(worktree),
    ):
        with pytest.raises(GitError, match="bare repository"):
            call()
    # Read gently (as ``git config`` does), the planted repository is simply not a repository.
    assert git._git_query(worktree, "config", "--get", "hflow.planted").stdout == ""


def test_a_project_whose_main_repository_is_bare_still_works(tmp_path: Path) -> None:
    """A linked worktree of a bare repository is found through its ``.git`` file: explicit."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "base", {"app.txt": "value = 1\n"})
    bare = tmp_path / "project.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    checkout = tmp_path / "project"
    _git(bare, "worktree", "add", "-q", str(checkout), "main")

    git = GitRepo.discover(checkout)
    assert git.root == checkout.resolve()
    common = (git.root / git.common_dir).resolve()
    assert common == bare.resolve()
    assert git.checked_out_at("refs/heads/main") == [checkout.resolve()]
    assert git.main_worktree() == bare.resolve()

    worktree = git.create_worktree("R-bare-main", git.head)
    snapshot = git.metadata_snapshot(worktree)
    (worktree / "app.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
    freeze = git.freeze_candidate(worktree, ["app.txt"], expected_head=git.head)
    assert freeze.paths == ("app.txt",)
    assert git.metadata_snapshot(worktree) == snapshot
    # Cleanup's registration check names the repository with ``--git-dir``: explicit too.
    assert git_registration_exists(str(common), worktree)
    git.remove_worktree_checked(worktree)
    assert not git_registration_exists(str(common), worktree)


# -- inherited variables -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [*gitworkspace._REPOSITORY_LOCATING_ENV, *gitworkspace._CONTENT_REDIRECTING_ENV],
)
def test_an_inherited_variable_never_reaches_git(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, "1")
    assert name not in _base_env()


def test_history_discovery_attribute_and_diff_redirects_are_all_dropped() -> None:
    dropped = {*gitworkspace._REPOSITORY_LOCATING_ENV, *gitworkspace._CONTENT_REDIRECTING_ENV}
    assert {
        "GIT_SHALLOW_FILE",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_ATTR_SOURCE",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_CONFIG",
        "GIT_IMPLICIT_WORK_TREE",
    } <= dropped


def test_an_inherited_ceiling_is_kept_because_it_only_narrows_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert _base_env()["GIT_CEILING_DIRECTORIES"] == str(tmp_path)


def test_grafts_inherited_or_written_by_a_worker_do_not_change_ancestry(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _git(repo, "rev-parse", "HEAD~1").strip()
    second = _git(repo, "rev-parse", "HEAD").strip()
    # ``info/grafts`` in the common git directory: writable from any worktree. A graft line with
    # no parents makes ``second`` a root commit.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(second + "\n", encoding="utf-8")
    # Control: plain git obeys it, replace objects off or not.
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "1")
    assert _git_completed(repo, "merge-base", "--is-ancestor", first, second).returncode == 1

    git = GitRepo(repo)
    assert git.is_ancestor(first, second)
    assert git.run("rev-list", "--count", second).strip() == "2"
    assert not os.path.exists(_base_env()["GIT_GRAFT_FILE"])

    inherited = tmp_path / "inherited-grafts"
    inherited.write_text(second + "\n", encoding="utf-8")
    (repo / ".git" / "info" / "grafts").unlink()
    monkeypatch.setenv("GIT_GRAFT_FILE", str(inherited))
    assert _git_completed(repo, "merge-base", "--is-ancestor", first, second).returncode == 1
    assert _base_env()["GIT_GRAFT_FILE"] != str(inherited)
    assert git.is_ancestor(first, second)


def test_an_inherited_shallow_file_does_not_cut_history(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _git(repo, "rev-parse", "HEAD~1").strip()
    second = _git(repo, "rev-parse", "HEAD").strip()
    shallow = tmp_path / "shallow"
    shallow.write_text(second + "\n", encoding="utf-8")
    monkeypatch.setenv("GIT_SHALLOW_FILE", str(shallow))
    assert _git_completed(repo, "merge-base", "--is-ancestor", first, second).returncode == 1

    assert GitRepo(repo).is_ancestor(first, second)


def test_an_inherited_git_config_does_not_blind_the_metadata_snapshot(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_CONFIG`` makes ``git config`` list one file as ``command`` scope - which the
    snapshot skips - so a worker's filter in the shared config would have gone unseen."""
    git = GitRepo(repo)
    worktree = git.create_worktree("R-git-config", git.head)
    before = git.metadata_snapshot(worktree)
    elsewhere = tmp_path / "elsewhere.gitconfig"
    elsewhere.write_text("[user]\n\tname = Someone\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG", str(elsewhere))
    # Control: plain ``git config --list`` now shows that one file, and only as command scope.
    listed = _git(repo, "config", "--list", "--show-scope").splitlines()
    assert listed and all(line.startswith("command\t") for line in listed)

    assert git.metadata_snapshot(worktree) == before
    # What a worker can do from its worktree: write the shared repository config.
    with open(repo / ".git" / "config", "a", encoding="utf-8", newline="\n") as handle:
        handle.write('[filter "planted"]\n\tclean = planted-clean\n')
    assert git.metadata_snapshot(worktree) != before


def test_inherited_diff_variables_do_not_change_the_bounded_diff(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [f"line {index}\n" for index in range(40)]
    before = _commit(repo, "long", {"long.txt": "".join(lines)})
    lines[20] = "changed\n"
    after = _commit(repo, "changed", {"long.txt": "".join(lines)})
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=30")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "planted-external-diff")
    control = _git(repo, "diff", "--no-ext-diff", "--unified=3", f"{before}..{after}", "--", "long.txt")
    assert sum(line.startswith(" ") for line in control.splitlines()) > 6

    text, truncated = GitRepo(repo).diff_text_bounded(
        before, after, "long.txt", max_bytes=100_000
    )
    assert not truncated
    assert sum(line.startswith(" ") for line in text.splitlines()) == 6, text


def test_an_inherited_attribute_source_does_not_change_attributes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = _commit(repo, "attributes", {".gitattributes": "*.txt diff=planted\n"})
    (repo / ".gitattributes").write_text("*.txt -diff\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_ATTR_SOURCE", committed)
    assert _git(repo, "check-attr", "diff", "app.txt").strip() == "app.txt: diff: planted"

    assert GitRepo(repo).run("check-attr", "diff", "app.txt").strip() == "app.txt: diff: unset"


@pytest.mark.parametrize("name", ["GIT_ICASE_PATHSPECS", "GIT_GLOB_PATHSPECS"])
def test_an_inherited_pathspec_mode_does_not_break_the_freeze(
    repo: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    git = GitRepo(repo)
    worktree = git.create_worktree("R-pathspec", git.head)
    (worktree / "app.txt").write_text("value = 3\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv(name, "1")
    # Control: git refuses to combine it with the literal pathspecs HFlow stages with.
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    assert _git_completed(worktree, "ls-files", "--", "app.txt").returncode != 0
    monkeypatch.delenv("GIT_LITERAL_PATHSPECS")

    freeze = git.freeze_candidate(worktree, ["app.txt"], expected_head=git.head)
    assert freeze.paths == ("app.txt",)
