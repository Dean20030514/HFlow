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


# -- creating a run's worktree ---------------------------------------------------------------


def _user_worktree_files(linked: Path) -> dict[str, bytes]:
    """The two files that link the user's own linked worktree to the repository, as bytes."""
    gitdir = (linked / ".git").read_bytes()
    admin = Path(gitdir.decode("utf-8").removeprefix("gitdir: ").strip())
    if not admin.is_absolute():
        admin = (linked / admin).resolve()
    return {".git": gitdir, "admin gitdir": (admin / "gitdir").read_bytes()}


def test_creating_a_run_worktree_leaves_the_users_other_worktrees_byte_identical(
    repo: Path, tmp_path: Path
) -> None:
    """Rule 12. ``git worktree repair`` rewrote *every* linked worktree's ``.git`` file.

    With ``worktree.useRelativePaths`` set after the user made a worktree, the repair that used to
    follow the staging rename turned the user's absolute ``gitdir:`` into a relative one. Only the
    run's own worktree may change.
    """
    linked = tmp_path / "user-linked"
    _git(repo, "worktree", "add", "-q", "-b", "user-branch", str(linked))
    _git(repo, "config", "worktree.useRelativePaths", "true")
    before = _user_worktree_files(linked)
    assert Path(before[".git"].decode("utf-8").removeprefix("gitdir: ").strip()).is_absolute()

    git = GitRepo(repo)
    worktree = git.create_worktree("R-relative", git.head)

    assert _user_worktree_files(linked) == before
    assert git.worktree_registration(worktree) is not None
    assert GitRepo.discover(worktree).root == worktree.resolve()
    assert [path.name for path in worktree.parent.iterdir()] == ["R-relative"], "no staging left"
    assert git.status_report(worktree) == gitworkspace.StatusReport((), (), ())


def test_a_staging_name_left_by_an_earlier_process_with_the_same_pid_does_not_collide(
    repo: Path,
) -> None:
    git = GitRepo(repo)
    parent = git.worktree_parent()
    parent.mkdir()
    (parent / f"R-pid.staging-{os.getpid()}").mkdir()

    worktree = git.create_worktree("R-pid", git.head)

    assert git.worktree_registration(worktree) is not None
    assert (parent / f"R-pid.staging-{os.getpid()}").is_dir(), "an earlier process's path is kept"


@pytest.mark.parametrize("failure", ["git-error", "os-error"])
def test_a_failed_move_into_place_is_a_git_error_and_removes_the_staging_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """An antivirus or indexer holding the staging directory made ``hflow run`` print a traceback
    (an ``OSError`` the controller does not catch) and left ``<run>.staging-<pid>`` registered."""
    git = GitRepo(repo)
    real_run = subprocess.run

    def failing_move(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if list(argv[:3]) == ["git", "worktree", "move"]:
            if failure == "os-error":
                raise PermissionError(13, "The process cannot access the file")
            return subprocess.CompletedProcess(argv, 1, "", "fatal: move failed")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_move)
    with pytest.raises(GitError, match="worktree"):
        git.create_worktree("R-move-fails", git.head)
    monkeypatch.undo()

    assert list(git.worktree_parent().iterdir()) == []
    assert [Path(block["worktree"]).resolve() for block in git.worktree_blocks()] == [repo.resolve()]


def test_a_parent_that_cannot_be_created_is_a_git_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitRepo(repo)

    def denied(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - Path.mkdir shape
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(GitError, match="Access is denied"):
        git.create_worktree("R-no-parent", git.head)


def test_git_that_cannot_start_is_a_git_error(repo: Path) -> None:
    """A ``cwd`` that vanished makes ``subprocess`` raise ``OSError`` before git runs."""
    with pytest.raises(GitError, match="could not start"):
        GitRepo(repo).run("status", cwd=repo / "no-such-directory")


# -- forced configuration ----------------------------------------------------------------------


def _forced(key: str) -> str:
    return dict(_forced_config())[key]


def test_no_directory_can_ever_exist_where_the_hooks_path_points(repo: Path, tmp_path: Path) -> None:
    """``core.hooksPath`` names a path below an existing file no worker can replace.

    The empty directory it used to name lived in the user's temporary directory, where a worker
    running as the same user could plant ``post-commit`` or ``reference-transaction`` - hooks
    ``--no-verify`` does not stop. ``GIT_GRAFT_FILE`` sat in that directory too.
    """
    hooks = Path(_forced("core.hooksPath"))
    graft = Path(_base_env()["GIT_GRAFT_FILE"])
    assert graft.parent == hooks.parent
    assert hooks.parent.exists() and not hooks.parent.is_dir()
    for path in (hooks, graft):
        with pytest.raises(OSError):
            os.makedirs(path / "post-commit")

    marker = tmp_path / "hook-ran"
    for name in ("pre-commit", "post-commit", "reference-transaction", "post-checkout"):
        (repo / ".git" / "hooks" / name).write_text(
            f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", encoding="utf-8", newline="\n"
        )
    # Control: plain git runs them.
    _commit(repo, "control", {"app.txt": "value = 3\n"})
    assert marker.exists()
    marker.unlink()

    git = GitRepo(repo)
    worktree = git.create_worktree("R-hooks", git.head)
    (worktree / "app.txt").write_text("value = 4\n", encoding="utf-8", newline="\n")
    git.freeze_candidate(worktree, ["app.txt"], expected_head=git.head)
    # Git says nothing about the hooks path or the graft file: no warning, no hint.
    args = ("commit", "--allow-empty", "-q", "-m", "x")
    completed = gitworkspace._run_git(args, cwd=worktree, timeout=60)
    assert (completed.returncode, completed.stderr) == (0, "")
    assert not marker.exists()


def test_a_commit_starts_no_automatic_maintenance(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git commit`` runs ``git maintenance run --auto``; Windows cannot detach it, so it ran in
    the foreground of the freeze. Forced off, in protected ``command`` scope."""
    assert (_forced("maintenance.auto"), _forced("gc.auto")) == ("false", "0")
    trace = tmp_path / "trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    # Control: plain git starts it.
    _commit(repo, "control", {"app.txt": "value = 3\n"})
    assert '"maintenance"' in trace.read_text(encoding="utf-8")
    trace.unlink()

    git = GitRepo(repo)
    worktree = git.create_worktree("R-maintenance", git.head)
    (worktree / "app.txt").write_text("value = 4\n", encoding="utf-8", newline="\n")
    git.freeze_candidate(worktree, ["app.txt"], expected_head=git.head)
    events = trace.read_text(encoding="utf-8")
    assert '"commit"' in events and '"maintenance"' not in events
    assert git.run("config", "--show-scope", "--get", "gc.auto") == "command\t0\n"


def test_git_output_is_read_as_utf_8_whatever_the_ansi_code_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git for Windows writes paths as UTF-8; ``text=True`` decoded them with the ANSI code page."""
    import locale

    root = tmp_path / "projet-é-数据"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "first", {"app.txt": "value = 1\n"})
    name = "naïve-数据.txt"
    # A Western-European Windows: what ``subprocess`` decodes text-mode output with.
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")

    git = GitRepo.discover(root)
    assert git.root == root.resolve()
    worktree = git.create_worktree("R-utf8-é", git.head)
    assert git.worktree_registration(worktree) is not None
    (worktree / name).write_text("x\n", encoding="utf-8")
    assert git.status_report(worktree).changed == (name,)
    freeze = git.freeze_candidate(worktree, [name], expected_head=git.head)
    assert freeze.paths == (name,)
    assert git.diff_paths(git.head, freeze.candidate_commit) == [name]
    assert git.checked_out_at("refs/heads/main") == [root.resolve()]


# -- what git may reach and read -------------------------------------------------------------


def test_git_reads_no_standard_input(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git for Windows asks "Unlink of file ... failed. Should I try again? (y/n)" on a terminal."""
    git = GitRepo(repo)
    real_run = subprocess.run
    seen: list[object] = []

    def recording(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        seen.append(kwargs.get("stdin"))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    git.run("status")
    git.is_ancestor(git.head, git.head)
    assert seen == [subprocess.DEVNULL, subprocess.DEVNULL]


def test_a_partial_clone_never_fetches_a_missing_object(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_NO_LAZY_FETCH=1``: a missing blob is an error, never a fetch from a remote."""
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    assert _base_env()["GIT_NO_LAZY_FETCH"] == "1"
    _git(repo, "config", "uploadpack.allowFilter", "true")
    blob = _git(repo, "rev-parse", "HEAD~1:app.txt").strip()
    clone = tmp_path / "partial"
    _git(tmp_path, "clone", "-q", "--filter=blob:none", "--no-checkout", repo.as_uri(), str(clone))

    with pytest.raises(GitError):
        GitRepo(clone).run("cat-file", "-p", blob)
    # Control: plain git fetches it from the remote.
    monkeypatch.delenv("GIT_NO_LAZY_FETCH")
    assert _git(clone, "cat-file", "-p", blob) == "value = 1\n"
