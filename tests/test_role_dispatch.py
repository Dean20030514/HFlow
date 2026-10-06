"""Per-role configuration: both roles really run through the binding their profile names.

The batch's first requirement is that `implementer` and `reviewer` *use* the configured
binding, and that an unusable one is refused. These tests drive the controller with two
distinct role drivers and check which one each role reached - a profile that resolved but was
then ignored by the dispatch would pass every other test in this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.cli import EXIT_OK, main
from hflow.contracts import MachineProfile
from hflow.controller import NOTE_PACKET, Controller, inspect_run
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.prepare import resolve_permissions, resolve_run, role_drivers
from hflow.report import report_json, status_text
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import write_profile, write_project, write_task

WRITE_PLAN = {
    "src/parser.py": "def parse(text):\n    if not text:\n        return None\n    return text\n"
}


@pytest.fixture()
def two_agent_profile() -> MachineProfile:
    """Two roles, two agents, two model selections - both offline so the run needs no model."""
    return MachineProfile(
        profile_id="two-agents",
        role_bindings={"implementer": "worker-a", "reviewer": "worker-b"},
        agents={
            "worker-a": {
                "harness": "dsh",
                "driver": "fake",
                "model_selection": "implementer-model",
                "capability_record": "record-a",
            },
            "worker-b": {
                "harness": "dsh",
                "driver": "fake-offline",
                "model_selection": "reviewer-model",
                "capability_record": "record-b",
            },
        },
    )


class RoleRecordingDriver:
    """Delegates to the offline fake driver and records which binding each role arrived with."""

    driver_id = "role-recorder"

    def __init__(self, binding, *, project_root: Path, seen: list[tuple[str, str, str]]) -> None:
        self.binding = binding
        self.seen = seen
        self.inner = FakeDriver(project_root, FakeScript(write_plan=dict(WRITE_PLAN)))

    def probe(self, binding):  # noqa: ANN001, ANN201
        return self.inner.probe(binding)

    def start(self, request):  # noqa: ANN001, ANN201
        self.seen.append((self.binding.model_selection, request.role, request.invocation_id))
        return self.inner.start(request)

    def cancel(self, invocation_id: str):  # noqa: ANN001, ANN201
        return self.inner.cancel(invocation_id)

    def reconcile(self, invocation_id: str):  # noqa: ANN001, ANN201
        return self.inner.reconcile(invocation_id)


def _resolved(tmp_path: Path, project, task_spec, project_root: Path, profile: MachineProfile):
    data_dir = tmp_path / "data"
    write_profile(data_dir, profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    return resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id=profile.profile_id,
    )


def test_each_role_dispatches_through_its_own_configured_driver(
    tmp_path: Path, project, task_spec, project_root: Path, two_agent_profile
) -> None:
    resolved = _resolved(tmp_path, project, task_spec, project_root, two_agent_profile)
    seen: list[tuple[str, str, str]] = []

    def factory(binding, *, data_dir, launch=None):  # noqa: ANN001, ANN202
        return RoleRecordingDriver(binding, project_root=project_root, seen=seen)

    drivers = role_drivers(resolved, factory=factory)
    assert drivers["implementer"] is not drivers["reviewer"], (
        "two different role bindings must not collapse into one driver"
    )

    store = Store(tmp_path / "hflow.sqlite")
    check_runner = FakeCheckRunner()
    controller = Controller(
        store,
        drivers["implementer"],  # type: ignore[arg-type]
        reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
        controller_build="role-test",
        runners=CheckRunners({"fake": check_runner, "command": check_runner}),
        effective_config=resolved.effective,
    )
    try:
        outcome = controller.run_task(resolved.request())
    finally:
        store.close()

    # The reviewer was dispatched through the reviewer's binding, not the implementer's.
    assert [(model, role) for model, role, _ in seen] == [
        ("implementer-model", "implementer"),
        ("reviewer-model", "reviewer"),
    ], seen
    assert outcome.task_state.value == "ACCEPTED", (outcome.task_state, outcome.block_reason)


def test_the_effective_configuration_is_recorded_and_readable(
    tmp_path: Path, project, task_spec, project_root: Path, two_agent_profile
) -> None:
    resolved = _resolved(tmp_path, project, task_spec, project_root, two_agent_profile)
    seen: list[tuple[str, str, str]] = []

    def factory(binding, *, data_dir, launch=None):  # noqa: ANN001, ANN202
        return RoleRecordingDriver(binding, project_root=project_root, seen=seen)

    drivers = role_drivers(resolved, factory=factory)
    store = Store(tmp_path / "hflow.sqlite")
    check_runner = FakeCheckRunner()
    controller = Controller(
        store,
        drivers["implementer"],  # type: ignore[arg-type]
        reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
        controller_build="role-test",
        runners=CheckRunners({"fake": check_runner, "command": check_runner}),
        effective_config=resolved.effective,
    )
    try:
        outcome = controller.run_task(resolved.request())
        inspection = inspect_run(store, outcome.run_id)
    finally:
        store.close()

    recorded = inspection.effective_config
    assert recorded is not None
    assert recorded.profile_id == "two-agents"
    assert recorded.digest() == resolved.effective.digest()
    assert recorded.role("reviewer").model_selection == "reviewer-model"  # type: ignore[union-attr]

    rendered = status_text(inspection)
    assert "configuration machine_profile profile=two-agents" in rendered
    assert f"digest={resolved.effective.digest()}" in rendered
    assert "agent=worker-b driver=fake-offline -> fake" in rendered
    assert report_json(inspection)["effective_config"]["profile_id"] == "two-agents"


def test_a_run_without_a_recorded_configuration_says_not_recorded(
    store: Store, controller: Controller, run_request
) -> None:
    """History stays readable: an older run is not back-filled from the current configuration."""
    outcome = controller.run_task(run_request)
    inspection = inspect_run(store, outcome.run_id, project_root=run_request.project_root)

    assert inspection.effective_config is None
    assert "not recorded" in status_text(inspection)
    assert report_json(inspection)["effective_config"] is None


def test_the_first_recorded_configuration_wins(
    store: Store, controller: Controller, run_request, two_agent_profile, project
) -> None:
    """A later invocation cannot rewrite which configuration a run ran under."""
    from hflow.contracts import EffectiveConfig, RoleConfig

    outcome = controller.run_task(run_request)
    first = EffectiveConfig(
        source="machine_profile",
        profile_id="first",
        roles=[RoleConfig(role="implementer", agent="a", harness="dsh", driver="fake", driver_id="fake")],
    )
    second = first.model_copy(update={"profile_id": "second"})

    store.record_effective_config(outcome.run_id, first)
    from hflow.store import StoreError

    with pytest.raises(StoreError, match="conflicting immutable effective_config"):
        store.record_effective_config(outcome.run_id, second)

    recorded = store.effective_config_for(outcome.run_id)
    assert recorded is not None and recorded.profile_id == "first"
    notes = [n for n in store.notes_for(outcome.run_id) if n.startswith("effective_config:")]
    assert len(notes) == 1, notes


# --------------------------------------------------------------------------
# the whole daily loop, through the CLI
# --------------------------------------------------------------------------


def test_prepare_then_run_then_report_reuses_one_configuration(
    tmp_path: Path, project, task_spec, project_root: Path, two_agent_profile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The completion standard: reuse a config, preview with `prepare`, run, read the report."""
    data_dir = tmp_path / "data"
    write_profile(data_dir, two_agent_profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(WRITE_PLAN), encoding="utf-8")

    common = [
        "--task", str(task_file),
        "--project", str(project_file),
        "--project-root", str(project_root),
        "--profile", "two-agents",
        "--json",
        "--data-dir", str(data_dir),
    ]
    assert main(["prepare", *common]) == EXIT_OK
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["model_calls_made"] == 0
    assert prepared["effective_config"]["profile_id"] == "two-agents"
    assert prepared["authorization"]["required"] is False

    assert main(["run", *common, "--fake-write-plan", str(plan_file)]) == EXIT_OK
    ran = json.loads(capsys.readouterr().out)
    assert ran["task_state"] == "ACCEPTED"
    assert ran["implementer_invocations"] == 1
    assert ran["reviewer_invocations"] == 1

    run_id = ran["run_id"]
    store = Store(data_dir / "hflow.sqlite")
    try:
        notes = store.notes_for(run_id)
    finally:
        store.close()
    # The packet the implementer was actually handed is the one `prepare` previewed - same
    # bytes, same digest. A preview of a different input would be worse than no preview.
    assert (
        f"{NOTE_PACKET}: role=implementer bytes={prepared['packet_preview']['implementer']['bytes']} "
        f"digest={prepared['packet_preview']['implementer']['digest']}" in notes
    ), notes

    assert main(["status", run_id, "--data-dir", str(data_dir)]) == EXIT_OK
    status_out = capsys.readouterr().out
    assert "profile=two-agents" in status_out
    assert f"digest={prepared['effective_config_digest']}" in status_out

    assert main(["report", run_id, "--data-dir", str(data_dir)]) == EXIT_OK
    report_out = capsys.readouterr().out
    assert "configuration machine_profile profile=two-agents" in report_out
    # The config `prepare` reported is the config the run actually used.
    assert prepared["effective_config_digest"] in report_out

    assert main(["report", run_id, "--json", "--data-dir", str(data_dir)]) == EXIT_OK
    reported = json.loads(capsys.readouterr().out)
    assert reported["effective_config_digest"] == prepared["effective_config_digest"]


def test_write_permission_is_one_value_shared_by_preview_and_dispatch(
    task_spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permission an approval covers is resolved by the same function the dispatch uses."""
    in_place = task_spec.model_copy(
        update={"workspace": task_spec.workspace.model_copy(update={"mode": "in_place"})}
    )
    worktree = task_spec.model_copy(
        update={"workspace": task_spec.workspace.model_copy(update={"mode": "worktree"})}
    )
    on = {"HFLOW_ALLOW_WRITES": "yes"}

    assert resolve_permissions(in_place, on) == (False, False), "no worktree, no write"
    assert resolve_permissions(worktree, on) == (True, False), "a reviewer never writes"
    assert resolve_permissions(worktree, {}) == (False, False), "no opt-in, no write"
    # The controller asks the same function, so a preview cannot promise a permission the
    # dispatch does not grant.
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    assert resolve_permissions(worktree) == (True, False)
