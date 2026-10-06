"""Continuation refuses drift before claiming, spending or changing a workspace."""

from pathlib import Path

import pytest

from hflow.cli import EXIT_IN_PROGRESS, _outcome_exit_code
from hflow.contracts import (
    EffectiveConfig, LaunchConfig, LaunchFileDigest, RoleConfig, RunRequest,
    TaskState, WorkspaceProvenance,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.gitworkspace import GitError, GitRepo
from hflow.ownership import lock_path_for, new_owner_token
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from .test_m2_slice import FIXED_SOURCE, _git, _project, _task, sample_repo
from .test_owner_lease import _dead_identity, windows_only


def _controller(store, request, script, config=None):
    runner = FakeCheckRunner()
    return Controller(
        store, FakeDriver(Path(request.project_root), script), controller_build="binding-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        effective_config=config, production=False,
    )


def _config(content="a"):
    return EffectiveConfig(source="command_line", profile_id="original", roles=[RoleConfig(
        role="implementer", agent="offline", harness="dsh", driver="fake-offline",
        driver_id="fake-offline", launch=LaunchConfig(
            driver_id="fake-offline", harness="dsh", content_digests=[LaunchFileDigest(
                kind="client_entry", path="offline-entry.py", sha256=content * 64, size=1,
            )],
        ),
    )])


def _seed(controller, request, *, state=TaskState.DRAFT, dead=False):
    run_id = "R-binding"
    owner = {}
    if dead:
        token = new_owner_token()
        lock = lock_path_for(controller.store.path.parent, token)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"")
        owner = dict(controller_id="departed", owner_token=token, owner_identity=_dead_identity())
    spec = request.task
    binding = controller._admission_binding(run_id, request)
    controller.store.create_run(
        run_id=run_id, project_id=request.project.project_id, spec=spec,
        spec_digest=spec.spec_digest(), controller_build="binding-test",
        checks_digest=request.project.checks_digest(), turn_limit=spec.budget.max_agent_turns,
        repair_limit=spec.budget.max_repair_cycles, admission_binding=binding,
        effective_config=controller.effective_config, **owner,
    )
    if state != TaskState.DRAFT:
        controller.store.set_task_state(run_id, [TaskState.DRAFT], state)
    return run_id, binding


def _snapshot(store, run_id):
    return (dict(store.get_run(run_id)), store.notes_for(run_id),
            list(store.attempts_for(run_id)), list(store.invocations_for(run_id)))


@pytest.mark.parametrize("drift", ["config", "project", "missing_check", "deadline", "launch", "config_missing", "config_corrupt"])
@pytest.mark.parametrize("state", [TaskState.DRAFT, TaskState.READY])
def test_drift_refuses_before_claim_and_restore_continues(store, run_request, fake_script, drift, state):
    owner = _controller(store, run_request, fake_script, _config())
    run_id, _ = _seed(owner, run_request, state=state)
    request = run_request
    config = _config()
    if drift == "config":
        config = config.model_copy(update={"profile_id": "different"})
    elif drift == "project":
        check = request.project.checks[0].model_copy(update={"timeout_seconds": 3})
        project = request.project.model_copy(update={"checks": [check, *request.project.checks[1:]]})
        request = request.model_copy(update={"project": project})
    elif drift == "missing_check":
        request = request.model_copy(update={"project": request.project.model_copy(update={"checks": []})})
    elif drift == "deadline":
        request = request.model_copy(update={"deadline_seconds": 42})
    elif drift == "launch":
        config = _config("b")
    else:
        with store.transaction() as conn:
            if drift == "config_missing":
                conn.execute("DELETE FROM run_notes WHERE run_id=? AND note LIKE 'effective_config: %'", (run_id,))
            else:
                conn.execute("UPDATE run_notes SET note='effective_config: {bad' WHERE run_id=? AND note LIKE 'effective_config: %'", (run_id,))
    before = _snapshot(store, run_id)
    successor = _controller(store, request, fake_script, config)
    outcome = successor.run_task(request)
    assert outcome.task_state == state and _outcome_exit_code(outcome) == EXIT_IN_PROGRESS
    assert successor.driver.started == []
    assert _snapshot(store, run_id) == before
    assert "continuation refused" in " ".join(outcome.notes)
    successor.close()
    if drift in {"config_missing", "config_corrupt"}:
        return  # Unknown historical facts are never reconstructed from the new request.
    restored = _controller(store, run_request, fake_script, _config())
    accepted = restored.run_task(run_request)
    assert accepted.run_id == run_id and accepted.task_state is TaskState.ACCEPTED
    assert len(restored.driver.started) == 2
    assert store.get_run(run_id)["turns_reserved"] == 2
    restored.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "conflicting_duplicate"])
def test_unknown_admission_is_not_backfilled(store, run_request, fake_script, damage):
    controller = _controller(store, run_request, fake_script)
    run_id, _ = _seed(controller, run_request)
    with store.transaction() as conn:
        if damage == "missing":
            conn.execute("DELETE FROM run_notes WHERE run_id=? AND note LIKE 'admission_binding: %'", (run_id,))
        elif damage == "corrupt":
            conn.execute("UPDATE run_notes SET note='admission_binding: {bad' WHERE run_id=? AND note LIKE 'admission_binding: %'", (run_id,))
        else:
            conn.execute("INSERT INTO run_notes(note_id,run_id,note,created_at) VALUES('E-duplicate',?,'admission_binding: {}','2026-10-05T00:00:00Z')", (run_id,))
    before = _snapshot(store, run_id)
    outcome = controller.run_task(run_request)
    assert _outcome_exit_code(outcome) == EXIT_IN_PROGRESS
    assert controller.driver.started == [] and _snapshot(store, run_id) == before
    assert "cancel this run" in " ".join(outcome.notes)
    controller.close()


@windows_only
@pytest.mark.parametrize("state", [TaskState.DRAFT, TaskState.READY])
def test_real_dead_owner_continues_only_matching_binding(store, run_request, fake_script, state):
    original = _controller(store, run_request, fake_script)
    run_id, _ = _seed(original, run_request, state=state, dead=True)
    successor = _controller(store, run_request, fake_script)
    before = _snapshot(store, run_id)
    changed = run_request.model_copy(update={"deadline_seconds": 42})
    assert _outcome_exit_code(successor.run_task(changed)) == EXIT_IN_PROGRESS
    assert _snapshot(store, run_id) == before and successor.driver.started == []
    accepted = successor.run_task(run_request)
    assert accepted.task_state is TaskState.ACCEPTED and len(successor.driver.started) == 2
    assert store.get_run(run_id)["claim_generation"] == 2
    successor.close()


@pytest.mark.parametrize("damage", ["base_moves", "dirty", "foreign", "empty", "unregistered", "assume_unchanged", "skip_worktree"])
def test_reused_worktree_is_verified_before_claim(store, sample_repo, damage):
    repo = GitRepo.discover(sample_repo)
    project, task = _project(sample_repo), _task(sample_repo, "HEAD")
    request = RunRequest(task=task, project=project, project_root=sample_repo, workspace_root=sample_repo)
    script = FakeScript(write_plan={})
    controller = _controller(store, request, script)
    run_id, binding = _seed(controller, request, state=TaskState.READY)
    target = Path(binding.worktree_path)
    if damage == "foreign":
        target.mkdir(parents=True)
        _git(target, "init", "-q")
        (target / "owned.txt").write_text("someone else's work", encoding="utf-8")
        _git(target, "add", ".")
        _git(target, "commit", "-qm", "foreign")
    elif damage == "empty":
        target.mkdir(parents=True)
    else:
        repo.create_worktree(run_id, binding.base_commit)
    store.record_worktree(run_id, target, provenance=WorkspaceProvenance(
        project_root=binding.project_root, git_common_dir=binding.git_common_dir, worktree_path=binding.worktree_path,
    ))
    if damage == "base_moves":
        (sample_repo / "later.txt").write_text("new base", encoding="utf-8")
        _git(sample_repo, "add", ".")
        _git(sample_repo, "commit", "-qm", "advance HEAD")
    elif damage == "dirty":
        (target / "user-draft.txt").write_text("keep this", encoding="utf-8")
    elif damage in {"assume_unchanged", "skip_worktree"}:
        flag = "--assume-unchanged" if damage == "assume_unchanged" else "--skip-worktree"
        _git(target, "update-index", flag, "src/textkit/__init__.py")
        (target / "src/textkit/__init__.py").write_text("hidden draft", encoding="utf-8")
        assert not GitRepo.discover(target).status_report().changed
    elif damage == "unregistered":
        admin = _git(target, "rev-parse", "--absolute-git-dir").strip()
        (Path(admin) / "gitdir").write_text(str(sample_repo.parent / "elsewhere" / ".git"), encoding="utf-8")
    before = _snapshot(store, run_id)
    outcome = controller.run_task(request)
    assert _outcome_exit_code(outcome) == EXIT_IN_PROGRESS
    assert controller.driver.started == [] and _snapshot(store, run_id) == before
    assert target.exists()
    if damage == "dirty":
        assert (target / "user-draft.txt").read_text(encoding="utf-8") == "keep this"
    if damage in {"assume_unchanged", "skip_worktree"}:
        assert (target / "src/textkit/__init__.py").read_text(encoding="utf-8") == "hidden draft"
    if damage in {"foreign", "empty"}:
        with pytest.raises(GitError, match="reuse refused"):
            repo.create_worktree(run_id, binding.base_commit)
        assert target.exists()
    controller.close()


@windows_only
@pytest.mark.parametrize("state", [TaskState.DRAFT, TaskState.READY])
def test_matching_dead_owner_worktree_continues_from_recorded_base(store, sample_repo, state):
    repo = GitRepo.discover(sample_repo)
    request = RunRequest(task=_task(sample_repo, "HEAD"), project=_project(sample_repo),
                         project_root=sample_repo, workspace_root=sample_repo)
    script = FakeScript(write_plan={"src/textkit/__init__.py": FIXED_SOURCE})
    original = _controller(store, request, script)
    run_id, binding = _seed(original, request, state=state, dead=True)
    worktree = repo.create_worktree(run_id, binding.base_commit)
    store.record_worktree(run_id, worktree, provenance=WorkspaceProvenance(
        project_root=binding.project_root, git_common_dir=binding.git_common_dir,
        worktree_path=binding.worktree_path,
    ))
    successor = _controller(store, request, script)
    accepted = successor.run_task(request)
    assert accepted.task_state is TaskState.ACCEPTED
    assert accepted.receipt.candidate.base_commit == binding.base_commit
    assert [item.role for item in successor.driver.started] == ["implementer", "reviewer"]
    assert store.get_run(run_id)["turns_reserved"] == 2
    assert store.get_run(run_id)["claim_generation"] == 2
    successor.close()


def test_fresh_approval_covers_same_execution_without_rebinding(store, run_request, fake_script):
    from .test_batch_e_dispatch import _authorization, _binding, _limits

    binding = _binding(store, run_request.task, run_request.project_root)
    limits = _limits()
    store.register_root_budget(binding, limits)
    original = _controller(store, run_request, fake_script)
    original.root_binding, original.root_limits = binding, limits
    run_id, admitted = _seed(original, run_request)
    old = _authorization(spec=run_request.task, binding=binding, limits=limits,
                         project_root=run_request.project_root, authorization_id="AUTH-old")
    store.register_authorization(old.as_store_record())
    with store.transaction() as conn:
        conn.execute("UPDATE authorizations SET used_top_level_submissions=4 WHERE authorization_id='AUTH-old'")
    fresh = _authorization(spec=run_request.task, binding=binding, limits=limits,
                           project_root=run_request.project_root, authorization_id="AUTH-fresh")
    successor = _controller(store, run_request, fake_script)
    successor.root_binding, successor.root_limits, successor.authorization = binding, limits, fresh
    outcome = successor.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED
    assert store.admission_binding_for(run_id) == admitted
    assert store.authorization_state("AUTH-old")["used_top_level_submissions"] == 4
    assert store.authorization_state("AUTH-fresh")["used_top_level_submissions"] == 2
    assert store.root_budget_view(binding.root_id).used_top_level_submissions == 2
    successor.close()
