"""The CLI reports drift of an admitted, never-dispatched run before new-run gates.

The live configuration here only resolves the checked-in launcher stand-ins. No real driver
is constructed, no client is probed, and no model or submission is started.
"""

from __future__ import annotations

import json

import pytest

from hflow.authorization import AuthorizationRecord, current_binding
from hflow.cli import EXIT_IN_PROGRESS, EXIT_REFUSED, _NotOwnedDriver, main
from hflow.contracts import TaskState
from hflow.controller import Controller
from hflow.paths import database_path
from hflow.prepare import resolve_run
from hflow.store import Store

from .conftest import write_profile, write_project, write_task
from .test_m2_slice import _git, _project, _task, sample_repo  # noqa: F401 - registers the shared fixture


@pytest.fixture()
def admitted_cli(tmp_path, sample_repo, live_profile, acpx_client, monkeypatch):  # noqa: F811 - pytest fixture
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    client = tmp_path / "client.py"
    client.write_bytes(acpx_client.read_bytes())
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(client))
    data_dir = tmp_path / "data"
    profile_file = write_profile(data_dir, live_profile)
    project = _project(sample_repo)
    task = _task(sample_repo, _git(sample_repo, "rev-parse", "HEAD").strip())
    project_file = tmp_path / "project.json"
    task_file = write_task(tmp_path / "task.json", task)
    write_project(project_file, project)
    resolved = resolve_run(
        task_path=task_file, project_root=sample_repo, data_dir=data_dir,
        project_path=project_file, profile_id=live_profile.profile_id,
    )
    expected = current_binding(
        mode="m2-live-change", driver=resolved.effective.role("implementer").driver,
        project=project, request=resolved.request(), spec_path=task_file,
        effective=resolved.effective,
    )
    approval = AuthorizationRecord(
        authorization_id="AUTH-cli-original", user_text="I approve this exact bounded run.",
        authorized_at="2026-10-05T00:00:00Z", max_top_level_submissions=4,
        binding=expected,
    )
    approval_file = tmp_path / "approval.json"
    approval_file.write_text(approval.model_dump_json(), encoding="utf-8")
    store = Store(database_path(data_dir))
    drivers = {role: _NotOwnedDriver(resolved.effective.role(role).driver_id)
               for role in ("implementer", "reviewer")}
    controller = Controller(
        store, drivers["implementer"], reviewer_driver=drivers["reviewer"],
        controller_build="cli-binding-test", production=False,
        effective_config=resolved.effective,
    )
    run_id = "R-cli-binding"
    store.create_run(
        run_id=run_id, project_id=project.project_id, spec=task,
        spec_digest=task.spec_digest(), controller_build="cli-binding-test",
        checks_digest=project.checks_digest(), turn_limit=task.budget.max_agent_turns,
        repair_limit=task.budget.max_repair_cycles,
        admission_binding=controller._admission_binding(run_id, resolved.request()),
        effective_config=resolved.effective,
    )
    store.register_authorization(approval.as_store_record())
    argv = [
        "run", "--task", str(task_file), "--project", str(project_file),
        "--project-root", str(sample_repo), "--profile", live_profile.profile_id,
        "--authorization-file", str(approval_file), "--data-dir", str(data_dir), "--json",
    ]

    def no_driver(*args, **kwargs):
        pytest.fail("this refusal must happen before constructing a real driver")

    monkeypatch.setattr("hflow.prepare.role_drivers", no_driver)
    yield {
        "store": store, "run_id": run_id, "argv": argv, "project": project,
        "project_file": project_file, "profile_file": profile_file, "client": client,
        "approval": approval, "approval_file": approval_file, "data_dir": data_dir,
    }
    store.close()


def _facts(env):
    store, run_id = env["store"], env["run_id"]
    return (dict(store.get_run(run_id)), store.notes_for(run_id),
            [dict(row) for row in store.attempts_for(run_id)], store.invocations_for(run_id),
            store.authorization_state("AUTH-cli-original"),
            store.conn.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0],
            sorted(str(path) for path in env["data_dir"].rglob("*.lock")))


@pytest.mark.parametrize("drift", ["missing_check", "profile", "launch", "binding_missing", "binding_corrupt"])
@pytest.mark.parametrize("state", [TaskState.DRAFT, TaskState.READY])
def test_cli_drift_keeps_admitted_run_and_allowance_unchanged(admitted_cli, drift, state, capsys):
    env = admitted_cli
    store, run_id = env["store"], env["run_id"]
    if state is TaskState.READY:
        store.set_task_state(run_id, [TaskState.DRAFT], state)
    if drift == "missing_check":
        write_project(env["project_file"], env["project"].model_copy(update={"checks": []}))
    elif drift == "profile":
        profile = json.loads(env["profile_file"].read_text(encoding="utf-8"))
        profile["agents"]["dsh-implementer"]["capability_record"] = "changed-record"
        env["profile_file"].write_text(json.dumps(profile), encoding="utf-8")
    elif drift == "launch":
        with env["client"].open("ab") as stream:
            stream.write(b"\n# changed launcher bytes\n")
    else:
        with store.transaction() as conn:
            if drift == "binding_missing":
                conn.execute("DELETE FROM run_notes WHERE run_id=? AND note LIKE 'admission_binding: %'", (run_id,))
            else:
                conn.execute("UPDATE run_notes SET note='admission_binding: {bad' WHERE run_id=? AND note LIKE 'admission_binding: %'", (run_id,))
    before = _facts(env)

    assert main(env["argv"]) == EXIT_IN_PROGRESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == run_id and payload["task_state"] == state.value
    assert any("continuation refused" in note for note in payload["notes"])
    assert any("cancel this run" in note and "new revision" in note for note in payload["notes"])
    assert payload["driver_invocations_total"] == 0
    assert _facts(env) == before


def test_matching_admission_still_requires_matching_live_approval(admitted_cli, capsys):
    env = admitted_cli
    approval = env["approval"].model_copy(update={
        "binding": env["approval"].binding.model_copy(update={"effective_config_digest": "sha256:" + "f" * 64}),
    })
    env["approval_file"].write_text(approval.model_dump_json(), encoding="utf-8")
    before = _facts(env)

    assert main(env["argv"]) == EXIT_REFUSED
    assert "authorization covers a different configuration" in capsys.readouterr().err
    assert _facts(env) == before


def test_new_refused_task_does_not_create_a_ledger(tmp_path, project_root, project, task_spec, capsys):
    data_dir = tmp_path / "absent-data"
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "project.json", project.model_copy(update={"checks": []}))

    assert main([
        "run", "--task", str(task_file), "--project", str(project_file),
        "--project-root", str(project_root), "--data-dir", str(data_dir), "--json",
    ]) == EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["refused"] is True
    assert not database_path(data_dir).exists()
    assert not data_dir.exists()
