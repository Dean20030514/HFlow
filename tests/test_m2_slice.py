"""M2 offline slice: isolated worktree -> small change -> frozen candidate -> acceptance.

This is the first end-to-end run that produces a *deliverable* rather than a transport
result:

```text
fixed base commit
  -> isolated Git worktree (the user's checkout is never written to)
  -> a fake driver applies one small, declared change
  -> the controller freezes the candidate as a real Git commit
  -> the approved check runs against that frozen candidate
  -> the controller issues a local delivery receipt bound to the candidate
```

The sample project is created fresh in a temp directory, so nothing here depends on a
network, a credential, a model, or the HFlow repository itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryState,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller, inspect_run
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.gitworkspace import IGNORED_ARTIFACT_ALLOWLIST, GitError, GitRepo, GitStatusParseError
from hflow.store import Store
from hflow.verify import CheckRunners
from hflow.workspace import dsh_context_paths, matches_pattern

SCRIPT_SOURCE = '''"""Tiny text utilities used as the M2 sample project."""


def normalise(text):
    """Return a normalised form of ``text``."""
    parts = text.split()
    return " ".join(parts)


def slugify(text):
    """Return a URL-friendly slug for ``text``."""
    return "-".join(text.split()).lower()
'''

TEST_SOURCE = '''import pytest

from textkit import normalise, slugify


def test_collapses_whitespace():
    assert normalise("  a   b ") == "a b"


def test_slugify_lowercases_and_joins():
    assert slugify("Hello World") == "hello-world"


def test_none_input_is_rejected_cleanly():
    """The declared bug: None reaches str.split() and raises AttributeError."""
    with pytest.raises(TypeError):
        normalise(None)
'''

FIXED_SOURCE = '''"""Tiny text utilities used as the M2 sample project."""


def normalise(text):
    """Return a normalised form of ``text``."""
    if text is None:
        raise TypeError("normalise() expects str, got None")
    parts = text.split()
    return " ".join(parts)


def slugify(text):
    """Return a URL-friendly slug for ``text``."""
    return "-".join(text.split()).lower()
'''


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "Sample Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Sample Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """A real one-commit Git project with a genuine bug in it."""
    repo = tmp_path / "sample"
    (repo / "src" / "textkit").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "textkit" / "__init__.py").write_text(SCRIPT_SOURCE, encoding="utf-8")
    (repo / "tests" / "test_textkit.py").write_text(TEST_SOURCE, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "sample project with an empty-input bug")
    return repo


def _project(repo: Path) -> ProjectConfig:
    """The approved check runs the sample project's own test command in the worktree."""
    return ProjectConfig(
        project_id="sample-project",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[sys.executable, "-m", "pytest", "-q", "tests"],
                timeout_seconds=300,
            )
        ],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )


def _task(repo: Path, base_commit: str, *, mode: str = "worktree") -> TaskSpec:
    return TaskSpec(
        task_id="T-m2-none-input",
        revision=1,
        goal="Reject None input in normalise() with a clear TypeError instead of AttributeError",
        acceptance=[
            AcceptanceCriterion(
                id="AC-1", statement="None input raises TypeError", check_ids=["unit"]
            ),
            AcceptanceCriterion(
                id="AC-2", statement="existing whitespace and slug behaviour is unchanged", check_ids=["unit"]
            ),
        ],
        scope=Scope(
            write_allow=["src/textkit/__init__.py"],
            write_deny=[".git/**"],
        ),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:python-stdlib-only",
            reason="standard library only; no new component",
        ),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
        workspace=WorkspaceSpec(mode=mode, base_commit=base_commit, keep=True),
    )


def _controller(store: Store, repo: Path, data_dir: Path) -> Controller:
    script = FakeScript(write_plan={"src/textkit/__init__.py": FIXED_SOURCE}, agent_turns=1)
    return Controller(
        store,
        FakeDriver(repo, script),
        controller_build="m2-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=data_dir,
        # The offline driver scripts its own change, so it needs no write permission. This slice
        # is about the worktree and the frozen candidate, not about the production write gate -
        # which is stated here rather than inferred away (a real change needs both a worktree and
        # HFLOW_ALLOW_WRITES; see tests/test_dispatch_gates.py).
        production=False,
    )


# --------------------------------------------------------------------------
# the slice
# --------------------------------------------------------------------------


def test_base_commit_already_fails_the_acceptance_check(sample_repo: Path, tmp_path: Path) -> None:
    """The sample must actually be broken at the base commit, or the slice proves nothing."""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=str(sample_repo),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode != 0
    assert "test_none_input_is_rejected_cleanly" in completed.stdout


def test_m2_slice_produces_a_frozen_git_candidate_and_a_receipt(
    sample_repo: Path, tmp_path: Path
) -> None:
    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    repo_before = GitRepo.discover(sample_repo)
    user_fingerprint_before = repo_before.user_change_fingerprint()
    controller = _controller(store, sample_repo, tmp_path / "data")
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(sample_repo, base_commit),
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )

        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert outcome.delivery_state is DeliveryState.LOCAL_CANDIDATE
        receipt = outcome.receipt
        assert receipt is not None

        # --- a real Git identity for the candidate, not a fingerprint standing in for one
        assert len(receipt.candidate.git_commit) == 40
        assert len(receipt.candidate.git_tree) == 40
        assert receipt.candidate.base_commit == base_commit
        assert receipt.candidate.git_commit != base_commit, "the candidate is a new commit"
        assert receipt.candidate_paths == ["src/textkit/__init__.py"]
        assert receipt.candidate.fingerprint.startswith("sha256:")
        assert receipt.candidate.fingerprint != receipt.candidate.git_tree, (
            "fingerprint and Git tree are different identities and must never be conflated"
        )

        worktree = Path(receipt.candidate.worktree)
        assert worktree.exists()
        assert worktree != sample_repo
        assert _git(worktree, "rev-parse", "HEAD").strip() == receipt.candidate.git_commit
        assert _git(worktree, "rev-parse", "HEAD^{tree}").strip() == receipt.candidate.git_tree
        assert "if text is None" in (worktree / "src" / "textkit" / "__init__.py").read_text(
            encoding="utf-8"
        )
        # The tracked candidate is frozen clean. A check may leave *ignored* byproducts
        # (bytecode caches); those are not candidate drift, and they are listed separately.
        assert _git(worktree, "status", "--porcelain", "--untracked-files=no").strip() == ""
        ignored = GitRepo.discover(worktree).ignored_artifacts(worktree)
        assert ignored, "the check did leave a cache; the policy must still name every entry"
        assert all(matches_pattern(entry, list(IGNORED_ARTIFACT_ALLOWLIST)) for entry in ignored), (
            f"unexpected ignored artifacts in the candidate worktree: {ignored}"
        )
        assert "?" not in _git(worktree, "status", "--porcelain", "--untracked-files=no")

        # --- verification ran on the frozen candidate
        assert receipt.verification.status == "passed"
        assert receipt.verification.evidence_ids
        evidence = store.evidence_for(outcome.run_id, "verification")
        assert evidence[0]["exit_code"] == 0
        assert receipt.delivery_state is DeliveryState.LOCAL_CANDIDATE

        # --- the user's own checkout was not touched
        repo_after = GitRepo.discover(sample_repo)
        assert repo_after.user_change_fingerprint() == user_fingerprint_before
        assert repo_after.head == base_commit, "the user's branch/HEAD did not move"
        assert _git(sample_repo, "status", "--porcelain").strip() == ""
        assert "if text is None" not in (sample_repo / "src" / "textkit" / "__init__.py").read_text(
            encoding="utf-8"
        ), "the fix must live in the worktree, not in the user's checkout"

        # --- the receipt is readable back from SQLite, not just from memory
        inspection = inspect_run(store, outcome.run_id, project_root=sample_repo)
        assert inspection.receipt is not None
        assert inspection.receipt.candidate.git_commit == receipt.candidate.git_commit
        assert inspection.run.workspace_matches_receipt is True
    finally:
        store.close()


def test_candidate_survives_but_check_failure_blocks_delivery(
    sample_repo: Path, tmp_path: Path
) -> None:
    """With the check still failing on the candidate, nothing is delivered - and kept."""
    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    # A "fix" that does not fix anything: the check must catch it.
    script = FakeScript(
        write_plan={"src/textkit/__init__.py": SCRIPT_SOURCE + "\n# touched but not fixed\n"},
        agent_turns=1,
    )
    controller = Controller(
        store,
        FakeDriver(sample_repo, script),
        controller_build="m2-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(sample_repo, base_commit),
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )

        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.receipt is None, "a failed check must not produce a delivery receipt"
        assert "acceptance is not met" in (outcome.block_reason or "")

        # The worktree is preserved for inspection, with the candidate still frozen in it.
        repo = GitRepo.discover(sample_repo)
        worktrees = [
            Path(line.split(" ", 1)[1])
            for line in repo.worktree_list()
            if line.startswith("worktree ")
        ]
        assert len(worktrees) >= 2, "the failed candidate must be kept, not cleaned up"
        failed_worktree = worktrees[-1]
        assert failed_worktree.exists() and failed_worktree.is_dir()
        assert _git(failed_worktree, "log", "--oneline", "-1").strip(), (
            "the failed attempt's changes are still committed in its worktree"
        )
    finally:
        store.close()


def test_worktree_drift_after_acceptance_invalidates_the_candidate(
    sample_repo: Path, tmp_path: Path
) -> None:
    """Editing the frozen worktree afterwards must show up as drift, not silent reuse."""
    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    controller = _controller(store, sample_repo, tmp_path / "data")
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(sample_repo, base_commit),
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )
        assert outcome.task_state is TaskState.ACCEPTED
        assert outcome.receipt is not None
        assert outcome.workspace_matches_receipt is True

        target = Path(outcome.receipt.candidate.worktree) / "src" / "textkit" / "__init__.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

        drifted = inspect_run(store, outcome.run_id, project_root=sample_repo)
        assert drifted.run.workspace_matches_receipt is False
        assert drifted.run.task_state is TaskState.ACCEPTED, "history is not rewritten"

        # And the drift is visible in the worktree's own git state too.
        assert _git(Path(outcome.receipt.candidate.worktree), "status", "--porcelain").strip() != ""
    finally:
        store.close()


def test_dirty_target_repository_is_left_alone(sample_repo: Path, tmp_path: Path) -> None:
    """User changes in the target repo are theirs: recorded, never stashed or committed."""
    dirty_file = sample_repo / "src" / "textkit" / "__init__.py"
    dirty_file.write_text(
        dirty_file.read_text(encoding="utf-8") + "\n# user work in progress\n", encoding="utf-8"
    )
    before = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"], cwd=str(sample_repo), capture_output=True, text=True, check=False
    ).stdout
    stashes_before = _git(sample_repo, "stash", "list").strip()

    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    controller = _controller(store, sample_repo, tmp_path / "data")
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(sample_repo, base_commit),
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )
        after = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"], cwd=str(sample_repo), capture_output=True, text=True, check=False
        ).stdout
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert after == before, "the user's uncommitted changes must be exactly as they were"
        assert _git(sample_repo, "stash", "list").strip() == stashes_before
        assert "user work in progress" in dirty_file.read_text(encoding="utf-8")
        # The controller recorded that the target was dirty instead of hiding it. Notes live
        # in the run's own audit table so they survive a terminal transition.
        assert any("uncommitted changes" in note for note in store.notes_for(outcome.run_id))
        # Exactly one commit exists: the controller did not commit the user's work.
        assert _git(sample_repo, "rev-list", "--count", "HEAD").strip() == "1"
    finally:
        store.close()



# --------------------------------------------------------------------------
# the freeze itself: what it stages and what it refuses
# --------------------------------------------------------------------------


def test_a_freeze_that_leaves_the_checked_change_behind_is_refused_as_incomplete(
    sample_repo: Path,
) -> None:
    """The commit must be the tree the checks see; a change left uncommitted refuses the freeze.

    A pattern entry is the shape that used to slip through: ``src/**`` matched the worker's write,
    so nothing called it unauthorised, yet no path of that name exists to stage. The freeze
    returned the base commit as the candidate and left the change in the worktree.
    """
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-incomplete", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")

    with pytest.raises(GitError, match="freeze incomplete"):
        repo.freeze_candidate(worktree, ["src/**"], expected_head=base)
    assert repo.worktree_commit(worktree) == base, "nothing was committed"


@pytest.mark.parametrize(
    ("denied_path", "deny"),
    [
        ("src/textkit/generated.py", ["src/textkit/generated.py"]),
        ("src/textkit/.acpxrc.json", []),
    ],
    ids=["declared-deny", "built-in-deny"],
)
def test_a_denied_change_is_never_staged_by_the_freeze(
    sample_repo: Path, denied_path: str, deny: list[str]
) -> None:
    """A denied path inside an allowed directory refuses the freeze before anything is staged.

    The built-in deny list applies even when the caller passes no rule of its own.
    """
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-denied", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")
    (worktree / denied_path).write_text("{}\n", encoding="utf-8")

    with pytest.raises(GitStatusParseError, match="denied"):
        repo.freeze_candidate(worktree, ["src"], deny=deny, expected_head=base)
    assert repo.worktree_commit(worktree) == base
    assert _git(worktree, "diff", "--cached", "--name-only").strip() == "", "nothing was staged"


def test_a_freeze_records_the_round_start_as_its_base(sample_repo: Path) -> None:
    """The freeze's base is the commit the round started from, not whatever HEAD happens to be."""
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-base", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert freeze.base_commit == base
    assert freeze.candidate_commit != base
    assert _git(worktree, "rev-parse", "HEAD~1").strip() == base


@pytest.mark.parametrize("worker_commits", ["out-of-scope", "in-scope"])
def test_a_worker_commit_is_never_frozen_as_the_candidate(
    sample_repo: Path, worker_commits: str
) -> None:
    """A worker that moved HEAD refuses the freeze, whatever its commit contains.

    The freeze used to take the worktree's HEAD as its base. A worker's own ``git commit`` left
    the status clean, so nothing was checked against the scope and the worker's commit - here
    carrying HFlow's own state and a file under a skipped cache directory - became the candidate.
    """
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-worker-commit", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")
    if worker_commits == "out-of-scope":
        (worktree / ".hflow").mkdir()
        (worktree / ".hflow" / "project.json").write_text("{}\n", encoding="utf-8")
        (worktree / "tools" / "__pycache__").mkdir(parents=True)
        (worktree / "tools" / "__pycache__" / "evil.py").write_text("x = 1\n", encoding="utf-8")
        _git(worktree, "add", "-f", ".hflow/project.json", "tools/__pycache__/evil.py")
    _git(worktree, "add", "src")
    _git(worktree, "commit", "-q", "-m", "worker commit")
    worker_head = _git(worktree, "rev-parse", "HEAD").strip()

    with pytest.raises(GitStatusParseError, match="moved HEAD"):
        repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert repo.worktree_commit(worktree) == worker_head, "the freeze committed nothing"


def _hook_script(marker: Path) -> str:
    return f"#!/bin/sh\necho ran > '{marker.as_posix()}'\nexit 1\n"


def test_hflow_git_calls_run_no_hook_and_no_signing(
    sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a worktree, freezing a candidate and keeping its ref execute nothing.

    Hooks in the repository's shared hooks directory, a ``core.hooksPath`` and an fsmonitor
    command a worker set from inside its worktree, and a global ``commit.gpgsign`` all used to
    take effect: a failing hook blocked the freeze, a worker-written hook ran in the
    controller's process, and signing waited on gpg.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    hooks_dir = sample_repo / ".git" / "hooks"
    for name in ("post-checkout", "pre-commit", "commit-msg", "post-commit", "reference-transaction"):
        (hooks_dir / name).write_text(_hook_script(markers / f"shared-{name}"), newline="\n")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = hflow-no-such-gpg\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-hooks", base)
    # What an approve-all worker can do from inside its worktree: point the shared config at
    # hooks and an fsmonitor command it wrote inside its own write scope.
    worker_hooks = worktree / "src" / "hooks"
    worker_hooks.mkdir(parents=True)
    for name in ("pre-commit", "reference-transaction", "fsmonitor"):
        (worker_hooks / name).write_text(_hook_script(markers / f"worker-{name}"), newline="\n")
    _git(worktree, "config", "core.hooksPath", "src/hooks")
    _git(worktree, "config", "core.fsmonitor", "src/hooks/fsmonitor")
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert repo.ensure_candidate_ref(
        repo.candidate_ref("R-freeze-hooks", "A1"), freeze.candidate_commit
    ) == "created"
    second = repo.create_worktree("R-freeze-hooks-2", base)
    assert second.is_dir()
    assert sorted(path.name for path in markers.iterdir()) == [], "no hook ran"


def test_a_rename_lists_both_the_deleted_source_and_the_new_path(sample_repo: Path) -> None:
    """``--name-only`` with rename detection names only the new path; the deletion is a change.

    Moving a file used to drop its old path from ``freeze.paths``, the tree digest and the
    cumulative delivery paths, so a reader of any of them could not see that it was deleted.
    """
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-rename", base)
    old = worktree / "src" / "textkit" / "__init__.py"
    (worktree / "src" / "textkit" / "core.py").write_bytes(old.read_bytes())
    old.unlink()

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    expected = ["src/textkit/__init__.py", "src/textkit/core.py"]
    assert list(freeze.paths) == expected
    assert sorted(repo.diff_paths(base, freeze.candidate_commit)) == expected


@pytest.mark.parametrize(
    ("path", "flagged"),
    [
        *(
            (path, True)
            for path in (
                "AGENTS.md",
                "CLAUDE.md",
                "AGENTS.local.md",
                "CLAUDE.local.md",
                "src/AGENTS.md",
                "a/b/c/claude.local.md",
                "Agents.MD",
                ".env",
                ".ENV",
                ".dsh/skills/x/SKILL.md",
                ".agents/skills/y.md",
                ".DSH/Skills/z",
                ".dsh",
                ".dsh/skills",
                ".agents",
            )
        ),
        *(
            (path, False)
            for path in (
                "src/.env",
                "docs/AGENTS.md.bak",
                "AGENTS.mdx",
                "my-AGENTS.md",
                ".dsh/config.yml",
                "src/.dsh/skills/x.md",
                ".agents/other/x",
                "README.md",
                ".env.example",
                "agents/skills/x",
                ".dshx/skills/a",
                ".agents/skillsx/a",
            )
        ),
    ],
)
def test_dsh_context_paths_flags_what_dsh_would_load_or_scan_through(
    path: str, flagged: bool
) -> None:
    """Instruction files at any depth, the root skill dirs (and what leads to them), a root .env."""
    assert dsh_context_paths([path]) == ([path] if flagged else [])


def test_dsh_context_paths_keeps_git_order_and_the_spelling_found() -> None:
    assert dsh_context_paths(["src/Agents.md", "README.md", ".env", "src/Agents.md"]) == [
        "src/Agents.md",
        ".env",
    ]


def test_a_moved_instruction_file_is_classified_on_both_sides(sample_repo: Path) -> None:
    """``--no-renames`` names the moved-away path too, so the deletion is classified as well."""
    (sample_repo / "src" / "AGENTS.md").write_text("be careful\n", encoding="utf-8")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "add instructions")
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-dsh-move", base)
    (worktree / "src" / "AGENTS.md").rename(worktree / "src" / "textkit" / "CLAUDE.md")

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert dsh_context_paths(repo.diff_paths(base, freeze.candidate_commit)) == [
        "src/AGENTS.md",
        "src/textkit/CLAUDE.md",
    ]


def test_a_staged_rename_of_a_denied_file_is_refused_after_staging(
    sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-staging guard sees the deleted side of a rename, not only the new name.

    The worker's status is read before staging; a denied file deleted after that read, with a
    near-identical file added beside it, is staged as a rename. The guard used to see only the
    new name and commit the deletion of a denied file.
    """
    secret = sample_repo / "src" / "textkit" / "secret.txt"
    secret.write_text("original secret\n", encoding="utf-8")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "a denied file inside src")
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-rename-denied", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")
    status_before_the_race = repo.status_report(worktree)
    (worktree / "src" / "textkit" / "secret.txt").unlink()
    (worktree / "src" / "textkit" / "moved.txt").write_text("original secret\n", encoding="utf-8")

    real_status = GitRepo.status_report
    reads: list[int] = []

    def status_read_before_the_race(self, cwd=None):  # noqa: ANN001, ANN202
        reads.append(1)
        return status_before_the_race if len(reads) == 1 else real_status(self, cwd)

    monkeypatch.setattr(GitRepo, "status_report", status_read_before_the_race)
    with pytest.raises(GitStatusParseError, match="secret.txt"):
        repo.freeze_candidate(
            worktree, ["src"], deny=["src/textkit/secret.txt"], expected_head=base
        )
    assert repo.worktree_commit(worktree) == base, "nothing was committed"



@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_an_index_flag_that_hides_an_in_scope_edit_refuses_the_freeze(
    sample_repo: Path, flag: str
) -> None:
    """A flagged index entry blinds status, ``git add`` and the staged diff to its edit.

    A worker that flagged a scoped file and edited it used to get a candidate commit holding the
    old bytes while the checks and the fingerprint read the edited ones.
    """
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree(f"R-freeze-flag{flag.replace('-', '_')}", base)
    _git(worktree, "update-index", flag, "src/textkit/__init__.py")
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")
    (worktree / "src" / "textkit" / "extra.py").write_text("X = 1\n", encoding="utf-8")

    with pytest.raises(GitStatusParseError, match="index flags"):
        repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert repo.worktree_commit(worktree) == base, "nothing was committed"


def test_a_repository_ignore_stat_setting_does_not_hide_the_workers_edit(
    sample_repo: Path,
) -> None:
    """``core.ignoreStat=true`` in the user's repository makes a checkout flag every entry.

    HFlow's own ``worktree add`` used to inherit it, so a worker's ordinary edit was invisible to
    the freeze and the candidate commit was the base.
    """
    _git(sample_repo, "config", "core.ignoreStat", "true")
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-freeze-ignorestat", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    assert freeze.paths == ("src/textkit/__init__.py",)
    committed = _git(sample_repo, "show", f"{freeze.candidate_commit}:src/textkit/__init__.py")
    assert committed == FIXED_SOURCE


def test_an_inherited_git_c_setting_does_not_override_the_forced_configuration(
    sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_CONFIG_PARAMETERS`` (what ``git -c`` exports) is read after ``GIT_CONFIG_COUNT``.

    Launched through a git alias or from a hook, HFlow inherited a ``core.hooksPath`` there that
    beat the forced empty hooks directory, and the freeze ran that hook. The caller's other
    ``-c`` settings still apply.
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    evil_hooks = tmp_path / "ambient-hooks"
    evil_hooks.mkdir()
    for name in ("pre-commit", "post-checkout", "reference-transaction"):
        (evil_hooks / name).write_text(_hook_script(markers / f"ambient-{name}"), newline="\n")
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        f"'core.hookspath'='{evil_hooks.as_posix()}' 'hflow.probe'='kept'",
    )

    repo = GitRepo.discover(sample_repo)
    assert repo.run("config", "core.hooksPath").strip() != evil_hooks.as_posix()
    assert repo.run("config", "hflow.probe").strip() == "kept"
    base = repo.head
    worktree = repo.create_worktree("R-freeze-ambient-c", base)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")
    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    repo.ensure_candidate_ref(repo.candidate_ref("R-freeze-ambient-c", "A1"), freeze.candidate_commit)
    assert sorted(path.name for path in markers.iterdir()) == [], "no hook ran"


def test_inherited_repository_locating_variables_do_not_redirect_hflows_git_calls(
    sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run inside a git hook, HFlow inherits ``GIT_DIR`` / ``GIT_INDEX_FILE`` for another repo."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    repo = GitRepo.discover(sample_repo)
    assert repo.root == sample_repo.resolve()
    assert repo.head == _git(sample_repo, "--git-dir", str(sample_repo / ".git"), "rev-parse", "HEAD").strip()

def json_dumps(value: str) -> str:
    return value


# --------------------------------------------------------------------------
# shared Git metadata: what HFlow's own git reads from outside the candidate
# --------------------------------------------------------------------------


@pytest.fixture()
def isolated_git_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global config and the XDG directory at this test, so only the test writes them."""
    home = tmp_path / "git-home"
    home.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "global.gitconfig"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    return home


def test_hflows_own_freeze_ref_and_status_reads_leave_the_metadata_snapshot_unchanged(
    sample_repo: Path, isolated_git_home: Path
) -> None:
    """The comparison must not fire on HFlow's own git: a self-inflicted change would block every run."""
    repo = GitRepo.discover(sample_repo)
    base = repo.head
    worktree = repo.create_worktree("R-metadata-own", base)
    before = repo.metadata_snapshot(worktree)
    (worktree / "src" / "textkit" / "__init__.py").write_text(FIXED_SOURCE, encoding="utf-8")

    freeze = repo.freeze_candidate(worktree, ["src"], expected_head=base)
    repo.ensure_candidate_ref(repo.candidate_ref("R-metadata-own", "A1"), freeze.candidate_commit)
    repo.diff_paths(base, freeze.candidate_commit, cwd=sample_repo)
    repo.user_change_fingerprint()

    after = repo.metadata_snapshot(worktree)
    assert after.changes_since(before) == []
    assert after.digest == before.digest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    [
        "shared-config",
        "info-attributes",
        "attributes-file-pointer",
        "global-attributes",
        "include-target",
        "inactive-includeif",
        "worktree-config",
        "global-config",
    ],
)
def test_the_metadata_snapshot_names_each_shared_source_a_worker_can_write(
    sample_repo: Path, tmp_path: Path, isolated_git_home: Path, source: str
) -> None:
    """Every place git reads a filter driver, or the mapping that selects one, from outside the tree."""
    include = tmp_path / "included.gitconfig"
    _write(include, "[hflow]\n\tprobe = 1\n")
    global_config = isolated_git_home / "global.gitconfig"
    _write(global_config, "[hflow]\n\tglobal = 1\n")
    if source == "include-target":
        _git(sample_repo, "config", "include.path", include.as_posix())
    if source == "worktree-config":
        _git(sample_repo, "config", "extensions.worktreeConfig", "true")
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree(f"R-metadata-{source}", repo.head)
    before = repo.metadata_snapshot(worktree)

    # What a worker can do from inside its worktree, with no `.gitattributes` in its scope.
    if source == "shared-config":
        _git(worktree, "config", "filter.hflowprobe.clean", "cat")
        expected = ["config@checkout:filter.*.clean", "config@worktree:filter.*.clean"]
    elif source == "info-attributes":
        _write(sample_repo / ".git" / "info" / "attributes", "* filter=hflowprobe\n")
        expected = ["/.git/info/attributes"]
    elif source == "attributes-file-pointer":
        _write(tmp_path / "worker-attributes", "* filter=hflowprobe\n")
        _git(worktree, "config", "core.attributesFile", (tmp_path / "worker-attributes").as_posix())
        expected = ["config@worktree:core.attributesfile", "/worker-attributes"]
    elif source == "global-attributes":
        _write(isolated_git_home / "xdg" / "git" / "attributes", "* filter=hflowprobe\n")
        expected = ["/xdg/git/attributes"]
    elif source == "include-target":
        _write(include, '[hflow]\n\tprobe = 1\n[filter "hflowprobe"]\n\tclean = cat\n')
        expected = ["config@worktree:filter.*.clean", "/included.gitconfig"]
    elif source == "inactive-includeif":
        _git(worktree, "config", "includeIf.onbranch:no-such-branch.path", include.as_posix())
        expected = ["config@worktree:includeif.*.path"]
    elif source == "worktree-config":
        _git(worktree, "config", "--worktree", "filter.hflowprobe.clean", "cat")
        expected = ["config@worktree:filter.*.clean", "/config.worktree"]
    else:
        _write(global_config, '[hflow]\n\tglobal = 1\n[filter "hflowprobe"]\n\tclean = cat\n')
        expected = ["config@checkout:filter.*.clean", "/global.gitconfig"]

    changes = repo.metadata_snapshot(worktree).changes_since(before)
    for item in expected:
        assert any(change == item or change.endswith(item) for change in changes), (item, changes)
    if source == "worktree-config":
        assert "config@checkout:filter.*.clean" not in changes, "the checkout does not read it"


def test_a_metadata_change_is_named_by_key_without_its_value_or_subsection(
    sample_repo: Path, isolated_git_home: Path
) -> None:
    """A subsection can be a URL carrying a credential; the refusal must not copy it into the ledger."""
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-redacted", repo.head)
    before = repo.metadata_snapshot(worktree)
    _git(
        worktree,
        "config",
        "url.https://hflow:s3cr3t@example.invalid/.insteadOf",
        "https://hflow-value.example.invalid/",
    )

    changes = repo.metadata_snapshot(worktree).changes_since(before)
    assert "config@worktree:url.*.insteadof" in changes
    assert not any("s3cr3t" in change or "hflow-value" in change for change in changes)


@pytest.mark.parametrize("setting", ["worktree-config-copy", "relative-worktrees"])
def test_hflows_own_worktree_add_writes_are_not_reported_as_a_metadata_change(
    sample_repo: Path, tmp_path: Path, isolated_git_home: Path, setting: str
) -> None:
    """``worktree add`` itself can write metadata; the snapshot is taken after it, so a run is accepted.

    With ``extensions.worktreeConfig`` git copies the checkout's ``config.worktree`` into the new
    worktree's; with ``worktree.useRelativePaths`` (Git 2.48+) it records
    ``extensions.relativeWorktrees`` in the shared config.
    """
    if setting == "worktree-config-copy":
        _git(sample_repo, "config", "extensions.worktreeConfig", "true")
        _git(sample_repo, "config", "--worktree", "hflow.checkout-only", "1")
    else:
        _git(sample_repo, "config", "worktree.useRelativePaths", "true")
    config_before = (sample_repo / ".git" / "config").read_text(encoding="utf-8")
    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    controller = _controller(store, sample_repo, tmp_path / "data")
    try:
        outcome = controller.run_task(
            RunRequest(
                task=_task(sample_repo, base_commit),
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )
        if setting == "relative-worktrees":
            config_after = (sample_repo / ".git" / "config").read_text(encoding="utf-8")
            if "relativeworktrees" not in config_after.lower():
                pytest.skip("this Git does not record extensions.relativeWorktrees (Git < 2.48)")
            assert config_after != config_before, "HFlow's own worktree add wrote the shared config"
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert any(
            note.startswith("git_metadata: snapshot before dispatch sha256:")
            for note in store.notes_for(outcome.run_id)
        )
    finally:
        store.close()


def test_a_gitattributes_change_inside_the_scope_is_recorded_on_the_receipt_not_blocked(
    sample_repo: Path, tmp_path: Path, isolated_git_home: Path
) -> None:
    """In-tree attributes travel with the candidate, so the delivery shows them; the receipt says so."""
    store = Store(tmp_path / "data" / "hflow.sqlite")
    base_commit = _git(sample_repo, "rev-parse", "HEAD").strip()
    task = _task(sample_repo, base_commit).model_copy(
        update={"scope": Scope(write_allow=["src/textkit"], write_deny=[".git/**"])}
    )
    script = FakeScript(
        write_plan={
            "src/textkit/__init__.py": FIXED_SOURCE,
            "src/textkit/.gitattributes": "*.txt -diff\n",
        },
        agent_turns=1,
    )
    controller = Controller(
        store,
        FakeDriver(sample_repo, script),
        controller_build="m2-test-build",
        runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task,
                project=_project(sample_repo),
                project_root=sample_repo,
                workspace_root=sample_repo,
            )
        )
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        receipt = outcome.receipt
        assert receipt is not None
        assert "src/textkit/.gitattributes" in receipt.candidate_paths
        assert any(
            "src/textkit/.gitattributes" in item and "git add applied those attributes" in item
            for item in receipt.limitations
        ), receipt.limitations
    finally:
        store.close()


def test_the_metadata_snapshot_reads_no_global_attributes_file_when_git_names_none(
    sample_repo: Path, isolated_git_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no home directory at all git reads no global attributes file; that is not a failure.

    ``git var GIT_ATTR_GLOBAL`` then exits 1 with no output. Read as an error, it would block every
    worktree run in a HOME-less environment (a service account, a stripped CI job).
    """
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-homeless", repo.head)
    for name in ("HOME", "XDG_CONFIG_HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(name, raising=False)

    before = repo.metadata_snapshot(worktree)
    after = repo.metadata_snapshot(worktree)
    assert after.changes_since(before) == []
    assert not any(label.endswith("/git/attributes") for label, _ in after.entries)


def test_an_older_git_without_git_attr_global_locates_the_global_attributes_file_by_hand(
    sample_repo: Path, tmp_path: Path, isolated_git_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git older than 2.42 answers ``git var GIT_ATTR_GLOBAL`` with usage and exit 129.

    The snapshot then follows git's own rule: ``core.attributesFile``, else
    ``$XDG_CONFIG_HOME/git/attributes``, else ``~/.config/git/attributes`` as git expands ``~``.
    """
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-old-git", repo.head)
    real_run = subprocess.run

    def older_git(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if list(argv[:3]) == ["git", "var", "GIT_ATTR_GLOBAL"]:
            return subprocess.CompletedProcess(argv, 129, "", "usage: git var (-l | <variable>)\n")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", older_git)

    def labels() -> list[str]:
        return [label for label, _ in repo.metadata_snapshot(worktree).entries]

    assert any(label.endswith("/xdg/git/attributes") for label in labels())
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert any(label.endswith("/.config/git/attributes") for label in labels())
    attributes = tmp_path / "attrs"
    _git(worktree, "config", "core.attributesFile", attributes.as_posix())
    found = labels()
    assert any(label.endswith("/attrs") for label in found), found
    assert not any(label.endswith("/xdg/git/attributes") for label in found)


def test_a_metadata_path_that_is_not_a_regular_file_is_recorded_by_type_without_being_read(
    sample_repo: Path, tmp_path: Path, isolated_git_home: Path
) -> None:
    """A FIFO or device (``/dev/zero``) could hang the read forever; a directory stands in for them."""
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-not-a-file", repo.head)
    before = repo.metadata_snapshot(worktree)
    directory = tmp_path / "attrs-dir"
    directory.mkdir()
    _git(worktree, "config", "core.attributesFile", directory.as_posix())

    after = repo.metadata_snapshot(worktree)
    recorded = [value for label, value in after.entries if label.endswith("/attrs-dir")]
    assert recorded and recorded[0].startswith("not-a-regular-file:"), after.entries
    changes = after.changes_since(before)
    assert any(change.endswith("/attrs-dir") for change in changes), changes
    assert "config@worktree:core.attributesfile" in changes


def test_a_git_timeout_inside_the_metadata_snapshot_is_a_git_error(
    sample_repo: Path, isolated_git_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TimeoutExpired`` is not a ``GitError``; unconverted it would escape and leave the run RUNNING."""
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-timeout", repo.head)
    real_run = subprocess.run

    def hung_listing(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if "--list" in argv:
            raise subprocess.TimeoutExpired(argv, 300)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", hung_listing)
    with pytest.raises(GitError, match="timed out"):
        repo.metadata_snapshot(worktree)


#: A config value git stores as raw bytes and prints as-is: not valid UTF-8 (nor GBK).
NOT_TEXT_CONFIG = b"\n[hflowprobe]\n\tx = \xff\xfe\n"

#: On Windows CPython's subprocess reader thread raises the UnicodeDecodeError itself (stdout then
#: comes back ``None``, which ``GitRepo`` turns into a ``GitError``), and pytest reports that
#: thread's exception as a warning. It is the condition under test, not a leak.
NOT_TEXT_THREAD_WARNING = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)


def _append_to_shared_config(worktree: Path, data: bytes) -> None:
    """What a worker can do from its worktree: the repository's config is shared by every worktree."""
    common = Path(
        _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    )
    with (common / "config").open("ab") as handle:
        handle.write(data)


@NOT_TEXT_THREAD_WARNING
def test_a_config_value_that_is_not_text_makes_the_metadata_snapshot_a_git_error(
    sample_repo: Path, isolated_git_home: Path
) -> None:
    """Undecodable git output must raise ``GitError``, never ``AttributeError`` or ``UnicodeDecodeError``.

    On Windows the decode fails in subprocess's reader thread and the output comes back as
    ``None``; on POSIX ``subprocess.run`` raises ``UnicodeDecodeError`` itself. Unconverted, either
    escapes every ``except GitError`` and leaves the run ``RUNNING`` with no block.
    """
    import locale

    try:
        NOT_TEXT_CONFIG.decode(locale.getpreferredencoding(False))
    except UnicodeDecodeError:
        pass
    else:  # pragma: no cover - a single-byte locale decodes any byte
        pytest.skip("this locale decodes every byte, so git's output is always text here")
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-not-text", repo.head)
    repo.metadata_snapshot(worktree)
    _append_to_shared_config(worktree, NOT_TEXT_CONFIG)

    with pytest.raises(GitError, match="could not be decoded as text"):
        repo.metadata_snapshot(worktree)


@pytest.mark.parametrize("how", ["posix-raises", "windows-returns-none"])
@pytest.mark.parametrize("query", ["run-config-list", "git-query-attr-global"])
def test_undecodable_git_output_is_a_git_error_on_either_platforms_path(
    sample_repo: Path, isolated_git_home: Path, monkeypatch: pytest.MonkeyPatch, how: str, query: str
) -> None:
    """Both platform behaviours, simulated on whichever one runs the test, for both helpers."""
    repo = GitRepo.discover(sample_repo)
    worktree = repo.create_worktree("R-metadata-undecodable", repo.head)
    # ``GitRepo.run`` reads the listing; ``_git_query`` reads ``git var GIT_ATTR_GLOBAL``.
    marker = "--list" if query == "run-config-list" else "GIT_ATTR_GLOBAL"
    real_run = subprocess.run

    def undecodable(argv, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - subprocess.run shape
        if argv and argv[0] == "git" and marker in argv:
            if how == "posix-raises":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return subprocess.CompletedProcess(argv, 0, None, "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", undecodable)
    with pytest.raises(GitError, match="could not be decoded as text"):
        repo.metadata_snapshot(worktree)
