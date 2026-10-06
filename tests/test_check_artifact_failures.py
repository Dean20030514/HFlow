"""Artifact IO failures settle verification without buying another model invocation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import hflow.verify as verify_module
from hflow.authorization import AuthorizationBinding, AuthorizationRecord
from hflow.contracts import (
    AcceptanceCriterion,
    CheckDef,
    EvidenceStatus,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    RepairDecision,
    RepairPolicy,
    RootBudgetBinding,
    RootBudgetLimits,
    RunRequest,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.drivers.winjob import process_gone
from hflow.store import Store
from hflow.verify import CheckRunners, CommandCheckRunner, _reference_field


def _check(exit_code: int = 0) -> CheckDef:
    return CheckDef(
        id="unit", kind="command", timeout_seconds=30,
        argv=[
            sys.executable, "-c",
            "import os, sys; print(os.getpid()); print('check stderr', file=sys.stderr); "
            f"sys.exit({exit_code})",
        ],
    )


def _fail_manifest(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    writer = verify_module.write_artifact_manifest
    original_write = Path.write_text

    def partial_write(path, data, *args, **kwargs):
        original_write(path, data[: len(data) // 2], *args, **kwargs)
        raise OSError("manifest storage unavailable")

    def failing_writer(directory, payload):
        # Fail the real writer's named IO operation after the child has finished;
        # directory/stream setup remains real and unaffected.
        if operation == "partial_write":
            with patch.object(Path, "write_text", partial_write):
                return writer(directory, payload)
        with patch.object(Path, operation, side_effect=OSError("manifest storage unavailable")):
            return writer(directory, payload)

    monkeypatch.setattr(verify_module, "write_artifact_manifest", failing_writer)


@pytest.mark.parametrize("stage", ["factory", "directory", "temporary_directory"])
def test_artifact_directory_failures_never_start_a_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    artifact_dir = tmp_path / "artifact"

    def factory(_check, _evidence):
        if stage == "factory":
            raise OSError("factory storage unavailable")
        return artifact_dir

    runner = CommandCheckRunner(artifact_factory=None if stage == "temporary_directory" else factory)
    if stage == "directory":
        artifact_dir.write_text("a file cannot be an artifact directory", encoding="utf-8")
    if stage == "temporary_directory":
        monkeypatch.setattr(
            verify_module.tempfile, "mkdtemp",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("temporary storage unavailable")),
        )

    def unexpected_execution(*_args):
        pytest.fail("a check must not launch without its artifact directory")

    monkeypatch.setattr(runner, "_execute", unexpected_execution)
    outcome = runner.run(_check(), tmp_path, 30)
    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "no_artifact_dir"
    assert outcome.exit_code is None
    assert outcome.command == _check().argv
    assert outcome.artifacts == {}
    assert not outcome.artifact_path


@pytest.mark.parametrize("stream", ["stdout.txt", "stderr.txt"])
def test_stream_file_open_failure_never_starts_a_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str,
) -> None:
    artifact_dir = tmp_path / "artifact"
    original = Path.open

    def failing_open(path, *args, **kwargs):
        if path == artifact_dir / stream:
            raise OSError("stream storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    def unexpected_launch(*_args, **_kwargs):
        pytest.fail("a check must not launch when a stream file could not be opened")

    monkeypatch.setattr(verify_module, "popen_in_boundary", unexpected_launch)
    runner = CommandCheckRunner(artifact_factory=lambda *_: artifact_dir)
    outcome = runner.run(_check(), tmp_path, 30)
    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "output_capture_error"
    assert outcome.exit_code is None


@pytest.mark.parametrize("operation", ["mkdir", "write_text", "partial_write"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_manifest_failure_preserves_the_finished_check_and_readable_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, exit_code: int,
) -> None:
    artifact_dir = tmp_path / "artifact"
    _fail_manifest(monkeypatch, operation)
    runner = CommandCheckRunner(artifact_factory=lambda *_: artifact_dir)
    check = _check(exit_code)
    outcome = runner.run(check, tmp_path, 30)

    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "output_capture_error"
    assert outcome.exit_code == exit_code
    assert outcome.command == check.argv
    assert outcome.artifact_path == str(artifact_dir)
    assert "manifest storage unavailable" in outcome.detail
    manifest = artifact_dir / "artifact.json"
    if operation == "partial_write":
        assert manifest.is_file()
        with pytest.raises(json.JSONDecodeError):
            json.loads(manifest.read_text(encoding="utf-8"))
    else:
        assert not manifest.exists()
    for name, reference in outcome.artifacts.items():
        data = Path(reference["path"]).read_bytes()
        assert reference["retained_bytes"] == reference["total_bytes"] == len(data)
        assert reference["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert reference["failed"] is False
        assert reference["truncated"] is False
        assert data, name
    assert process_gone(int((artifact_dir / "stdout.txt").read_text().strip())) is True


@pytest.mark.parametrize("stage", ["factory", "directory", "manifest_mkdir", "manifest_write"])
def test_artifact_io_failure_blocks_the_run_without_repair_or_reviewer(
    tmp_path: Path, project_root: Path, project, task_spec, monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    # A real isolated Git candidate plus real command checks, with only the model driver fake.
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "1")
    def git(*argv):
        return subprocess.run(
            ["git", "-c", "user.name=Artifact Test", "-c", "user.email=artifact@example.invalid",
             *argv], cwd=project_root, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "initial fixture")
    base = git("rev-parse", "HEAD")
    check = _check(1)
    project = project.model_copy(update={"checks": [check]})
    spec = task_spec.model_copy(update={
        "acceptance": [AcceptanceCriterion(id="AC-1", statement="unit passes", check_ids=["unit"])],
        "workspace": WorkspaceSpec(mode="worktree", base_commit=base, keep=True),
        "repair_policy": RepairPolicy(check_exit_codes={"unit": [1]}, allow_reviewer_changes=True),
    })
    store = Store(tmp_path / "ledger.sqlite")
    binding = RootBudgetBinding.derive(
        project_id=project.project_id, repo_path=str(project_root), task_id=spec.task_id,
        ledger_path=store.path,
    )
    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=1)
    authorization = AuthorizationRecord(
        authorization_id="AUTH-artifact-io", authorized_at="2026-10-05T00:00:00Z",
        user_text="Offline test allowance for this exact task and root.",
        max_top_level_submissions=4, root_limits=limits,
        binding=AuthorizationBinding(
            mode="m2-live-change", driver="fake", project_id=project.project_id,
            repo_path=str(project_root), base_commit=base, spec_digest=spec.spec_digest(),
            spec_path=str(project_root / "task.json"), root_budget=binding,
        ),
    )
    driver = FakeDriver(project_root, FakeScript(agent_turns=1))
    controller = Controller(
        store, driver, controller_build="artifact-io-test", runners=CheckRunners.offline_default(),
        data_dir=tmp_path / "data", authorization=authorization, root_binding=binding,
        root_limits=limits, preflight=lambda: (True, "offline fixture"),
    )
    if stage == "factory":
        def factory_failure(*_args):
            raise OSError("factory storage unavailable")
        monkeypatch.setattr(controller, "_check_artifact_dir", factory_failure)
    elif stage == "directory":
        obstacle = tmp_path / "not-a-directory"
        obstacle.write_text("keep this file", encoding="utf-8")
        monkeypatch.setattr(controller, "_check_artifact_dir", lambda *_: obstacle)
    else:
        _fail_manifest(monkeypatch, "mkdir" if stage == "manifest_mkdir" else "write_text")

    request = RunRequest(
        task=spec, project=project, project_root=project_root, workspace_root=project_root,
    )
    try:
        outcome = controller.run_task(request)
        assert outcome.task_state is TaskState.BLOCKED, outcome.block_reason
        assert outcome.block_code is RefusalCode.VERIFICATION_FAILED
        assert outcome.receipt is None
        evidence = store.evidence_for(outcome.run_id, kind="verification")
        assert len(evidence) == 1
        row = evidence[0]
        assert row["status"] == EvidenceStatus.ERROR.value
        expected_reason = "output_capture_error" if stage.startswith("manifest") else "no_artifact_dir"
        assert row["exit_reason"] == expected_reason
        assert row["exit_code"] == (1 if stage.startswith("manifest") else None)
        if stage.startswith("manifest"):
            directory = Path(_reference_field(row["detail"], "artifact"))
            assert directory.is_dir()
            assert process_gone(int((directory / "stdout.txt").read_text().strip())) is True

        intents = store.invocations_for(outcome.run_id)
        assert [entry.role for entry in intents] == ["implementer"]
        assert intents[0].state is InvocationStartState.SETTLED
        assert intents[0].outcome is InvocationOutcome.COMPLETED
        root = store.root_budget_for_run(outcome.run_id)
        assert root["used_top_level_submissions"] == 1
        assert root["used_repairs"] == 0
        assert [record.decision for record in store.repair_records_for(outcome.run_id)] == [
            RepairDecision.NOT_A_BUSINESS_FAILURE,
        ]
        assert len(store.attempts_for(outcome.run_id)) == 1

        # Re-submission reads the settled block; it cannot replay a check or a model turn.
        again = controller.run_task(request)
        assert again.run_id == outcome.run_id
        assert again.task_state is TaskState.BLOCKED
        assert len(driver.started) == 1
        assert len(store.evidence_for(outcome.run_id, kind="verification")) == 1
        assert store.root_budget_for_run(outcome.run_id)["used_top_level_submissions"] == 1
    finally:
        controller.close()
        store.close()
