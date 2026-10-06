"""Durable admission documents are atomic, complete and immutable."""

from pathlib import Path

import pytest

from hflow.contracts import EffectiveConfig, RunAdmissionBinding, TaskSpec, WorkspaceProvenance, digest_of
from hflow.store import Store, StoreError, StoredRecordUnreadable


def _binding(tmp_path: Path, config: EffectiveConfig | None = None) -> RunAdmissionBinding:
    return RunAdmissionBinding(
        project_root=str(tmp_path / "project"), git_common_dir=str(tmp_path / "project" / ".git"),
        worktree_path=str(tmp_path / "worktree"), base_commit="a" * 40,
        project_contract_digest="sha256:" + "b" * 64,
        effective_config_digest=config.digest() if config else None,
        driver_ids=["fake"], deadline_seconds=120,
    )


def _create(store: Store, task_spec: TaskSpec, binding: RunAdmissionBinding, **kwargs):
    return store.create_run(
        run_id="R-binding", project_id="P-binding", spec=task_spec,
        spec_digest=digest_of(task_spec.model_dump(mode="json")), controller_build="test", checks_digest="checks",
        turn_limit=4, repair_limit=0, admission_binding=binding, **kwargs,
    )


def test_full_config_and_admission_commit_together(store, task_spec, tmp_path):
    config = EffectiveConfig(source="command_line", profile_id="x" * 12000)
    binding = _binding(tmp_path, config)
    _create(store, task_spec, binding, effective_config=config)
    assert store.admission_binding_for("R-binding") == binding
    assert store.effective_config_for("R-binding") == config
    assert any(len(note) > 8000 for note in store.notes_for("R-binding"))
    with pytest.raises(StoreError, match="conflicting immutable"):
        store.record_effective_config("R-binding", config.model_copy(update={"profile_id": "other"}))
    assert store.effective_config_for("R-binding") == config


def test_note_failure_rolls_back_the_new_run(store, task_spec, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise StoreError("injected note failure")

    monkeypatch.setattr(store, "_record_note_locked", fail)
    with pytest.raises(StoreError, match="injected"):
        _create(store, task_spec, _binding(tmp_path))
    assert store.list_runs() == []


def test_lost_insert_race_does_not_replace_or_backfill_notes(store, task_spec, tmp_path):
    original = _binding(tmp_path)
    _create(store, task_spec, original)
    before = store.notes_for("R-binding")
    row = _create(store, task_spec, original.model_copy(update={"deadline_seconds": 121}))
    assert row["run_id"] == "R-binding"
    assert store.notes_for("R-binding") == before


@pytest.mark.parametrize("note", ["admission_binding: {bad", 'admission_binding: {}'])
def test_corrupted_binding_is_unreadable_without_writes(store, task_spec, tmp_path, note):
    _create(store, task_spec, _binding(tmp_path))
    store.record_note("R-binding", note)
    before = store.conn.total_changes
    with pytest.raises(StoredRecordUnreadable, match="admission_binding"):
        store.admission_binding_for("R-binding")
    assert store.conn.total_changes == before


def test_conflicting_binding_documents_are_rejected(store, task_spec, tmp_path):
    from hflow.contracts import canonical_json

    binding = _binding(tmp_path)
    _create(store, task_spec, binding)
    other = binding.model_copy(update={"deadline_seconds": 121})
    store.record_note("R-binding", "admission_binding: " + canonical_json(other.model_dump()), limit=10000)
    with pytest.raises(StoredRecordUnreadable, match="conflicting admission_binding"):
        store.admission_binding_for("R-binding")


def test_worktree_path_and_provenance_are_one_immutable_fact(store, task_spec, tmp_path):
    binding = _binding(tmp_path)
    _create(store, task_spec, binding)
    provenance = WorkspaceProvenance(
        project_root=binding.project_root, git_common_dir=binding.git_common_dir,
        worktree_path=binding.worktree_path,
    )
    with pytest.raises(StoreError, match="differs from admission"):
        store.record_worktree("R-binding", Path(binding.worktree_path))
    assert not store.get_run("R-binding")["worktree_path"]
    store.record_worktree("R-binding", Path(binding.worktree_path), provenance=provenance)
    assert store.workspace_provenance_for("R-binding") == provenance
    with pytest.raises(StoreError):
        store.record_worktree("R-binding", tmp_path / "other", provenance=provenance)
    assert store.get_run("R-binding")["worktree_path"] == binding.worktree_path
