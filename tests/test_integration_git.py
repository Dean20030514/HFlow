"""Git primitives for controlled integration, against real temporary repositories.

Every case builds its own repository with the git on PATH; nothing reads the user's global or
system git configuration (``GIT_CONFIG_GLOBAL`` points at a missing file, ``GIT_CONFIG_NOSYSTEM``
is set). States that git itself creates (a conflicted rebase, a bisect, ``rebase --update-refs``)
are created by git; the remaining ones are written into the worktree's git directory by hand.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hflow.gitworkspace import (
    CANDIDATE_AUTHOR_EMAIL,
    CANDIDATE_AUTHOR_NAME,
    MIN_INTEGRATION_GIT,
    GitError,
    GitRepo,
    MergeTreeResult,
    parse_git_version,
)

SPACE_PATH = "a b.txt"
NON_ASCII_PATH = "ünï.txt"


def _installed_git_version() -> tuple[int, int, int]:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git command
        ["git", "version"], capture_output=True, text=True, check=True
    )
    return parse_git_version(completed.stdout)


pytestmark = pytest.mark.skipif(
    _installed_git_version() < MIN_INTEGRATION_GIT,
    reason=f"integration primitives need Git {MIN_INTEGRATION_GIT}",
)


@pytest.fixture(autouse=True)
def _no_ambient_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)


def _git_completed(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
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
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr!r}")
    return completed.stdout.decode("utf-8")


def _commit(cwd: Path, message: str, files: dict[str, str | None]) -> str:
    """Write (or, for ``None``, delete) ``files`` and commit everything; the new HEAD."""
    for name, content in files.items():
        target = cwd / name
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").strip()


def _admin_dir(worktree: Path) -> Path:
    return Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-dir").strip())


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base", {"README.md": "base\n", "shared.txt": "one\ntwo\nthree\n"})
    return root


def _diverge(repo: Path, ours: dict[str, str | None], theirs: dict[str, str | None]) -> tuple[str, str, str]:
    """``(base, ours, theirs)``: ``theirs`` on a side branch, ``ours`` on main, both from HEAD."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "theirs", theirs)
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "ours", ours)
    return base, main, side


# -- version ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("git version 2.56.0.windows.2\n", (2, 56, 0)),
        ("git version 2.39.3 (Apple Git-146)\n", (2, 39, 3)),
        ("git version 2.45.0.rc1", (2, 45, 0)),
        ("git version 2.40.GIT", (2, 40, 0)),
        ("git version 3.0.12", (3, 0, 12)),
    ],
)
def test_parse_git_version_reads_vendor_strings(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_git_version(text) == expected


@pytest.mark.parametrize("text", ["", "git version", "version 2.40.0", "git version x.y.z", "git version 2"])
def test_parse_git_version_refuses_what_it_cannot_read(text: str) -> None:
    with pytest.raises(GitError, match="unrecognised"):
        parse_git_version(text)


def test_git_version_reads_the_running_git_and_compares_with_the_minimum(repo: Path) -> None:
    version = GitRepo(repo).git_version()
    assert version == _installed_git_version()
    assert len(version) == 3 and all(isinstance(part, int) for part in version)
    assert version >= MIN_INTEGRATION_GIT
    assert (2, 39, 9) < MIN_INTEGRATION_GIT <= (2, 40, 0)


# -- ancestry, trees, branch names --------------------------------------------------------------


def test_is_ancestor_true_false_and_bad_object(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    child = _commit(repo, "child", {"README.md": "child\n"})
    git = GitRepo(repo)
    assert git.is_ancestor(base, child) is True
    assert git.is_ancestor(child, child) is True
    assert git.is_ancestor(child, base) is False
    with pytest.raises(GitError, match="merge-base"):
        git.is_ancestor("0123456789abcdef0123456789abcdef01234567", child)
    with pytest.raises(GitError, match="not a revision"):
        git.is_ancestor("--all", child)


def test_tree_of_names_the_commit_tree(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    git = GitRepo(repo)
    assert git.tree_of(head) == _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    assert git.tree_of("main") == git.tree_of(head)
    with pytest.raises(GitError):
        git.tree_of("0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(GitError, match="not a revision"):
        git.tree_of("-x")


@pytest.mark.parametrize("name", ["main", "feature/x", "ünï", "heads/x"])
def test_branch_ref_accepts_a_plain_branch_name(repo: Path, name: str) -> None:
    assert GitRepo(repo).branch_ref(name) == f"refs/heads/{name}"


@pytest.mark.parametrize(
    "name", ["-x", "refs/heads/x", "a..b", "@{-1}", "HEAD", "", "@", "x@{u}", "x.lock", "a b", "a\nb"]
)
def test_branch_ref_refuses_anything_but_a_plain_branch_name(repo: Path, name: str) -> None:
    # A previous branch exists, so ``check-ref-format --branch @{-1}`` would expand to it.
    _git(repo, "checkout", "-q", "-b", "previous")
    _git(repo, "checkout", "-q", "main")
    with pytest.raises(GitError, match="refusing branch name"):
        GitRepo(repo).branch_ref(name)


# -- merge-tree ---------------------------------------------------------------------------------


def test_merge_tree_clean_merge_holds_both_sides(repo: Path) -> None:
    base, ours, theirs = _diverge(
        repo,
        ours={"README.md": "ours\n"},
        theirs={"shared.txt": "one\nTHEIRS\nthree\n", "added.txt": "new\n"},
    )
    index_before = (repo / ".git" / "index").read_bytes()
    result = GitRepo(repo).merge_tree(base=base, ours=ours, theirs=theirs)
    assert result == MergeTreeResult(clean=True, tree=result.tree, conflicts=())
    assert _git(repo, "cat-file", "-t", result.tree).strip() == "tree"
    assert _git(repo, "show", f"{result.tree}:README.md") == "ours\n"
    assert _git(repo, "show", f"{result.tree}:shared.txt") == "one\nTHEIRS\nthree\n"
    assert _git(repo, "show", f"{result.tree}:added.txt") == "new\n"
    # Objects only: the checkout, its index and HEAD are as they were.
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD").strip() == ours
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_merge_tree_same_line_conflict_lists_paths_with_space_and_non_ascii(repo: Path) -> None:
    _commit(repo, "more files", {SPACE_PATH: "one\ntwo\nthree\n", NON_ASCII_PATH: "x\n", "calm.txt": "c\n"})
    base, ours, theirs = _diverge(
        repo,
        ours={SPACE_PATH: "one\nOURS\nthree\n", NON_ASCII_PATH: "ours\n"},
        theirs={SPACE_PATH: "one\nTHEIRS\nthree\n", NON_ASCII_PATH: "theirs\n", "calm.txt": "c2\n"},
    )
    result = GitRepo(repo).merge_tree(base=base, ours=ours, theirs=theirs)
    assert result.clean is False
    assert result.conflicts == (SPACE_PATH, NON_ASCII_PATH)
    # Git still prints a tree for a conflicted merge; it holds conflict markers.
    assert _git(repo, "cat-file", "-t", result.tree).strip() == "tree"
    assert "<<<<<<<" in _git(repo, "show", f"{result.tree}:{SPACE_PATH}")
    assert _git(repo, "status", "--porcelain") == ""


def test_merge_tree_add_add_conflict(repo: Path) -> None:
    base, ours, theirs = _diverge(repo, ours={"new.txt": "ours\n"}, theirs={"new.txt": "theirs\n"})
    result = GitRepo(repo).merge_tree(base=base, ours=ours, theirs=theirs)
    assert (result.clean, result.conflicts) == (False, ("new.txt",))


@pytest.mark.parametrize("deleted_by", ["ours", "theirs"])
def test_merge_tree_delete_modify_conflict(repo: Path, deleted_by: str) -> None:
    delete: dict[str, str | None] = {"shared.txt": None}
    modify: dict[str, str | None] = {"shared.txt": "one\nchanged\nthree\n"}
    ours_files, theirs_files = (delete, modify) if deleted_by == "ours" else (modify, delete)
    base, ours, theirs = _diverge(repo, ours=ours_files, theirs=theirs_files)
    result = GitRepo(repo).merge_tree(base=base, ours=ours, theirs=theirs)
    assert (result.clean, result.conflicts) == (False, ("shared.txt",))


def test_merge_tree_with_the_target_at_the_base_is_the_candidate_tree(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    theirs = _commit(repo, "theirs", {"README.md": "theirs\n"})
    result = GitRepo(repo).merge_tree(base=base, ours=base, theirs=theirs)
    assert result.clean and result.tree == _git(repo, "rev-parse", f"{theirs}^{{tree}}").strip()


def test_merge_tree_errors_are_not_conflicts(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    git = GitRepo(repo)
    with pytest.raises(GitError, match="merge-tree"):
        git.merge_tree(base=head, ours=head, theirs="0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(GitError, match="not a revision"):
        git.merge_tree(base=head, ours="--quiet", theirs=head)
    with pytest.raises(GitError, match="not a revision"):
        git.merge_tree(base="", ours=head, theirs=head)


# -- commit-tree --------------------------------------------------------------------------------


def test_commit_tree_writes_parents_identity_and_message_verbatim(repo: Path) -> None:
    base, ours, theirs = _diverge(repo, ours={"README.md": "ours\n"}, theirs={"other.txt": "o\n"})
    git = GitRepo(repo)
    tree = git.merge_tree(base=base, ours=ours, theirs=theirs).tree
    message = "hflow: integrate run R-1\n\nIntegration: I-1\nCandidate: c\n"
    commit = git.commit_tree(tree, parents=[ours], message=message)
    assert _git(repo, "rev-parse", f"{commit}^{{tree}}").strip() == tree
    assert _git(repo, "rev-list", "--parents", "-n", "1", commit).split() == [commit, ours]
    identity = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", commit).strip()
    expected = f"{CANDIDATE_AUTHOR_NAME} <{CANDIDATE_AUTHOR_EMAIL}>"
    assert identity == f"{expected}|{expected}"
    raw = _git_completed(repo, "cat-file", "commit", commit).stdout
    assert raw.endswith(message.encode("utf-8"))
    assert b"\r" not in raw and b"\nencoding " not in raw
    # No ref moved and the checkout is untouched.
    assert _git(repo, "rev-parse", "HEAD").strip() == ours
    assert _git(repo, "status", "--porcelain") == ""

    two = git.commit_tree(tree, parents=[ours, theirs], message="two parents")
    assert _git(repo, "rev-list", "--parents", "-n", "1", two).split() == [two, ours, theirs]
    root = git.commit_tree(tree, parents=[], message="root")
    assert _git(repo, "rev-list", "--parents", "-n", "1", root).split() == [root]


@pytest.mark.parametrize(
    ("parents", "message", "match"),
    [
        (["HEAD"], "", "empty message"),
        (["HEAD"], " \n\t", "empty message"),
        (["HEAD"], "a\0b", "NUL"),
        ("HEAD", "m", "sequence"),
        (["HEAD", "HEAD"], "m", "repeated parent"),
        (["-p"], "m", "not a revision"),
    ],
)
def test_commit_tree_refusals(repo: Path, parents: object, message: str, match: str) -> None:
    git = GitRepo(repo)
    tree = git.tree_of("HEAD")
    with pytest.raises(GitError, match=match):
        git.commit_tree(tree, parents=parents, message=message)  # type: ignore[arg-type]


def test_commit_tree_refuses_a_bad_tree(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(GitError, match="not a revision"):
        GitRepo(repo).commit_tree("-x", parents=[head], message="m")
    with pytest.raises(GitError, match="commit-tree"):
        GitRepo(repo).commit_tree(head, parents=[head], message="m")  # a commit is not a tree


# -- update-ref compare-and-set -------------------------------------------------------------------


def test_update_ref_cas_moves_a_branch_and_records_the_reason(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "branch", "target", old)
    new = _commit(repo, "next", {"README.md": "next\n"})
    GitRepo(repo).update_ref_cas("refs/heads/target", new, old, reason="hflow integrate I-1")
    assert _git(repo, "rev-parse", "refs/heads/target").strip() == new
    assert _git(repo, "reflog", "-1", "--format=%gs", "refs/heads/target").strip() == "hflow integrate I-1"


def test_update_ref_cas_refuses_a_stale_expected_old_and_leaves_the_ref(repo: Path) -> None:
    first = _git(repo, "rev-parse", "HEAD").strip()
    second = _commit(repo, "second", {"README.md": "2\n"})
    third = _commit(repo, "third", {"README.md": "3\n"})
    _git(repo, "branch", "target", second)
    with pytest.raises(GitError, match="update-ref"):
        GitRepo(repo).update_ref_cas("refs/heads/target", third, first, reason="stale")
    assert _git(repo, "rev-parse", "refs/heads/target").strip() == second


def test_update_ref_cas_never_creates_a_branch(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD").strip()
    new = _commit(repo, "next", {"README.md": "next\n"})
    with pytest.raises(GitError):
        GitRepo(repo).update_ref_cas("refs/heads/absent", new, old, reason="create?")
    assert _git_completed(repo, "rev-parse", "--verify", "-q", "refs/heads/absent").returncode != 0


@pytest.mark.parametrize("ref", ["refs/tags/target", "refs/hflow/x", "HEAD", "target", "refs/heads/", "refs/heads/a..b"])
def test_update_ref_cas_refuses_refs_outside_refs_heads(repo: Path, ref: str) -> None:
    old = _git(repo, "rev-parse", "HEAD").strip()
    new = _commit(repo, "next", {"README.md": "next\n"})
    _git(repo, "tag", "target", old)
    with pytest.raises(GitError, match="refusing"):
        GitRepo(repo).update_ref_cas(ref, new, old, reason="r")
    assert _git(repo, "rev-parse", "refs/tags/target").strip() == old


def test_update_ref_cas_refuses_short_and_zero_ids_and_bad_reasons(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD").strip()
    new = _commit(repo, "next", {"README.md": "next\n"})
    _git(repo, "branch", "target", old)
    git = GitRepo(repo)
    for new_value, old_value in (
        (new[:12], old),
        (new, old[:7]),
        (new, ""),
        ("0" * 40, old),
        (new, "0" * 40),
        (new.upper(), old),
        ("main", old),
    ):
        with pytest.raises(GitError, match="not a full object id"):
            git.update_ref_cas("refs/heads/target", new_value, old_value, reason="r")
    for reason in ("", "  ", "two\nlines"):
        with pytest.raises(GitError, match="reason"):
            git.update_ref_cas("refs/heads/target", new, old, reason=reason)
    assert _git(repo, "rev-parse", "refs/heads/target").strip() == old


def test_update_ref_cas_refuses_a_symbolic_branch(repo: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "branch", "target", old)
    new = _commit(repo, "next", {"README.md": "next\n"})
    _git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/target")
    with pytest.raises(GitError, match="symbolic ref"):
        GitRepo(repo).update_ref_cas("refs/heads/alias", new, old, reason="r")
    assert _git(repo, "symbolic-ref", "refs/heads/alias").strip() == "refs/heads/target"
    assert _git(repo, "rev-parse", "refs/heads/target").strip() == old


# -- checked out --------------------------------------------------------------------------------


def test_checked_out_at_names_the_main_worktree_branch(repo: Path) -> None:
    git = GitRepo(repo)
    assert git.checked_out_at("refs/heads/main") == [repo.resolve()]
    _git(repo, "branch", "idle")
    assert git.checked_out_at("refs/heads/idle") == []


def test_checked_out_at_names_a_linked_worktree_with_an_unusual_path(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked ü wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(linked))
    git = GitRepo(repo)
    assert git.checked_out_at("refs/heads/feat") == [linked.resolve()]
    assert git.checked_out_at("refs/heads/main") == [repo.resolve()]
    # Asked from inside the linked worktree, the answer is the same.
    assert GitRepo(linked).checked_out_at("refs/heads/feat") == [linked.resolve()]


def test_checked_out_at_does_not_name_a_detached_worktree(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "branch", "parked", head)
    _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "detached"), "parked")
    git = GitRepo(repo)
    assert git.checked_out_at("refs/heads/parked") == []
    assert git.checked_out_at("refs/heads/main") == [repo.resolve()]


def test_checked_out_at_lists_every_worktree_in_git_order(repo: Path, tmp_path: Path) -> None:
    second = tmp_path / "second"
    _git(repo, "worktree", "add", "-q", "--force", str(second), "main")
    assert GitRepo(repo).checked_out_at("refs/heads/main") == [repo.resolve(), second.resolve()]


def test_checked_out_at_names_a_linked_worktree_in_a_conflicted_rebase(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "rebasing"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(linked))
    _commit(linked, "feat side", {"shared.txt": "feat\n"})
    _commit(repo, "main side", {"shared.txt": "main\n"})
    rebase = _git_completed(linked, "rebase", "main")
    assert rebase.returncode != 0, "the rebase must stop on its conflict"
    git = GitRepo(repo)
    block = git.worktree_registration(linked)
    assert block is not None and "detached" in block and "branch" not in block
    assert git.checked_out_at("refs/heads/feat") == [linked.resolve()]
    assert GitRepo(linked).checked_out_at("refs/heads/feat") == [linked.resolve()]


def test_checked_out_at_names_a_linked_worktree_in_a_bisect(repo: Path, tmp_path: Path) -> None:
    good = _git(repo, "rev-parse", "HEAD").strip()
    linked = tmp_path / "bisecting"
    _git(repo, "worktree", "add", "-q", "-b", "bis", str(linked))
    for number in range(3):
        bad = _commit(linked, f"step {number}", {"README.md": f"{number}\n"})
    _git(linked, "bisect", "start", bad, good)
    git = GitRepo(repo)
    block = git.worktree_registration(linked)
    assert block is not None and "branch" not in block
    assert git.checked_out_at("refs/heads/bis") == [linked.resolve()]
    _git(linked, "bisect", "reset")
    assert git.checked_out_at("refs/heads/bis") == [linked.resolve()]  # back on the branch


def test_checked_out_at_names_branches_a_rebase_will_update(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "stack"
    _git(repo, "worktree", "add", "-q", "-b", "top", str(linked))
    _commit(linked, "lower", {"shared.txt": "lower\n"})
    _git(linked, "branch", "mid")
    _commit(linked, "upper", {"upper.txt": "u\n"})
    _commit(repo, "main side", {"shared.txt": "main\n"})
    rebase = _git_completed(linked, "rebase", "--update-refs", "main")
    assert rebase.returncode != 0, "the rebase must stop on its conflict"
    git = GitRepo(repo)
    assert git.checked_out_at("refs/heads/mid") == [linked.resolve()]
    assert git.checked_out_at("refs/heads/top") == [linked.resolve()]
    assert git.checked_out_at("refs/heads/main") == [repo.resolve()]


def test_checked_out_at_reads_hand_written_operation_state(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    linked = tmp_path / "detached"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), head)
    admin = _admin_dir(linked)
    git = GitRepo(repo)
    ref = "refs/heads/sim"
    assert git.checked_out_at(ref) == []

    (admin / "rebase-merge").mkdir()
    (admin / "rebase-merge" / "head-name").write_bytes(b"refs/heads/sim\n")
    assert git.checked_out_at(ref) == [linked.resolve()]
    (admin / "rebase-merge" / "head-name").write_bytes(b"detached HEAD\n")
    assert git.checked_out_at(ref) == []
    (admin / "rebase-merge" / "head-name").unlink()
    (admin / "rebase-merge").rmdir()

    # ``rebase-apply`` is a rebase unless ``applying`` marks it as ``git am``.
    (admin / "rebase-apply").mkdir()
    (admin / "rebase-apply" / "head-name").write_bytes(b"refs/heads/sim\n")
    assert git.checked_out_at(ref) == [linked.resolve()]
    (admin / "rebase-apply" / "applying").write_bytes(b"")
    assert git.checked_out_at(ref) == []
    for name in ("applying", "head-name"):
        (admin / "rebase-apply" / name).unlink()
    (admin / "rebase-apply").rmdir()

    # A bisect counts only while ``BISECT_LOG`` exists; ``BISECT_START`` holds a bare branch name.
    (admin / "BISECT_START").write_bytes(b"sim\n")
    assert git.checked_out_at(ref) == []
    (admin / "BISECT_LOG").write_bytes(b"")
    assert git.checked_out_at(ref) == [linked.resolve()]


def test_checked_out_at_reads_main_worktree_state_from_the_common_dir(repo: Path) -> None:
    common = repo / ".git"
    (common / "rebase-merge").mkdir()
    (common / "rebase-merge" / "head-name").write_bytes(b"refs/heads/other\n")
    git = GitRepo(repo)
    assert git.checked_out_at("refs/heads/other") == [repo.resolve()]
    (common / "rebase-merge" / "head-name").write_bytes(b"refs/heads/main\n")
    assert git.checked_out_at("refs/heads/main") == [repo.resolve()]  # listed once


# -- HFlow-owned refs -----------------------------------------------------------------------------


def test_integration_ref_is_built_from_validated_ids(repo: Path) -> None:
    git = GitRepo(repo)
    assert git.integration_ref("R-1", "I_2") == "refs/hflow/integrations/R-1/I_2"
    for run_id, integration_id in (("", "I1"), ("R1", ""), ("R/1", "I1"), ("R1", ".."), ("R 1", "I1"), ("R1", "I.lock")):
        with pytest.raises(GitError, match="invalid"):
            git.integration_ref(run_id, integration_id)


def test_ensure_ref_creates_reuses_and_refuses_a_conflict(repo: Path) -> None:
    first = _git(repo, "rev-parse", "HEAD").strip()
    second = _commit(repo, "second", {"README.md": "2\n"})
    git = GitRepo(repo)
    ref = git.integration_ref("R1", "I1")
    assert git.ensure_ref(ref, first) == "created"
    assert git.ref_target(ref) == first
    assert git.ensure_ref(ref, first) == "reused"
    with pytest.raises(GitError, match="already exists"):
        git.ensure_ref(ref, second)
    assert git.ref_target(ref) == first
    for bad_ref in ("HEAD", "ORIG_HEAD", "hflow/x", ""):
        with pytest.raises(GitError, match="refs/"):
            git.ensure_ref(bad_ref, first)
    with pytest.raises(GitError, match="full object id"):
        git.ensure_ref(git.integration_ref("R1", "I2"), first[:12])
    candidate = git.candidate_ref("R1", "A1")
    assert git.ensure_candidate_ref(candidate, second) == "created"
    assert git.ensure_candidate_ref(candidate, second) == "reused"
    with pytest.raises(GitError, match="already exists"):
        git.ensure_candidate_ref(candidate, first)
