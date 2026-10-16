"""How HFlow calls git: revisions git cannot mistake, and failures never read as answers.

Every case builds real temporary repositories with the git on PATH; nothing reads the user's
global or system git configuration. A failure git did not itself produce - a process that could
not start (Windows ``0xC0000142``), a timeout, output that cannot be decoded - is simulated by
replacing ``subprocess.run`` for the one call under test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import hflow.cleanup as cleanup
from hflow.contracts import RefusalCode, RefusedError
from hflow.gitworkspace import GitError, GitRepo, exit_status

#: What Windows reports for a process that could not initialise (STATUS_DLL_INIT_FAILED).
DLL_INIT_FAILED = 0xC0000142


@pytest.fixture(autouse=True)
def _no_ambient_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)


def _git_completed(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
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
    """Two commits; ``HEAD~1`` changed ``app.txt`` into what ``HEAD`` holds."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "first", {"app.txt": "value = 1\n"})
    _commit(root, "second", {"app.txt": "value = 2\n", "extra.txt": "x\n"})
    return root


def _failing(monkeypatch: pytest.MonkeyPatch, matches, outcome) -> list[list[str]]:  # noqa: ANN001
    """Replace ``subprocess.run`` for the git calls ``matches(argv)`` selects; the rest run for real.

    ``outcome`` is a ``CompletedProcess`` (returned with the real argv) or an exception (raised).
    Returns the argv of every intercepted call.
    """
    real_run = subprocess.run
    seen: list[list[str]] = []

    def run(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if argv and argv[0] == "git" and matches(list(argv)):
            seen.append(list(argv))
            if isinstance(outcome, BaseException):
                raise outcome
            return subprocess.CompletedProcess(argv, outcome.returncode, outcome.stdout, outcome.stderr)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    """What ``subprocess.run`` returns to HFlow's git runner, whose pipes are binary."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# -- B1: a revision is never also a file name ------------------------------------------------


def test_a_file_spelled_like_the_delivery_range_does_not_break_the_diff(repo: Path) -> None:
    """Without ``--`` git stat()s the range as a file name; spelled like one, it is "ambiguous".

    The same check is what fails with "Filename too long" in a deep Windows work tree, after the
    implementer was paid. The control shows the plain command does fail here.
    """
    first = _git(repo, "rev-parse", "HEAD~1").strip()
    second = _git(repo, "rev-parse", "HEAD").strip()
    (repo / f"{first}..{second}").write_text("a user's file\n", encoding="utf-8")

    control = _git_completed(repo, "diff", "--no-renames", "--name-only", "-z", f"{first}..{second}")
    assert control.returncode != 0 and "ambiguous argument" in control.stderr

    assert GitRepo(repo).diff_paths(first, second) == ["app.txt", "extra.txt"]


def test_a_worker_file_named_head_does_not_break_the_freeze(repo: Path) -> None:
    """A revision ``HEAD`` is ambiguous once a file is named ``HEAD``, unless git is told which."""
    git = GitRepo(repo)
    worktree = git.create_worktree("R-head-file", git.head)
    (worktree / "HEAD").write_text("a worker's file\n", encoding="utf-8")
    control = _git_completed(worktree, "rev-list", "--count", "HEAD")
    assert control.returncode != 0 and "ambiguous argument" in control.stderr

    freeze = git.freeze_candidate(worktree, ["HEAD"], expected_head=git.head)

    assert freeze.paths == ("HEAD",)
    assert git.worktree_commit(worktree) == freeze.candidate_commit
    assert git.diff_paths(git.head, freeze.candidate_commit, cwd=worktree) == ["HEAD"]


@pytest.mark.parametrize("revision", ["--git-dir", "HEAD~1..HEAD", "-x"])
def test_resolve_commit_takes_exactly_one_revision_and_never_an_option(repo: Path, revision: str) -> None:
    """``rev-parse`` echoes an unknown option and prints two lines for a range, both with exit 0."""
    with pytest.raises(GitError):
        GitRepo(repo).resolve_commit(revision)


@pytest.mark.parametrize(("base", "candidate"), [("-p", "HEAD"), ("HEAD", "--output=x"), ("", "HEAD")])
def test_a_delivery_range_spelled_like_an_option_is_refused(repo: Path, base: str, candidate: str) -> None:
    with pytest.raises(GitError):
        GitRepo(repo).diff_paths(base, candidate)
    assert not (repo / "x").exists()


# -- B2: only "not a git repository" is a scope refusal -----------------------------------------


def test_a_directory_outside_any_repository_is_a_scope_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(RefusedError) as refused:
        GitRepo.discover(plain)
    assert refused.value.code is RefusalCode.SCOPE_VIOLATION
    assert "not inside a git repository" in str(refused.value)


@pytest.mark.parametrize(
    ("returncode", "stderr", "named"),
    [
        (DLL_INIT_FAILED, "", "0xC0000142"),
        (128, "fatal: detected dubious ownership in repository at '/x'\n", "dubious ownership"),
        (-9, "", "signal 9"),
    ],
)
def test_git_that_fails_for_another_reason_is_a_git_error_not_a_scope_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str, named: str
) -> None:
    """Under load git can fail to start at all (empty stderr); that is not "no repository here"."""
    seen = _failing(
        monkeypatch, lambda argv: "--show-toplevel" in argv, _completed(returncode, b"", stderr.encode())
    )
    with pytest.raises(GitError, match=named):
        GitRepo.discover(repo)
    assert seen, "the discovery call was the one that failed"


def test_not_a_git_repository_from_git_itself_is_the_scope_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "fatal: not a git repository (or any of the parent directories): .git\n"
    _failing(monkeypatch, lambda argv: "--show-toplevel" in argv, _completed(128, b"", message.encode()))
    with pytest.raises(RefusedError) as refused:
        GitRepo.discover(repo)
    assert refused.value.code is RefusalCode.SCOPE_VIOLATION


def test_exit_status_names_windows_codes_in_hex() -> None:
    assert exit_status(DLL_INIT_FAILED) == "0xC0000142"
    assert exit_status(128) == "128"
    assert exit_status(-15) == "signal 15"


# -- S1: one git call opens a repository ------------------------------------------------------


def test_discovery_and_construction_each_take_one_git_call(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    common = (repo / ".git").resolve()
    (repo / "sub" / "deep").mkdir(parents=True)
    linked = repo.parent / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
    real_run = subprocess.run
    counted: list[list[str]] = []

    def counting(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if argv and argv[0] == "git":
            counted.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    for where, root in ((repo, repo), (repo / "sub" / "deep", repo), (linked, linked)):
        counted.clear()
        found = GitRepo.discover(where)
        assert len(counted) == 1, counted
        assert found.root == root.resolve()
        assert found.head == head
        assert Path(found.absolute_common_dir) == common
        counted.clear()
        opened = GitRepo(root)
        assert len(counted) == 1, counted
        assert opened.head == head and Path(opened.absolute_common_dir) == common


def test_a_repository_with_no_commit_yet_still_cannot_be_opened(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    with pytest.raises(GitError):
        GitRepo(empty)
    with pytest.raises(GitError):
        GitRepo.discover(empty)


# -- B3: a failed ref read is never "the ref is gone" -----------------------------------------


def test_ref_target_is_none_only_for_a_ref_that_does_not_exist(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = GitRepo(repo)
    assert git.ref_target("refs/heads/main") == git.head
    assert git.ref_target("refs/heads/absent") is None
    _failing(
        monkeypatch,
        lambda argv: "--quiet" in argv and argv[-1] == "refs/heads/main",
        _completed(DLL_INIT_FAILED),
    )
    with pytest.raises(GitError, match="0xC0000142"):
        git.ref_target("refs/heads/main")


@pytest.mark.parametrize("stdout", [b"", b"not-an-object-id\n"])
def test_ref_target_refuses_an_answer_that_is_not_an_object_id(
    repo: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    git = GitRepo(repo)
    _failing(monkeypatch, lambda argv: "--quiet" in argv, _completed(0, stdout))
    with pytest.raises(GitError, match="object id"):
        git.ref_target("refs/heads/main")


# -- B4: every reader converts a timeout and undecodable output --------------------------------


def _readers(git: GitRepo, repo: Path) -> dict[str, tuple]:
    """``name -> (call, argv marker)`` for every reader that used to call ``subprocess`` itself."""
    common = str((repo / ".git").resolve())
    return {
        "discover": (lambda: GitRepo.discover(repo), "--show-toplevel"),
        "construct": (lambda: GitRepo(repo), "--git-common-dir"),
        "resolve_commit": (lambda: git.resolve_commit("HEAD"), "HEAD^{commit}"),
        "commit_exists": (lambda: git.commit_exists(git.head), "cat-file"),
        "ref_target": (lambda: git.ref_target("refs/heads/main"), "--quiet"),
        "registration": (lambda: cleanup.git_registration_exists(common, repo), "--git-dir"),
    }


@pytest.mark.parametrize(
    "reader", ["discover", "construct", "resolve_commit", "commit_exists", "ref_target", "registration"]
)
@pytest.mark.parametrize(
    ("how", "named"),
    [("timeout", "timed out"), ("cannot-start", "could not start"), ("not-text", "could not be decoded")],
)
def test_every_git_reader_turns_an_unreadable_answer_into_a_git_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, reader: str, how: str, named: str
) -> None:
    """Never ``TimeoutExpired``, ``OSError`` or ``UnicodeDecodeError``, and never "empty".

    Each of those escaped the ``except (GitError, RefusedError, OSError)`` handlers around these
    calls and left a run, a cleanup or an integration with a traceback instead of a recorded state.
    Every reader goes through the one binary-pipe runner, which decodes standard output strictly.
    """
    git = GitRepo(repo)
    call, marker = _readers(git, repo)[reader]
    outcome = {
        "timeout": subprocess.TimeoutExpired(["git"], 30),
        "cannot-start": FileNotFoundError(2, "The system cannot find the file specified"),
        "not-text": _completed(0, b"\xff\xfe\n"),
    }[how]
    seen = _failing(monkeypatch, lambda argv: marker in argv, outcome)
    with pytest.raises(GitError, match=named):
        call()
    assert seen


def test_an_undecodable_stderr_does_not_fail_a_call_that_succeeded(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standard error is only quoted in a message; standard output is the answer."""
    git = GitRepo(repo)
    answer = (git.head + "\n").encode()
    _failing(monkeypatch, lambda argv: "--quiet" in argv, _completed(0, answer, b"\xff\xfe"))
    assert git.ref_target("refs/heads/main") == git.head


def test_git_output_keeps_a_carriage_return_as_git_printed_it(repo: Path) -> None:
    """Text-mode pipes read ``a\\r\\nb`` as ``a\\nb``: a value changed only in that would look unchanged."""
    git = GitRepo(repo)
    with (repo / ".git" / "config").open("ab") as handle:
        # A lone CR (not before LF) is part of the value; backslash-n is git's escape for a newline.
        handle.write(b"[hflowprobe]\n\tvalue = a\r\\nb\n")
    assert git.run("config", "--get", "hflowprobe.value") == "a\r\nb\n"
    assert git.query("config", "--get", "hflowprobe.value").stdout == "a\r\nb\n"
