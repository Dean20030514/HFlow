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
from hflow.gitworkspace import GitRepo
from hflow.store import Store
from hflow.verify import CheckRunners

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
        assert all(entry.rstrip("/") in {"__pycache__", "src/textkit/__pycache__", "tests/__pycache__", ".pytest_cache"} or "__pycache__" in entry for entry in ignored), (
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
        # The controller recorded that the target was dirty instead of hiding it.
        assert "uncommitted changes" in (store.get_run(outcome.run_id)["block_reason"] or "")
        # Exactly one commit exists: the controller did not commit the user's work.
        assert _git(sample_repo, "rev-list", "--count", "HEAD").strip() == "1"
    finally:
        store.close()


def json_dumps(value: str) -> str:
    return value
