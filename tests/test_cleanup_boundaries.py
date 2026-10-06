"""Cleanup cannot race a live admission or infer a missing registration."""

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hflow.cleanup as cleanup
from hflow.contracts import TaskState, WorkspaceProvenance, WorkspaceSpec, canonical_json, digest_of
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.gitworkspace import GitError, GitRepo
from hflow.store import Store
from tests.test_batch_e_repair import (
    FIXED_SOURCE, FailingOnceThenPassing, _controller, _git, _project, _request,
    _scoped_spec, scoped_repo,  # noqa: F401 - pytest fixture
)
from hflow.verify import CheckRunners


@pytest.fixture()
def managed(store, task_spec, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "sample.txt").write_text("sample\n")
    (repo_path / ".gitignore").write_text(".env\n")
    _git(repo_path, "init", "-q", "-b", "main")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "fixture")
    repo = GitRepo.discover(repo_path)
    base = repo.resolve_commit("HEAD")
    worktree = repo.create_worktree("R-clean", base)
    spec = task_spec.model_copy(update={"workspace": WorkspaceSpec(mode="worktree", base_commit=base)})
    store.create_run(
        run_id="R-clean", project_id="P-clean", spec=spec,
        spec_digest=digest_of(spec.model_dump(mode="json")), controller_build="test",
        checks_digest="checks", turn_limit=4, repair_limit=0,
    )
    provenance = WorkspaceProvenance(
        project_root=str(repo_path), git_common_dir=str((repo.root / repo.common_dir).resolve()),
        worktree_path=str(worktree),
    )
    store.record_worktree("R-clean", worktree, provenance=provenance)
    store.conn.execute("UPDATE runs SET task_state='BLOCKED' WHERE run_id='R-clean'")
    return repo, worktree, provenance


@pytest.mark.parametrize("state", ["DRAFT", "READY"])
def test_clean_refuses_during_admission_then_original_run_completes(
    state, tmp_path, scoped_repo, monkeypatch,
):
    store = Store(tmp_path / "hflow.sqlite")
    project = _project()
    base = _git(scoped_repo, "rev-parse", "HEAD").strip()
    spec = _scoped_spec(base, allow=["src"])
    driver = FakeDriver(scoped_repo, FakeScript(write_plan={"src/parser.py": FIXED_SOURCE}))
    owner = _controller(
        store, project_root=scoped_repo, spec=spec, driver=driver,
        runners=CheckRunners({"fake": FailingOnceThenPassing(fail_first=None)}),
    )
    reached, release = threading.Event(), threading.Event()
    if state == "DRAFT":
        original = store.record_worktree

        def stop_after_record(*args, **kwargs):
            original(*args, **kwargs)
            reached.set()
            assert release.wait(20)

        monkeypatch.setattr(store, "record_worktree", stop_after_record)
    else:
        original = store.set_task_state

        def stop_after_ready(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2] == TaskState.READY:
                reached.set()
                assert release.wait(20)
            return result

        monkeypatch.setattr(store, "set_task_state", stop_after_ready)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(owner.run_task, _request(project=project, spec=spec, project_root=scoped_repo))
            try:
                assert reached.wait(20)
                with Store(store.path) as cleaner:
                    row = cleaner.list_runs()[0]
                    run_id = row["run_id"]
                    worktree = Path(row["worktree_path"])
                    assert row["task_state"] == state and cleaner.attempts_for(run_id) == []
                    result = cleanup.apply_cleanup(cleaner, run_id)
                    assert result["status"] == "REFUSED"
                    assert worktree.exists() and cleaner.get_run(run_id)["cleanup_intent_at"] is None
                    assert driver.started == []
            finally:
                release.set()
            outcome = future.result(timeout=30)
            assert outcome.task_state == TaskState.ACCEPTED
            assert sum(item.role == "implementer" for item in driver.started) == 1
            assert cleanup.plan_cleanup(store, outcome.run_id).allowed
    finally:
        store.close()


@pytest.mark.parametrize("drift", ["state", "path", "provenance"])
def test_claim_rechecks_plan_facts_before_writing_intent(store, managed, monkeypatch, drift):
    _, worktree, provenance = managed
    original = store.record_cleanup_intent

    def change_before_claim(*args, **kwargs):
        if drift == "state":
            store.conn.execute("UPDATE runs SET task_state='READY' WHERE run_id='R-clean'")
        elif drift == "path":
            store.conn.execute("UPDATE runs SET worktree_path=? WHERE run_id='R-clean'", (str(worktree.parent / "other"),))
        else:
            altered = provenance.model_copy(update={"project_root": str(worktree.parent / "other")})
            store.conn.execute(
                "UPDATE run_notes SET note=? WHERE run_id='R-clean' AND note LIKE 'workspace_provenance:%'",
                ("workspace_provenance: " + canonical_json(altered.model_dump()),),
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "record_cleanup_intent", change_before_claim)
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REFUSED"
    assert store.get_run("R-clean")["cleanup_intent_at"] is None
    assert worktree.exists()


def test_retry_rechecks_ignored_files_after_the_first_remove_error(store, managed, monkeypatch):
    _, worktree, _ = managed
    original = GitRepo.remove_worktree_checked
    calls = []

    def fail_once(repo, path):
        calls.append(str(path))
        if len(calls) == 1:
            (path / ".env").write_text("user-owned secret\n")
            assert not cleanup.plan_cleanup(store, "R-clean").allowed
            raise GitError("a handle was still closing")
        return original(repo, path)

    monkeypatch.setattr(GitRepo, "remove_worktree_checked", fail_once)
    monkeypatch.setattr(cleanup.time, "sleep", lambda _: None)
    result = cleanup.apply_cleanup(store, "R-clean")
    assert result["status"] == "REFUSED" and not result["applied"]
    assert len(calls) == 1
    assert (worktree / ".env").read_text() == "user-owned secret\n"
    assert store.get_run("R-clean")["cleanup_intent_at"] is None
    assert store.get_run("R-clean")["cleanup_done_at"] is None


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_hidden_index_changes_are_preserved_until_flags_and_dirty_bytes_are_resolved(store, managed, flag):
    repo, worktree, _ = managed
    path = worktree / "sample.txt"
    original = path.read_bytes()
    assert cleanup.plan_cleanup(store, "R-clean").allowed
    _git(worktree, "update-index", "--" + flag, "sample.txt")
    path.write_text("uncommitted user bytes\n")
    assert repo.status_report(worktree).changed == ()
    assert repo.index_flagged_paths(worktree) == ["sample.txt"]
    plan = cleanup.plan_cleanup(store, "R-clean")
    assert not plan.allowed
    assert "index_flags_hide_changes" in [item["reason"] for item in plan.refusals]
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REFUSED"
    assert path.read_text() == "uncommitted user bytes\n"
    assert store.get_run("R-clean")["cleanup_intent_at"] is None
    _git(worktree, "update-index", "--no-" + flag, "sample.txt")
    assert repo.index_flagged_paths(worktree) == []
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REFUSED"
    assert path.read_text() == "uncommitted user bytes\n"
    path.write_bytes(original)
    assert cleanup.plan_cleanup(store, "R-clean").allowed
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REMOVED"
    assert not worktree.exists()


@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_retry_rechecks_new_hidden_index_flags(store, managed, monkeypatch, flag):
    repo, worktree, _ = managed
    calls = []

    def plant_flag(instance, path):
        calls.append(str(path))
        _git(path, "update-index", "--" + flag, "sample.txt")
        (path / "sample.txt").write_text("hidden user bytes\n")
        assert repo.status_report(path).changed == ()
        raise GitError("a handle was still closing")

    monkeypatch.setattr(GitRepo, "remove_worktree_checked", plant_flag)
    monkeypatch.setattr(cleanup.time, "sleep", lambda _: None)
    result = cleanup.apply_cleanup(store, "R-clean")
    assert result["status"] == "REFUSED" and not result["applied"]
    assert len(calls) == 1
    assert "index_flags_hide_changes" in [item["reason"] for item in result["plan"]["refusals"]]
    assert (worktree / "sample.txt").read_text() == "hidden user bytes\n"
    assert store.get_run("R-clean")["cleanup_intent_at"] is None


def test_directory_vanishing_after_claim_never_marks_residual_registration_removed(store, managed, monkeypatch):
    _, worktree, provenance = managed
    original = store.record_cleanup_intent

    def vanish_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        shutil.rmtree(worktree)
        return result

    monkeypatch.setattr(store, "record_cleanup_intent", vanish_after_claim)
    result = cleanup.apply_cleanup(store, "R-clean")
    assert result["status"] == "PARTIAL" and not result["applied"]
    assert cleanup.git_registration_exists(provenance.git_common_dir, worktree)
    assert store.get_run("R-clean")["cleanup_done_at"] is None
    assert store.get_run("R-clean")["cleanup_intent_at"] is not None


def test_postclaim_plan_cannot_redirect_deletion_to_another_valid_worktree(store, managed, monkeypatch):
    repo, worktree, provenance = managed
    other = repo.create_worktree("R-other", repo.resolve_commit("HEAD"))
    original = store.record_cleanup_intent

    def redirect_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        altered = provenance.model_copy(update={"worktree_path": str(other)})
        store.conn.execute("UPDATE runs SET worktree_path=? WHERE run_id='R-clean'", (str(other),))
        store.conn.execute(
            "UPDATE run_notes SET note=? WHERE run_id='R-clean' AND note LIKE 'workspace_provenance:%'",
            ("workspace_provenance: " + canonical_json(altered.model_dump()),),
        )
        assert cleanup.plan_cleanup(store, "R-clean").allowed
        return result

    monkeypatch.setattr(store, "record_cleanup_intent", redirect_after_claim)
    result = cleanup.apply_cleanup(store, "R-clean")
    assert result["status"] == "REFUSED" and not result["applied"]
    assert worktree.exists() and other.exists()
    assert store.get_run("R-clean")["cleanup_intent_at"] is None


def test_missing_directory_with_residual_registration_keeps_intent(store, managed):
    repo, worktree, _ = managed
    store.record_cleanup_intent("R-clean", "test")
    shutil.rmtree(worktree)
    for _ in range(2):
        assert cleanup.reconcile_cleanup(store, "R-clean")["status"] == "PARTIAL"
        assert store.get_run("R-clean")["cleanup_done_at"] is None
        assert store.get_run("R-clean")["cleanup_intent_at"] is not None
    # An explicit operator Git removal releases the administrative entry as well.
    repo.run("worktree", "remove", str(worktree))
    assert cleanup.reconcile_cleanup(store, "R-clean")["status"] == "REMOVED"
    completed = store.get_run("R-clean")["cleanup_done_at"]
    assert cleanup.reconcile_cleanup(store, "R-clean")["status"] == "REMOVED"
    assert store.get_run("R-clean")["cleanup_done_at"] == completed


@pytest.mark.parametrize("error", [GitError("query failed"), subprocess.TimeoutExpired("git", 1)])
def test_registration_query_failure_is_unknown_without_completion(store, managed, monkeypatch, error):
    _, worktree, _ = managed
    store.record_cleanup_intent("R-clean", "test")
    shutil.rmtree(worktree)

    def fail(*args):
        raise error

    monkeypatch.setattr(cleanup, "git_registration_exists", fail)
    assert cleanup.reconcile_cleanup(store, "R-clean")["status"] == "REGISTRATION_UNKNOWN"
    assert store.get_run("R-clean")["cleanup_intent_at"] is not None
    assert store.get_run("R-clean")["cleanup_done_at"] is None


@pytest.mark.parametrize("root_backed", [False, True])
def test_legacy_reconciliation_uses_only_a_recorded_source(store, managed, monkeypatch, root_backed):
    repo, worktree, _ = managed
    store.record_cleanup_intent("R-clean", "test")
    store.conn.execute("DELETE FROM run_notes WHERE run_id='R-clean' AND note LIKE 'workspace_provenance:%'")
    repo.remove_worktree_checked(worktree)
    monkeypatch.setattr(store, "root_budget_for_run", lambda _: {"repo_path": str(repo.root)} if root_backed else None)
    result = cleanup.reconcile_cleanup(store, "R-clean")
    assert result["status"] == ("REMOVED" if root_backed else "REGISTRATION_UNKNOWN")
    assert bool(store.get_run("R-clean")["cleanup_done_at"]) == root_backed


def test_legacy_rootless_apply_refuses_an_existing_registered_worktree(store, managed):
    _, worktree, _ = managed
    store.conn.execute("DELETE FROM run_notes WHERE run_id='R-clean' AND note LIKE 'workspace_provenance:%'")
    plan = cleanup.plan_cleanup(store, "R-clean")
    assert not plan.allowed
    assert "workspace_provenance_missing" in [item["reason"] for item in plan.refusals]
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REFUSED"
    assert store.get_run("R-clean")["cleanup_intent_at"] is None
    assert worktree.exists()


def test_legacy_root_source_is_checked_against_the_actual_worktree_common_dir(store, managed, tmp_path, monkeypatch):
    _, worktree, _ = managed
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "commit", "--allow-empty", "-q", "-m", "fixture")
    store.conn.execute("DELETE FROM run_notes WHERE run_id='R-clean' AND note LIKE 'workspace_provenance:%'")
    monkeypatch.setattr(store, "root_budget_for_run", lambda _: {"repo_path": str(other)})
    plan = cleanup.plan_cleanup(store, "R-clean")
    assert not plan.allowed
    assert "provenance_mismatch" in [item["reason"] for item in plan.refusals]
    assert cleanup.apply_cleanup(store, "R-clean")["status"] == "REFUSED"
    assert worktree.exists() and store.get_run("R-clean")["cleanup_intent_at"] is None


@pytest.mark.parametrize("record", ["dsh_context", "repair", "effective_config"])
@pytest.mark.parametrize("invalid", ["{invalid", "{}", "[" * 2000 + "0" + "]" * 2000], ids=["json", "contract", "deep_json"])
def test_bad_record_reports_exit_six_then_cancel_and_resume_still_work(
    store, run_request, check_runner, capsys, record, invalid,
):
    import json

    from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_RECORD_UNREADABLE, main
    from hflow.controller import Controller
    from hflow.ids import new_evidence_id, utc_now

    class InterruptedDriver(FakeDriver):
        def start(self, request):
            self.started.append(request)
            self._report_spawn(request, created=True, detail="the invocation began")
            raise KeyboardInterrupt

    owner = Controller(
        store, InterruptedDriver(run_request.project_root), controller_build="test",
        runners=CheckRunners({"fake": check_runner, "command": check_runner}),
        data_dir=store.path.parent, production=False,
    )
    with pytest.raises(KeyboardInterrupt):
        owner.run_task(run_request)
    run_id = store.list_runs()[0]["run_id"]
    if record == "repair":
        store.conn.execute(
            "INSERT INTO run_repair_records(record_id,run_id,record_json,created_at) VALUES(?,?,?,?)",
            (new_evidence_id(), run_id, invalid, utc_now()),
        )
    else:
        note = record + ": " + invalid
        store.record_note(run_id, note, limit=len(note))
    common = ["--data-dir", str(store.path.parent), "--project-root", str(run_request.project_root)]
    snapshot = list(store.conn.iterdump())
    for verb in ("status", "report"):
        for json_mode in ([], ["--json"]):
            assert main([verb, run_id, *common, *json_mode]) == EXIT_RECORD_UNREADABLE
            output = capsys.readouterr()
            assert record in output.err and "Traceback" not in output.err
            assert len(output.err.strip().splitlines()) == 1 and output.out == ""
            assert list(store.conn.iterdump()) == snapshot
    assert main(["cancel", run_id, *common, "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    assert main(["resume", run_id, *common, "--json"]) == EXIT_BLOCKED
    resumed = capsys.readouterr()
    assert "Traceback" not in resumed.err
    assert json.loads(resumed.out)["run_id"] == run_id
    if record == "effective_config":
        assert any(note.startswith("stored_config_unreadable:") for note in store.notes_for(run_id))
