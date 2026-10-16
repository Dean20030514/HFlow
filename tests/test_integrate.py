"""Batch I2: controlled integration of an accepted candidate into a local branch.

Every test builds a real Git repository, runs one offline task to ``ACCEPTED/LOCAL_CANDIDATE``
with the fake driver and a real command check, and then integrates it. Nothing here calls a
model: integration itself never does.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from hflow import integrate as integrate_module
from hflow.cli import main
from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryState,
    IntegrationState,
    ProjectConfig,
    ProjectLimits,
    RefusalCode,
    RefusedError,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.gitworkspace import GIT_METADATA_CHANGED, GitError, GitRepo
from hflow.hints import shell_word
from hflow.integrate import (
    apply_integration,
    prepare_integration,
    reconcile_integration,
)
from hflow.ownership import Probe
from hflow.store import Store
from hflow.verify import CheckRunners

BUILD = "integrate-test-build"

CHECK_SOURCE = """import pathlib, sys
app = pathlib.Path("app.txt").read_text(encoding="utf-8")
notes = pathlib.Path("notes.txt").read_text(encoding="utf-8")
if "value = 2" not in app:
    print("app.txt does not set value = 2"); sys.exit(1)
if "broken" in notes:
    print("notes.txt says broken"); sys.exit(1)
print("ok")
"""


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


def _commit_on(repo: Path, branch: str, path: str, text: str, message: str) -> str:
    """Commit one file onto ``branch`` without touching any checkout (plumbing only)."""
    blob = subprocess.run(  # noqa: S603,S607
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo), input=text, capture_output=True, text=True, check=True,
    ).stdout.strip()
    parent = _git(repo, "rev-parse", f"refs/heads/{branch}").strip()
    index = repo / ".git" / f"tmp-index-{branch.replace('/', '_')}"
    env = {**__import__("os").environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "read-tree", parent], cwd=str(repo), env=env, check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603,S607
        ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
        cwd=str(repo), env=env, check=True,
    )
    tree = subprocess.run(  # noqa: S603,S607
        ["git", "write-tree"], cwd=str(repo), env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    index.unlink()
    commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", message).strip()
    _git(repo, "update-ref", f"refs/heads/{branch}", commit, parent)
    return commit


def _project() -> ProjectConfig:
    return ProjectConfig(
        project_id="integrate-project",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=[sys.executable, "check.py"],
                timeout_seconds=120,
            )
        ],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )


def _task(base_commit: str, task_id: str = "T-integrate") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        revision=1,
        goal="Set value to 2 in app.txt",
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="value is 2", check_ids=["unit"]),
        ],
        scope=Scope(write_allow=["app.txt"], write_deny=[".git/**"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:none",
            reason="a one-line change",
        ),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
        workspace=WorkspaceSpec(mode="worktree", base_commit=base_commit, keep=True),
    )


class Scene:
    def __init__(self, tmp_path: Path, *, user_branch: str) -> None:
        self.repo = tmp_path / "project"
        self.repo.mkdir()
        (self.repo / "app.txt").write_text("value = 1\n", encoding="utf-8")
        (self.repo / "notes.txt").write_text("notes\n", encoding="utf-8")
        (self.repo / "check.py").write_text(CHECK_SOURCE, encoding="utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD").strip()
        if user_branch != "main":
            _git(self.repo, "switch", "-q", "-c", user_branch)
        # The user's own uncommitted work, which nothing in an integration may touch.
        (self.repo / "scratch.txt").write_text("user's own uncommitted file\n", encoding="utf-8")
        self.data_dir = tmp_path / "data"
        self.store = Store(self.data_dir / "hflow.sqlite")
        self.project = _project()

    def accept(self, task_id: str = "T-integrate", text: str = "value = 2\n") -> str:
        controller = Controller(
            self.store,
            FakeDriver(self.repo, FakeScript(write_plan={"app.txt": text}, agent_turns=1)),
            controller_build=BUILD,
            runners=CheckRunners.offline_default(),
            data_dir=self.data_dir,
            production=False,
        )
        outcome = controller.run_task(
            RunRequest(
                task=_task(self.base, task_id),
                project=self.project,
                project_root=self.repo,
                workspace_root=self.repo,
            )
        )
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        return outcome.run_id

    def prepare(self, run_id: str, target: str = "main"):
        return prepare_integration(
            self.store,
            run_id=run_id,
            target_branch=target,
            project=self.project,
            runners=CheckRunners.offline_default(),
            data_dir=self.data_dir,
        )

    def apply(self, integration_id: str, expect: str):
        return apply_integration(
            self.store,
            integration_id=integration_id,
            expect_target=expect,
            applied_by="tester",
            controller_build=BUILD,
        )

    def tip(self, branch: str = "main") -> str:
        return _git(self.repo, "rev-parse", f"refs/heads/{branch}").strip()

    def user_state(self) -> tuple[str, str, str]:
        return (
            _git(self.repo, "symbolic-ref", "HEAD").strip(),
            _git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            (self.repo / "scratch.txt").read_text(encoding="utf-8"),
        )


@pytest.fixture()
def scene(tmp_path: Path) -> Scene:
    """The user works on another branch, so ``main`` is not checked out anywhere."""
    scene = Scene(tmp_path, user_branch="work")
    yield scene
    scene.store.close()


@pytest.fixture()
def scene_on_main(tmp_path: Path) -> Scene:
    """The user's checkout has ``main`` checked out."""
    scene = Scene(tmp_path, user_branch="main")
    yield scene
    scene.store.close()


def _gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrate_module, "probe", lambda identity: Probe("gone", "test: exited"))


# --------------------------------------------------------------------------
# squash: the target has not moved
# --------------------------------------------------------------------------


def test_squash_prepare_builds_one_checked_commit_and_moves_nothing(scene: Scene) -> None:
    run_id = scene.accept()
    run_receipt = json.loads(scene.store.get_run(run_id)["receipt_json"])
    user_before = scene.user_state()

    outcome = scene.prepare(run_id)

    record = outcome.record
    assert record.state is IntegrationState.READY, record.detail
    assert record.mode == "squash"
    assert record.target_tip == scene.base
    assert scene.tip() == scene.base, "prepare never moves the target"
    repo = GitRepo(scene.repo)
    # One new commit whose parent is the tip and whose tree is exactly the checked candidate's.
    assert _git(scene.repo, "rev-parse", f"{record.integration_commit}^").strip() == scene.base
    assert repo.tree_of(record.integration_commit) == run_receipt["candidate"]["git_tree"]
    assert record.integration_commit != run_receipt["candidate"]["git_commit"]
    assert repo.ref_target(record.integration_ref) == record.integration_commit
    assert record.integration_ref == f"refs/hflow/integrations/{run_id}/{record.integration_id}"
    assert record.paths == ["app.txt"]
    # The checks ran in a worktree of their own, which is gone again.
    assert record.evidence_ids
    rows = scene.store.evidence_for(run_id, kind="integration-check")
    assert [r["evidence_id"] for r in rows] == record.evidence_ids
    assert all(r["status"] == "passed" for r in rows)
    assert record.worktree_state == "REMOVED"
    assert not Path(record.worktree_path).exists()
    assert scene.user_state() == user_before
    # The scene's ledger is not the default one, so the printed command names it.
    assert outcome.handoff == [
        f"hflow integrate apply {record.integration_id} --expect-target {scene.base} "
        f"--data-dir {shell_word(os.path.abspath(scene.data_dir))}"
    ]


def test_apply_moves_the_branch_by_compare_and_set_and_keeps_the_run_receipt(scene: Scene) -> None:
    run_id = scene.accept()
    receipt_before = scene.store.get_run(run_id)["receipt_json"]
    invocations_before = len(scene.store.invocations_for(run_id))
    prepared = scene.prepare(run_id).record
    user_before = scene.user_state()

    outcome = scene.apply(prepared.integration_id, scene.base)

    assert outcome.record.state is IntegrationState.INTEGRATED
    assert outcome.record.basis == "hflow_ref_update"
    assert scene.tip() == prepared.integration_commit
    receipt = outcome.receipt
    assert receipt is not None
    assert receipt.delivery_state is DeliveryState.INTEGRATED
    assert receipt.target_tip_before == scene.base
    assert receipt.integration_commit == prepared.integration_commit
    assert receipt.verification.evidence_ids == prepared.evidence_ids
    assert scene.store.integration_receipt(prepared.integration_id) == receipt
    # The run's own delivery record is never rewritten, and nothing was dispatched.
    row = scene.store.get_run(run_id)
    assert row["receipt_json"] == receipt_before
    assert row["delivery_state"] == DeliveryState.LOCAL_CANDIDATE.value
    assert len(scene.store.invocations_for(run_id)) == invocations_before
    assert scene.user_state() == user_before

    # Idempotent: a second apply writes nothing and returns the same receipt.
    again = scene.apply(prepared.integration_id, scene.base)
    assert again.record.state is IntegrationState.INTEGRATED
    assert again.receipt == receipt
    assert scene.tip() == prepared.integration_commit


def test_apply_refuses_an_expect_target_that_is_not_the_checked_tip(scene: Scene) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    with pytest.raises(RefusedError) as caught:
        scene.apply(prepared.integration_id, "0" * 40)
    assert caught.value.code is RefusalCode.TARGET_MOVED
    assert scene.tip() == scene.base
    assert scene.store.integration(prepared.integration_id).state is IntegrationState.READY


# --------------------------------------------------------------------------
# the target is checked out: hand off, then observe
# --------------------------------------------------------------------------


def test_a_checked_out_target_is_never_moved_and_a_hand_merge_is_observed(
    scene_on_main: Scene,
) -> None:
    scene = scene_on_main
    run_id = scene.accept()
    prepared = scene.prepare(run_id)
    record = prepared.record
    assert record.state is IntegrationState.READY
    assert prepared.handoff[0].startswith("git -C ")
    assert record.integration_commit in prepared.handoff[0]

    outcome = scene.apply(record.integration_id, scene.base)
    assert outcome.record.state is IntegrationState.READY
    assert scene.tip() == scene.base, "a checked-out branch is never moved by HFlow"
    assert any("checked out" in note for note in outcome.notes)
    assert (scene.repo / "app.txt").read_text(encoding="utf-8") == "value = 1\n"

    # The operator merges it themselves; their own uncommitted file survives a fast-forward.
    _git(scene.repo, "merge", "--ff-only", "-q", record.integration_commit)
    assert (scene.repo / "scratch.txt").exists()
    reconciled = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert reconciled.record.state is IntegrationState.INTEGRATED
    assert reconciled.record.basis == "operator_merge_observed"
    assert reconciled.receipt is not None
    assert reconciled.receipt.applied_by == ""
    assert any("did not move the target" in item for item in reconciled.receipt.limitations)


# --------------------------------------------------------------------------
# replayed: the target moved since the task's base
# --------------------------------------------------------------------------


def test_a_moved_target_is_replayed_and_checked_on_the_merged_tree(scene: Scene) -> None:
    run_id = scene.accept()
    moved = _commit_on(scene.repo, "main", "notes.txt", "notes, updated\n", "unrelated")

    record = scene.prepare(run_id).record

    assert record.state is IntegrationState.READY, record.detail
    assert record.mode == "replayed"
    assert record.target_tip == moved
    assert _git(scene.repo, "rev-parse", f"{record.integration_commit}^").strip() == moved
    merged_app = _git(scene.repo, "show", f"{record.integration_commit}:app.txt")
    merged_notes = _git(scene.repo, "show", f"{record.integration_commit}:notes.txt")
    assert merged_app == "value = 2\n" and merged_notes == "notes, updated\n"
    assert record.paths == ["app.txt"]
    outcome = scene.apply(record.integration_id, moved)
    assert outcome.record.state is IntegrationState.INTEGRATED
    assert any("not reviewed again" in item for item in outcome.receipt.limitations)


def test_a_conflict_is_recorded_and_nothing_is_committed_or_moved(scene: Scene) -> None:
    run_id = scene.accept()
    moved = _commit_on(scene.repo, "main", "app.txt", "value = 3\n", "conflicting")

    record = scene.prepare(run_id).record

    assert record.state is IntegrationState.CONFLICT
    assert record.conflict_paths == ["app.txt"]
    assert record.integration_commit == ""
    assert scene.tip() == moved
    with pytest.raises(RefusedError):
        scene.apply(record.integration_id, moved)


def test_a_merged_tree_that_fails_its_checks_is_not_applicable(scene: Scene) -> None:
    run_id = scene.accept()
    moved = _commit_on(scene.repo, "main", "notes.txt", "broken\n", "breaks the check")

    record = scene.prepare(run_id).record

    assert record.state is IntegrationState.CHECKS_FAILED
    assert "approved checks failed" in record.detail
    rows = scene.store.evidence_for(run_id, kind="integration-check")
    assert [r["status"] for r in rows] == ["failed"]
    with pytest.raises(RefusedError):
        scene.apply(record.integration_id, moved)
    assert scene.tip() == moved


def test_a_target_that_moves_after_prepare_goes_stale_and_a_new_prepare_succeeds(
    scene: Scene,
) -> None:
    first_run = scene.accept()
    first = scene.prepare(first_run).record
    moved = _commit_on(scene.repo, "main", "notes.txt", "notes, later\n", "someone else")

    stale = scene.apply(first.integration_id, scene.base)
    assert stale.record.state is IntegrationState.STALE
    assert scene.tip() == moved

    second = scene.prepare(first_run).record
    assert second.state is IntegrationState.READY
    assert second.mode == "replayed"
    done = scene.apply(second.integration_id, moved)
    assert done.record.state is IntegrationState.INTEGRATED
    assert scene.tip() == second.integration_commit


def test_a_new_prepare_supersedes_a_ready_one(scene: Scene) -> None:
    run_id = scene.accept()
    first = scene.prepare(run_id).record
    second = scene.prepare(run_id).record
    assert scene.store.integration(first.integration_id).state is IntegrationState.SUPERSEDED
    assert second.state is IntegrationState.READY
    with pytest.raises(RefusedError):
        scene.apply(first.integration_id, scene.base)


# --------------------------------------------------------------------------
# refusals before any record exists
# --------------------------------------------------------------------------


def test_refusals_create_no_record(scene: Scene, tmp_path: Path) -> None:
    run_id = scene.accept()
    with pytest.raises(RefusedError) as missing:
        scene.prepare(run_id, target="no-such-branch")
    assert missing.value.code is RefusalCode.NOT_INTEGRABLE
    with pytest.raises(RefusedError) as invalid:
        scene.prepare(run_id, target="-x")
    assert invalid.value.code is RefusalCode.INVALID_SPEC
    changed = ProjectConfig.model_validate(
        {
            **scene.project.model_dump(mode="json"),
            "checks": [
                {"id": "unit", "kind": "command", "argv": [sys.executable, "-c", "pass"],
                 "timeout_seconds": 60}
            ],
        }
    )
    with pytest.raises(RefusedError) as digest:
        prepare_integration(
            scene.store, run_id=run_id, target_branch="main", project=changed,
            runners=CheckRunners.offline_default(), data_dir=scene.data_dir,
        )
    assert digest.value.code is RefusalCode.NOT_INTEGRABLE
    # A branch that does not contain the task's base (an unrelated line of history).
    _git(scene.repo, "branch", "other", scene.base)
    orphan_tree = _git(scene.repo, "rev-parse", f"{scene.base}^{{tree}}").strip()
    orphan = _git(scene.repo, "commit-tree", orphan_tree, "-m", "orphan").strip()
    _git(scene.repo, "update-ref", "refs/heads/other", orphan)
    with pytest.raises(RefusedError) as unrelated:
        scene.prepare(run_id, target="other")
    assert unrelated.value.code is RefusalCode.TARGET_MOVED
    assert scene.store.integrations_for(run_id) == []


def test_only_an_accepted_run_is_integrable(scene: Scene) -> None:
    controller = Controller(
        scene.store,
        FakeDriver(scene.repo, FakeScript(write_plan={"app.txt": "value = 9\n"}, agent_turns=1)),
        controller_build=BUILD,
        runners=CheckRunners.offline_default(),
        data_dir=scene.data_dir,
        production=False,
    )
    outcome = controller.run_task(
        RunRequest(
            task=_task(scene.base, "T-fails"),
            project=scene.project,
            project_root=scene.repo,
            workspace_root=scene.repo,
        )
    )
    assert outcome.task_state is TaskState.BLOCKED
    with pytest.raises(RefusedError) as caught:
        scene.prepare(outcome.run_id)
    assert caught.value.code is RefusalCode.NOT_INTEGRABLE


# --------------------------------------------------------------------------
# crash recovery
# --------------------------------------------------------------------------


def test_an_apply_interrupted_before_the_ref_moved_reconciles_to_ready(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record

    def crash(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "update_ref_cas", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.apply(prepared.integration_id, scene.base)
    assert scene.store.integration(prepared.integration_id).state is IntegrationState.APPLYING
    monkeypatch.undo()

    # The owner is this very process, so it still reads as running: nothing is written.
    with pytest.raises(RefusedError):
        reconcile_integration(
            scene.store, integration_id=prepared.integration_id, controller_build=BUILD
        )
    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=prepared.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.READY
    assert scene.tip() == scene.base
    done = scene.apply(prepared.integration_id, scene.base)
    assert done.record.state is IntegrationState.INTEGRATED


def test_an_apply_interrupted_after_the_ref_moved_reconciles_to_integrated(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    real = GitRepo.update_ref_cas

    def update_then_crash(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        real(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "update_ref_cas", update_then_crash)
    with pytest.raises(KeyboardInterrupt):
        scene.apply(prepared.integration_id, scene.base)
    monkeypatch.undo()
    assert scene.tip() == prepared.integration_commit
    assert scene.store.integration(prepared.integration_id).state is IntegrationState.APPLYING

    _gone(monkeypatch)
    # apply itself reconciles an interrupted apply first and never runs the update twice.
    outcome = scene.apply(prepared.integration_id, scene.base)
    assert outcome.record.state is IntegrationState.INTEGRATED
    assert outcome.record.basis == "observed_after_interruption"


def test_an_interrupted_apply_whose_target_moved_elsewhere_goes_stale(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record

    def crash(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "update_ref_cas", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.apply(prepared.integration_id, scene.base)
    monkeypatch.undo()
    _commit_on(scene.repo, "main", "notes.txt", "elsewhere\n", "someone else")
    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=prepared.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.STALE


def _ref_reads_fail(monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    """``git rev-parse --verify --quiet <ref>`` fails as a git that could not start does.

    Windows reports ``0xC0000142`` with no output at all when a process cannot initialise, which
    was seen under load. Every other git call runs for real.
    """
    real_run = subprocess.run

    def flaky(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - subprocess.run shape
        if argv[:2] == ["git", "rev-parse"] and "--quiet" in argv and argv[-1] == ref:
            return subprocess.CompletedProcess(argv, 0xC0000142, b"", b"")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", flaky)


def test_a_failed_target_read_leaves_an_applied_integration_unsettled_not_stale(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CAS moved the branch, then the process died; a failing read must not record STALE.

    Read as "the branch is gone", the failure would settle an integration that did land as
    ``stale ... (deleted)``. It raises instead, the record stays ``applying``, and a reconcile
    that can read the branch records what happened.
    """
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    real = GitRepo.update_ref_cas

    def update_then_crash(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        real(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "update_ref_cas", update_then_crash)
    with pytest.raises(KeyboardInterrupt):
        scene.apply(prepared.integration_id, scene.base)
    monkeypatch.undo()
    assert scene.tip() == prepared.integration_commit

    real_run = subprocess.run
    _gone(monkeypatch)
    _ref_reads_fail(monkeypatch, prepared.target_ref)
    for attempt in (
        lambda: reconcile_integration(
            scene.store, integration_id=prepared.integration_id, controller_build=BUILD
        ),
        lambda: scene.apply(prepared.integration_id, scene.base),
    ):
        with pytest.raises(GitError, match="0xC0000142"):
            attempt()
        assert scene.store.integration(prepared.integration_id).state is IntegrationState.APPLYING

    monkeypatch.setattr(subprocess, "run", real_run)
    outcome = reconcile_integration(
        scene.store, integration_id=prepared.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.INTEGRATED
    assert outcome.record.basis == "observed_after_interruption"


def test_a_failed_target_read_never_records_a_ready_integration_stale(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    real_run = subprocess.run
    _ref_reads_fail(monkeypatch, prepared.target_ref)
    with pytest.raises(GitError, match="0xC0000142"):
        scene.apply(prepared.integration_id, scene.base)
    with pytest.raises(GitError, match="0xC0000142"):
        reconcile_integration(
            scene.store, integration_id=prepared.integration_id, controller_build=BUILD
        )
    assert scene.store.integration(prepared.integration_id).state is IntegrationState.READY
    assert scene.tip() == scene.base

    monkeypatch.setattr(subprocess, "run", real_run)
    assert scene.apply(prepared.integration_id, scene.base).record.state is IntegrationState.INTEGRATED


def test_a_prepare_interrupted_during_checks_reconciles_to_interrupted(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()

    def crash(**kwargs):  # noqa: ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(integrate_module, "verify_candidate", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    assert record.state is IntegrationState.CHECKING
    assert Path(record.worktree_path).exists()
    # Another prepare of the same run is refused while that one is unresolved.
    with pytest.raises(RefusedError):
        scene.prepare(run_id)

    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.INTERRUPTED
    assert outcome.record.worktree_state == "REMOVED"
    assert not Path(record.worktree_path).exists()
    assert scene.tip() == scene.base
    assert scene.prepare(run_id).record.state is IntegrationState.READY


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_prepare_apply_show_and_status(
    scene: Scene, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_id = scene.accept()
    project_file = tmp_path / "project.json"
    project_file.write_text(scene.project.model_dump_json(), encoding="utf-8")
    data = ["--data-dir", str(scene.data_dir)]
    scene.store.close()

    assert main(["integrate", "prepare", run_id, "--target", "main", "--project",
                 str(project_file), "--json", *data]) == 0
    prepared = json.loads(capsys.readouterr().out)
    integration_id = prepared["integration"]["integration_id"]
    assert prepared["integration"]["state"] == "ready"

    assert main(["integrate", "apply", integration_id, "--expect-target", "0" * 40, *data]) == 2
    capsys.readouterr()
    assert main(["integrate", "apply", integration_id, "--expect-target", scene.base, *data]) == 0
    applied = capsys.readouterr().out
    assert "INTEGRATED (hflow_ref_update)" in applied

    assert main(["integrate", "show", integration_id, *data]) == 0
    assert "state         integrated" in capsys.readouterr().out
    assert main(["status", run_id, *data]) == 0
    status = capsys.readouterr().out
    assert "delivery      LOCAL_CANDIDATE" in status
    assert f"{integration_id}  state=integrated target=refs/heads/main" in status
    assert "kind=integration-check" in status
    assert main(["report", run_id, "--json", *data]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["state"] for item in report["integrations"]] == ["integrated"]
    assert main(["integrate", "show", "G-missing", *data]) == 4


def test_cli_prepare_defaults_to_the_recorded_contract_only_while_it_still_matches(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--project``, ``<project root>/.hflow/project.json`` is used only when it still
    digests to the contract the run was admitted under; anything else needs ``--project``."""
    run_id = scene.accept()
    data = ["--data-dir", str(scene.data_dir)]
    scene.store.close()
    default = scene.repo / ".hflow" / "project.json"

    # No file at the default path: nothing is recorded, the operator is told to name one.
    assert main(["integrate", "prepare", run_id, "--target", "main", *data]) == 4
    assert "pass --project" in capsys.readouterr().err

    # A weaker contract at the default path is not silently picked up.
    default.parent.mkdir()
    weaker = scene.project.model_copy(update={"write_deny": []})
    default.write_text(weaker.model_dump_json(), encoding="utf-8")
    assert main(["integrate", "prepare", run_id, "--target", "main", *data]) == 4
    assert "pass --project" in capsys.readouterr().err

    # Nothing was recorded, and printed hints name --project until the default resolves the
    # run's contract.
    assert main(["status", run_id, *data]) == 0
    status = capsys.readouterr().out
    assert "integration " not in status and "target=refs/heads" not in status
    assert "--project <project.json>" in status
    default.write_text(scene.project.model_dump_json(), encoding="utf-8")
    assert main(["status", run_id, *data]) == 0
    [hint] = [
        line for line in capsys.readouterr().out.splitlines() if f"integrate {run_id}" in line
    ]
    assert "--project" not in hint

    assert main(["integrate", "prepare", run_id, "--target", "main", "--json", *data]) == 0
    assert json.loads(capsys.readouterr().out)["integration"]["state"] == "ready"


def test_cli_prepare_without_project_names_an_unknown_run_and_never_raises_looking_for_one(
    scene: Scene, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Looking for the default contract is a lookup, not a command: an unknown run is named as
    one, and a default it cannot read or decode, or an admission record it cannot read, means
    "pass --project" - never a traceback. With ``--project`` the run's own unreadable record
    then surfaces through the command's error mapping (exit 6)."""
    run_id = scene.accept()
    data = ["--data-dir", str(scene.data_dir)]

    assert main(["integrate", "prepare", "R-no-such-run", "--target", "main", *data]) == 4
    err = capsys.readouterr().err
    assert "unknown run R-no-such-run" in err and "pass --project" not in err, err

    # A default contract that is not UTF-8: no default, no UnicodeDecodeError.
    default = scene.repo / ".hflow" / "project.json"
    default.parent.mkdir()
    default.write_bytes(b"\xff\xfe{\x00not utf-8")
    assert main(["integrate", "prepare", run_id, "--target", "main", *data]) == 4
    assert "pass --project" in capsys.readouterr().err

    # An admission record that no longer validates: no default, no StoredRecordUnreadable.
    default.write_text(scene.project.model_dump_json(), encoding="utf-8")
    scene.store.record_note(run_id, "admission_binding: {not a binding", limit=100)
    scene.store.close()
    assert main(["integrate", "prepare", run_id, "--target", "main", *data]) == 4
    assert "pass --project" in capsys.readouterr().err
    project_file = tmp_path / "project.json"
    project_file.write_text(scene.project.model_dump_json(), encoding="utf-8")
    assert main(["integrate", "prepare", run_id, "--target", "main", "--project",
                 str(project_file), *data]) == 6
    assert "unreadable" in capsys.readouterr().err
    assert scene.tip() == scene.base


# --------------------------------------------------------------------------
# printed next commands: pasted as printed, they reach the same ledger
# --------------------------------------------------------------------------


def _paste(command: str) -> list[str]:
    """The argv an operator's shell hands ``hflow`` for a printed command, without ``hflow``.

    POSIX word splitting reads the double-quoted Windows form of these paths as PowerShell does.
    """
    argv = shlex.split(command)
    assert argv[0] == "hflow", command
    return argv[1:]


def test_a_printed_apply_command_names_the_data_dir_and_works_as_pasted(
    scene: Scene, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """With a non-default ``--data-dir`` the printed command used to omit it and exit 4."""
    run_id = scene.accept()
    project_file = tmp_path / "project.json"
    project_file.write_text(scene.project.model_dump_json(), encoding="utf-8")
    scene.store.close()

    assert main(["--data-dir", str(scene.data_dir), "integrate", "prepare", run_id, "--target",
                 "main", "--project", str(project_file)]) == 0
    printed = capsys.readouterr().out.splitlines()
    [command] = [line.removeprefix("next:").strip() for line in printed if line.startswith("next: ")]
    assert command.endswith(f" --data-dir {shell_word(os.path.abspath(scene.data_dir))}")
    argv = _paste(command)

    # Without it the command opens the default ledger, where this integration does not exist.
    at = argv.index("--data-dir")
    assert main(argv[:at] + argv[at + 2 :]) == 4
    capsys.readouterr()

    assert main(argv) == 0
    assert "INTEGRATED (hflow_ref_update)" in capsys.readouterr().out
    assert scene.tip() != scene.base


def test_a_printed_command_names_no_data_dir_when_the_default_reaches_the_ledger(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HFLOW_DATA_DIR", str(scene.data_dir))
    run_id = scene.accept()

    outcome = scene.prepare(run_id)

    assert outcome.handoff == [
        f"hflow integrate apply {outcome.record.integration_id} --expect-target {scene.base}"
    ]


def test_a_checked_out_hand_off_prints_commands_that_work_as_pasted(
    scene_on_main: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = scene_on_main
    run_id = scene.accept()
    prepared = scene.prepare(run_id)
    record = prepared.record
    merge, reconcile = prepared.handoff
    assert merge.startswith("git -C ") and merge.endswith(
        f" merge --ff-only {record.integration_commit}"
    )
    assert reconcile == (
        f"hflow integrate reconcile {record.integration_id} "
        f"--data-dir {shell_word(os.path.abspath(scene.data_dir))}"
    )

    subprocess.run(  # noqa: S603 - the printed command, as an operator would paste it
        shlex.split(merge),
        capture_output=True,
        check=True,
        timeout=60,
        env={
            **os.environ,
            "GIT_COMMITTER_NAME": "Sample Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        },
    )
    assert scene.tip() == record.integration_commit
    scene.store.close()
    assert main(_paste(reconcile)) == 0
    assert "INTEGRATED (operator_merge_observed)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "windows", "expected"),
    [
        ("C:\\Users\\a b\\HFlow data", True, '"C:\\Users\\a b\\HFlow data"'),
        ("C:\\$data\\x", True, "'C:\\$data\\x'"),
        ("C:\\it's `x`", True, "'C:\\it''s `x`'"),
        ("C:\\a\u2019b$", True, "'C:\\a\u2019\u2019b$'"),
        ("C:\\a\u201cb", True, "'C:\\a\u201cb'"),
        # A root is the one directory printed with a trailing backslash: ``.`` keeps that
        # backslash away from the closing quote.
        ("C:\\", True, '"C:\\."'),
        ("\\\\srv\\my share\\", True, '"\\\\srv\\my share\\."'),
        ("D:\\$x y\\", True, "'D:\\$x y\\.'"),
        ("/srv/hflow data", False, "'/srv/hflow data'"),
        ("/srv/it's", False, "'/srv/it'\"'\"'s'"),
        ("/srv/plain", False, "/srv/plain"),
        ("/", False, "/"),
    ],
)
def test_a_printed_path_is_one_word_in_the_documented_shells(
    text: str, windows: bool, expected: str
) -> None:
    """PowerShell on Windows (double quotes unless it would expand or end there), sh elsewhere."""
    assert shell_word(text, windows=windows) == expected


def _powershell(name: str) -> str | None:
    """``pwsh.exe`` / ``powershell.exe`` on an absolute PATH entry, on Windows only."""
    if os.name != "nt":
        return None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / f"{name}.exe"
        if entry and Path(entry).is_absolute() and candidate.is_file():
            return str(candidate)
    return None


@pytest.mark.parametrize(
    "shell",
    [
        pytest.param(
            _powershell(name),
            id=name,
            marks=pytest.mark.skipif(_powershell(name) is None, reason=f"needs {name} on Windows"),
        )
        for name in ("pwsh", "powershell")
    ],
)
def test_a_printed_path_reaches_the_program_as_that_directory_through_powershell(
    tmp_path: Path, shell: str | None
) -> None:
    """Pasted into PowerShell, each word arrives as one argument naming the same directory.

    Windows PowerShell 5.1 is the strict one: it re-quotes an argument that holds a space for
    the program, so a printed ``"C:\\my dir\\"`` would arrive as ``C:\\my dir" <next word>``.
    """
    paths = [
        "C:\\",
        "C:\\my dir\\",
        "C:\\Users\\a b\\HFlow data",
        "C:\\$data x\\",
        "C:\\it's `x`",
        "C:\\100% done",
    ]
    echo = tmp_path / "echo_argv.py"
    echo.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    words = " ".join(f"{shell_word(path, windows=True)} next" for path in paths)
    script = tmp_path / "paste.ps1"
    script.write_text(
        f"& {shell_word(sys.executable, windows=True)} {shell_word(str(echo), windows=True)} "
        f"{words}\n",
        encoding="utf-8-sig",  # Windows PowerShell 5.1 reads a script without a BOM as ANSI
    )
    completed = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(script)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    argv = json.loads(completed.stdout.strip().splitlines()[-1])
    assert argv[1::2] == ["next"] * len(paths), argv
    assert [os.path.normpath(word) for word in argv[0::2]] == [
        os.path.normpath(path) for path in paths
    ], argv


def test_a_prepare_that_dies_right_after_worktree_add_leaves_nothing_unrecorded(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()
    real = GitRepo.create_worktree

    def add_then_crash(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        real(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "create_worktree", add_then_crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    assert record.state is IntegrationState.PREPARING
    assert record.worktree_state == "NONE"
    assert Path(record.worktree_path).exists(), "the path was recorded before the worktree existed"

    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.INTERRUPTED
    assert outcome.record.worktree_state == "REMOVED"
    assert not Path(record.worktree_path).exists()


# --------------------------------------------------------------------------
# review findings (batch I review): each case below was a confirmed defect
# --------------------------------------------------------------------------


def test_a_target_spelled_in_another_case_is_refused(scene_on_main: Scene) -> None:
    """On a case-insensitive file system `MAIN` resolves through `main`'s loose file; the
    checked-out test would then compare against the wrong name and move the user's branch."""
    scene = scene_on_main
    run_id = scene.accept()
    with pytest.raises(RefusedError) as caught:
        scene.prepare(run_id, target="MAIN")
    assert caught.value.code in {RefusalCode.INVALID_SPEC, RefusalCode.NOT_INTEGRABLE}
    assert scene.store.integrations_for(run_id) == []
    assert scene.tip() == scene.base


def test_checks_that_change_the_shared_git_metadata_stop_hflows_own_git(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check is candidate code. A clean filter it configures in the shared config would run
    inside HFlow's own ``git status`` and ``git worktree remove`` of the integration worktree,
    so the metadata is compared first - as a run compares it at acceptance."""
    run_id = scene.accept()
    real_verify = integrate_module.verify_candidate
    real_run = GitRepo.run
    configured: list[bool] = []
    after_checks: list[tuple[str, ...]] = []

    def verify_then_configure(**kwargs):  # noqa: ANN003
        result = real_verify(**kwargs)
        _git(Path(kwargs["project_root"]), "config", "filter.hflowprobe.clean", "cat")
        configured.append(True)
        return result

    def recording_run(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if configured:
            after_checks.append(args)
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(integrate_module, "verify_candidate", verify_then_configure)
    monkeypatch.setattr(GitRepo, "run", recording_run)
    record = scene.prepare(run_id).record
    monkeypatch.undo()

    assert record.state is IntegrationState.CHECKS_FAILED, record.detail
    assert record.detail.startswith(GIT_METADATA_CHANGED), record.detail
    assert "config@worktree:filter.*.clean" in record.detail
    assert record.worktree_state == "LEFT"
    assert Path(record.worktree_path).exists(), "the worktree is left for the operator"
    assert after_checks and not any(
        args[0] in {"status", "worktree"} for args in after_checks
    ), after_checks
    [note] = [
        note for note in scene.store.notes_for(run_id)
        if note.startswith(f"git_metadata: changed during integration {record.integration_id}")
    ]
    assert "config@worktree:filter.*.clean" in note and "hflow clean" in note
    with pytest.raises(RefusedError):
        scene.apply(record.integration_id, scene.base)
    assert scene.tip() == scene.base
    # Settled: a reconcile reports it and runs no removal either.
    outcome = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.CHECKS_FAILED
    assert Path(record.worktree_path).exists()


def test_a_target_a_checkout_holds_under_another_spelling_is_handed_off_not_moved(
    scene: Scene, tmp_path: Path
) -> None:
    """``git checkout Main`` on NTFS leaves HEAD naming ``refs/heads/Main``, which reads ``main``.

    The exact-name comparison found no checkout, and the compare-and-set moved ``main`` under
    that checkout's index and files (docs/batch-i-integration-plan.md §2.2.4, AGENTS rule 12).
    """
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    assert prepared.state is IntegrationState.READY, prepared.detail
    other = tmp_path / "other checkout"
    _git(scene.repo, "worktree", "add", "-q", "--detach", str(other), "main")
    _git(other, "symbolic-ref", "HEAD", "refs/heads/Main")

    outcome = scene.apply(prepared.integration_id, scene.base)

    assert outcome.record.state is IntegrationState.READY
    assert scene.tip() == scene.base, "a branch held under another spelling is never moved"
    assert any("checked out" in note and str(other.resolve()) in note for note in outcome.notes)
    assert outcome.handoff[0].startswith("git -C ")
    assert prepared.integration_commit in outcome.handoff[0]


def test_a_failed_ref_update_returns_to_ready_and_does_not_exit_zero(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hflow.gitworkspace import GitError

    run_id = scene.accept()
    prepared = scene.prepare(run_id).record

    def refuse(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise GitError("cannot lock ref 'refs/heads/main'")

    monkeypatch.setattr(GitRepo, "update_ref_cas", refuse)
    data = ["--data-dir", str(scene.data_dir)]
    scene.store.close()
    code = main(["integrate", "apply", prepared.integration_id, "--expect-target", scene.base,
                 *data])
    assert code == 3, "a branch that did not move is not a successful apply"
    out = capsys.readouterr().out
    assert "cannot lock ref" in out
    assert (
        f"next: hflow integrate apply {prepared.integration_id} --expect-target "
        f"{scene.base} --data-dir {shell_word(os.path.abspath(scene.data_dir))}\n"
    ) in out
    store = Store(scene.data_dir / "hflow.sqlite")
    try:
        record = store.integration(prepared.integration_id)
        assert record.state is IntegrationState.READY
        assert record.applied_by == "", "nobody applied it, so nobody is named"
    finally:
        store.close()
    assert scene.tip() == scene.base


def test_a_locked_target_fails_the_compare_and_set_and_stays_ready(scene: Scene) -> None:
    """Git's own lock decides: a lock left by another process fails the update with git's words."""
    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    lock = scene.repo / ".git" / "refs" / "heads" / "main.lock"
    lock.write_bytes(b"")
    try:
        outcome = scene.apply(prepared.integration_id, scene.base)
        assert lock.exists(), "HFlow never deletes a Git lock file"
    finally:
        lock.unlink()
    assert outcome.record.state is IntegrationState.READY, outcome.record.detail
    assert "the ref update failed" in outcome.record.detail
    assert "main.lock" in outcome.record.detail, "git's own words name the lock"
    assert outcome.record.applied_by == ""
    assert outcome.handoff and outcome.handoff[0].startswith("hflow integrate apply ")
    assert scene.tip() == scene.base

    done = scene.apply(prepared.integration_id, scene.base)
    assert done.record.state is IntegrationState.INTEGRATED
    assert scene.tip() == prepared.integration_commit


def test_a_prepare_that_dies_inside_worktree_add_is_found_by_its_staging_name(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()

    def add_staging_then_crash(self, name, commit):  # noqa: ANN001
        staging = self.worktree_parent() / f"{name}.staging-4242"
        self.worktree_parent().mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", "--detach", str(staging), commit)
        raise KeyboardInterrupt

    monkeypatch.setattr(GitRepo, "create_worktree", add_staging_then_crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    staging = GitRepo(scene.repo).worktree_parent() / f"{record.integration_id}.staging-4242"
    assert staging.exists()

    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.INTERRUPTED
    assert outcome.record.worktree_state == "REMOVED"
    assert not staging.exists()
    assert GitRepo(scene.repo).worktree_registration(staging) is None


def test_a_removal_git_could_not_finish_is_reported_as_unregistered_not_as_refused(
    scene: Scene,
) -> None:
    """Git deletes the registration even when the directory stays (an open file on Windows)."""
    from tests.test_cleanup_boundaries import _partly_undeletable

    git = GitRepo(scene.repo)
    worktree = git.create_worktree("I-half-removed", git.head)
    # LF bytes, not ``write_text``: this commit goes through the test's own git, which reads the
    # system config (Git for Windows sets core.autocrlf there), while HFlow's git never does. A
    # CRLF file committed as LF then reads as modified to HFlow whenever the index entry is
    # racily clean, and ``worktree remove`` refuses before deleting anything.
    (worktree / "sample.txt").write_bytes(b"held\n")
    _git(worktree, "add", "sample.txt")
    _git(worktree, "commit", "-q", "-m", "a file to hold open")
    assert integrate_module._half_removed(git, worktree) == ""
    with _partly_undeletable(worktree):
        with pytest.raises(GitError) as failed:
            git.remove_worktree_checked(worktree)
        assert worktree.is_dir() and git.worktree_registration(worktree) is None, failed.value
        assert "no longer registers it" in integrate_module._half_removed(git, worktree)


def test_a_worktree_already_removed_is_recorded_removed_not_left(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()

    def crash(**kwargs):  # noqa: ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(integrate_module, "verify_candidate", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    _git(scene.repo, "worktree", "remove", record.worktree_path)

    _gone(monkeypatch)
    outcome = reconcile_integration(
        scene.store, integration_id=record.integration_id, controller_build=BUILD
    )
    assert outcome.record.state is IntegrationState.INTERRUPTED
    assert outcome.record.worktree_state == "REMOVED"


def test_an_owner_that_cannot_be_judged_needs_an_attestation(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = scene.accept()

    def crash(**kwargs):  # noqa: ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(integrate_module, "verify_candidate", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    monkeypatch.setattr(
        integrate_module, "probe", lambda identity: Probe("unknown", "test: another host")
    )
    data = ["--data-dir", str(scene.data_dir)]
    scene.store.close()

    assert main(["integrate", "reconcile", record.integration_id, *data]) == 5
    assert "--owner-gone" in capsys.readouterr().err
    assert main(["integrate", "reconcile", record.integration_id, "--owner-gone", *data]) == 4
    capsys.readouterr()
    # The documented placeholder, pasted as printed, is nobody's attestation.
    def stored() -> object:
        reader = Store(scene.data_dir / "hflow.sqlite")
        try:
            return reader.integration(record.integration_id)
        finally:
            reader.close()

    before = stored()
    for placeholder in ("<what you know>", "<what you know and why>", "what you know and why"):
        assert main(["integrate", "reconcile", record.integration_id, "--owner-gone", "--attest",
                     placeholder, *data]) == 4
        assert "is a placeholder" in capsys.readouterr().err
    # The attestation is recorded whole or refused: longer than the 500 characters the run's
    # audit note holds is a usage error, never a silent cut.
    assert main(["integrate", "reconcile", record.integration_id, "--owner-gone", "--attest",
                 "the machine was rebooted " + "x" * 500, *data]) == 4
    assert "at most 500" in capsys.readouterr().err
    assert stored() == before, "nothing was settled"
    attestation = "the machine was rebooted " + "y" * (500 - len("the machine was rebooted "))
    assert main(["integrate", "reconcile", record.integration_id, "--owner-gone", "--attest",
                 attestation, *data]) == 3
    assert "interrupted" in capsys.readouterr().out

    # A process HFlow sees running is never overridden by an attestation.
    store = Store(scene.data_dir / "hflow.sqlite")
    try:
        notes = store.notes_for(run_id)
        assert any("attested by" in note and attestation in note for note in notes)
    finally:
        store.close()


def test_a_running_owner_is_never_overridden(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = scene.accept()

    def crash(**kwargs):  # noqa: ANN003
        raise KeyboardInterrupt

    monkeypatch.setattr(integrate_module, "verify_candidate", crash)
    with pytest.raises(KeyboardInterrupt):
        scene.prepare(run_id)
    monkeypatch.undo()
    [record] = scene.store.integrations_for(run_id)
    # This process is the recorded owner and it is running.
    with pytest.raises(integrate_module.IntegrationBusy):
        reconcile_integration(
            scene.store,
            integration_id=record.integration_id,
            controller_build=BUILD,
            owner_gone_attested=True,
            attested_by="tester",
        )
    assert scene.store.integration(record.integration_id).state is IntegrationState.CHECKING


def test_an_integrated_run_is_not_prepared_again_into_the_same_branch(scene: Scene) -> None:
    run_id = scene.accept()
    first = scene.prepare(run_id).record
    scene.apply(first.integration_id, scene.base)
    # Even after the change is reverted on the branch, the run's delivery already landed there.
    _commit_on(scene.repo, "main", "app.txt", "value = 1\n", "revert")
    with pytest.raises(RefusedError) as caught:
        scene.prepare(run_id)
    assert caught.value.code is RefusalCode.NOT_INTEGRABLE
    assert first.integration_id in str(caught.value)


def test_a_hand_merge_is_recorded_before_a_new_prepare_could_supersede_it(
    scene_on_main: Scene,
) -> None:
    scene = scene_on_main
    run_id = scene.accept()
    first = scene.prepare(run_id).record
    _git(scene.repo, "merge", "--ff-only", "-q", first.integration_commit)

    with pytest.raises(RefusedError):
        scene.prepare(run_id)
    settled = scene.store.integration(first.integration_id)
    assert settled.state is IntegrationState.INTEGRATED
    assert settled.basis == "operator_merge_observed"
    assert scene.store.integration_receipt(first.integration_id) is not None


def test_unknown_integration_ids_exit_usage(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    data = ["--data-dir", str(scene.data_dir)]
    scene.store.close()
    assert main(["integrate", "apply", "G-missing", "--expect-target", "0" * 40, *data]) == 4
    assert main(["integrate", "reconcile", "G-missing", *data]) == 4
    assert "unknown integration" in capsys.readouterr().err


def test_a_concurrent_settlement_is_reported_not_raised(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hflow.store import IntegrationConflict

    run_id = scene.accept()
    prepared = scene.prepare(run_id).record
    _commit_on(scene.repo, "main", "notes.txt", "someone else\n", "moved")

    def lost_race(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise IntegrationConflict("integration was settled by another process")

    monkeypatch.setattr(Store, "update_integration", lost_race)
    outcome = reconcile_integration(
        scene.store, integration_id=prepared.integration_id, controller_build=BUILD
    )
    assert any("another process changed this integration" in note for note in outcome.notes)
