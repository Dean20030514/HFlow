"""`hflow prepare`: a zero-model preview that must not become an authorization.

The three properties that matter, and each is asserted rather than promised: it calls no
model, it creates no state, and the artifact it prints cannot authorize anything. The fourth
is that its numbers are the ones `run` will use - a preview of a different configuration than
the one that executes would be worse than no preview.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hflow.authorization import (
    AuthorizationBinding,
    AuthorizationRecord,
    current_binding,
    load_authorization,
    verify_authorization,
)
from hflow.cli import EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import RefusedError, WorkspaceSpec
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.prepare import build_prepare_report, resolve_run, role_drivers

from .conftest import write_profile, write_project, write_task


@pytest.fixture()
def prepared(
    tmp_path: Path,
    live_project,
    worktree_task,
    project_root: Path,
    live_profile,
    acpx_client,
    monkeypatch: pytest.MonkeyPatch,
):
    """A resolved live run and its preview, in the shape a real delivery actually has.

    Worktree plus the write opt-in, because a task that declares write paths cannot run in
    place: a preview exercised against an unrunnable configuration would prove nothing about
    the one that runs.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)
    resolved = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id="dsh-local",
    )
    return resolved, build_prepare_report(resolved)


# --------------------------------------------------------------------------
# what the preview says
# --------------------------------------------------------------------------


def test_prepare_reports_the_effective_configuration_per_role(prepared) -> None:
    resolved, report = prepared
    effective = report.effective_config

    assert effective.source == "machine_profile"
    assert effective.profile_id == "dsh-local"
    assert effective.profile_digest.startswith("sha256:")
    assert effective.driver_ids() == ["acpx-dsh-acp", "acpx-dsh-acp"]
    implementer, reviewer = effective.role("implementer"), effective.role("reviewer")
    assert implementer is not None and reviewer is not None
    assert implementer.agent == "dsh-implementer"
    assert reviewer.agent == "dsh-reviewer"
    # Two roles, two model selections: this is the difference the batch exists to make real.
    assert implementer.model_selection == "implementer-model"
    assert reviewer.model_selection == "reviewer-model"
    assert effective.digest() == resolved.effective.digest()
    assert effective.implementer_writes is True, "the write opt-in is part of the configuration"


def test_the_effective_configuration_carries_the_resolved_launch(prepared) -> None:
    """The programs, not just the driver name: this is what an approval has to cover."""
    resolved, report = prepared
    launch = report.effective_config.role("implementer").launch  # type: ignore[union-attr]

    assert launch is not None
    assert launch.resolvable is True
    assert launch.driver_id == "acpx-dsh-acp"
    assert launch.client_entry.endswith("fake_acpx_client.py")
    assert launch.client_argv_prefix, "the interpreter that starts the client is recorded"
    assert launch.agent_argv[0] != "", "the launcher is recorded"
    assert launch.profile, "the DSH profile the launcher starts with is recorded"
    assert launch.python and launch.dsh_executable and launch.node
    # The launch is inside the digest, so approving this configuration approves this launch.
    without_launch = resolved.effective.model_copy(
        update={
            "roles": [
                entry.model_copy(update={"launch": None}) for entry in resolved.effective.roles
            ]
        }
    )
    assert without_launch.digest() != resolved.effective.digest()


def test_prepare_reports_scope_checks_and_budget(prepared) -> None:
    _, report = prepared

    assert report.write_allow == ["src/parser.py", "tests/test_parser.py"]
    assert report.write_deny == []  # the task adds none; the project's list is admission's
    assert [planned.check.id for planned in report.checks] == ["unit", "docs-check"]
    assert report.checks[0].required_by == ["AC-1", "AC-2"]
    assert report.checks[0].check.kind == "command"
    # A live run is verified by approved command checks, and admission would refuse a fake one.
    assert report.admission.ok is True
    # Implementer + reviewer, two separate dispatches with two separate reservations.
    assert report.budget.implementer_turns == 1
    assert report.budget.reviewer_turns == 1
    assert report.budget.required_turns == 2
    assert report.budget.within_budget is True
    assert report.roles == ["implementer", "reviewer"]


def test_prepare_previews_the_implementer_packet_from_real_facts(prepared) -> None:
    resolved, report = prepared
    preview = report.packet_preview["implementer"]

    assert preview["rendered"] is True
    assert preview["bytes"] > 0
    assert preview["digest"].startswith("sha256:")
    assert resolved.spec.task_id in preview["preview"]
    assert resolved.spec.acceptance[0].statement in preview["preview"]


def test_the_reviewer_packet_is_not_invented_here(prepared) -> None:
    """Its input embeds the frozen candidate, which does not exist before the implementer."""
    _, report = prepared
    reviewer = report.packet_preview["reviewer"]

    assert reviewer["rendered"] is False
    assert "candidate" in reviewer["reason"]
    assert "rendered at review time" in reviewer["reason"]


def test_prepare_reports_admission_problems_but_still_reports(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that admission refuses is still fully previewed: that is the useful answer."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    narrowed = live_project.model_copy(update={"limits": live_project.limits.model_copy(
        update={"max_agent_turns": 1}
    )})
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", narrowed)

    report = build_prepare_report(
        resolve_run(
            task_path=task_file,
            project_root=project_root,
            data_dir=data_dir,
            project_path=project_file,
            profile_id="dsh-local",
        )
    )
    assert report.admission.ok is False
    assert any(
        issue.location == "budget.max_agent_turns" for issue in report.admission.issues
    )
    # The preview still says what the loop needs, which is what makes the refusal actionable.
    assert report.budget.required_turns == 2
    assert report.effective_config.profile_id == "dsh-local"


# --------------------------------------------------------------------------
# prepare is not an authorizer, and not a side effect
# --------------------------------------------------------------------------


def test_prepare_prints_a_pending_binding_and_creates_no_authorization(prepared) -> None:
    _, report = prepared
    pending = report.authorization

    assert pending.required is True
    assert pending.creates_authorization is False
    assert pending.max_top_level_submissions_required == 2
    assert pending.binding_digest.startswith("sha256:")
    # What an approval would have to cover, and nothing that makes it one.
    assert pending.binding["spec_digest"] == report.spec_digest
    assert pending.binding["profile_id"] == "dsh-local"
    assert pending.binding["effective_config_digest"] == report.effective_config.digest()
    assert "user_text" not in pending.binding
    assert "provided_by" not in pending.binding
    assert "provided_by" not in report.model_dump(mode="json").get("authorization", {})
    assert report.model_calls_made == 0


def test_the_prepare_payload_cannot_be_loaded_as_an_authorization(
    tmp_path: Path, prepared
) -> None:
    """The whole payload, and its authorization section, both fail to authorize anything."""
    _, report = prepared
    whole = tmp_path / "prepare.json"
    whole.write_text(json.dumps(report.model_dump(mode="json")), encoding="utf-8")
    section = tmp_path / "section.json"
    section.write_text(
        json.dumps(report.authorization.model_dump(mode="json")), encoding="utf-8"
    )

    for path in (whole, section):
        with pytest.raises(Exception):  # noqa: B017 - pydantic rejects it, and that is the point
            load_authorization(path)


def test_prepare_creates_no_run_state(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_calls_made"] == 0
    assert payload["driver_mode"] == "live"
    # No database, no run row, no workspace: a preview that wrote state would not be a preview.
    assert not (data_dir / "hflow.sqlite").exists()
    assert list(data_dir.rglob("*.sqlite")) == []
    assert not (project_root.parent / f"{project_root.name}.hflow-worktrees").exists()


def test_prepare_exits_nonzero_when_admission_would_refuse(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    task = worktree_task.model_copy(update={"dependencies": ["T-000"]})
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["admission"]["ok"] is False
    assert "dependencies" in json.dumps(payload["admission"]["issues"])


def test_prepare_refuses_a_glob_write_allow_entry(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A glob in ``write_allow`` previews as refused, the way the run would refuse it."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    task = worktree_task.model_copy(
        update={"scope": worktree_task.scope.model_copy(update={"write_allow": ["src/**"]})}
    )
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["admission"]["ok"] is False
    assert any(
        issue["code"] == "scope_violation" and "glob" in issue["detail"]
        for issue in payload["admission"]["issues"]
    ), payload["admission"]["issues"]


def test_prepare_refuses_an_unknown_profile(
    tmp_path: Path, live_project, task_spec, capsys: pytest.CaptureFixture[str]
) -> None:
    """A profile that cannot be resolved refuses the preview instead of previewing a default."""
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(tmp_path),
            "--profile", "nope",
            "--json",
            "--data-dir", str(tmp_path / "data"),
        ]
    )
    assert code == EXIT_REFUSED
    assert "profile 'nope' not found" in capsys.readouterr().err
    assert not (tmp_path / "data" / "hflow.sqlite").exists()


def test_prepare_refuses_a_profile_whose_model_selection_could_not_be_a_fixed_flag(
    tmp_path: Path, live_project, task_spec, capsys: pytest.CaptureFixture[str]
) -> None:
    """A model value that is not a token or a provider/model pair stops the preview."""
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)
    path = tmp_path / "data" / "profiles" / "bad-model.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "profile_id": "bad-model",
                "role_bindings": {"implementer": "a", "reviewer": "a"},
                "agents": {
                    "a": {
                        "harness": "dsh",
                        "driver": "acpx-dsh",
                        "model_selection": "pro; rm -rf .",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(tmp_path),
            "--profile", "bad-model",
            "--json",
            "--data-dir", str(tmp_path / "data"),
        ]
    )
    assert code == EXIT_REFUSED
    assert "model_selection" in capsys.readouterr().err
    assert not (tmp_path / "data" / "hflow.sqlite").exists()


def test_prepare_and_run_resolve_one_configuration(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding prepare shows is the binding run verifies against, field for field."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    def resolve(**overrides):
        return resolve_run(
            task_path=task_file,
            project_root=project_root,
            data_dir=data_dir,
            project_path=project_file,
            profile_id="dsh-local",
            **overrides,
        )

    shown = build_prepare_report(resolve())
    # `run` computes its expected binding from its own resolution of the same inputs.
    expected = current_binding(
        mode="m2-live-change",
        driver=resolve().effective.role("implementer").driver,
        project=resolve().project,
        request=resolve().request(),
        spec_path=task_file,
        effective=resolve().effective,
    )
    assert shown.authorization.binding_digest == expected.digest()
    assert shown.authorization.binding == expected.model_dump(mode="json")

    # And the preview tracks the overrides `run` applies, so a previewed in-place run is the
    # one that would execute.
    through_override = build_prepare_report(resolve(workspace_mode="in_place"))
    assert through_override.workspace_mode == "in_place"
    assert through_override.execution_root_is_final is True
    assert through_override.spec_digest != shown.spec_digest
    assert through_override.effective_config.implementer_writes is False


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, test-local git commands
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Prepare Test",
            "GIT_AUTHOR_EMAIL": "prepare@example.invalid",
            "GIT_COMMITTER_NAME": "Prepare Test",
            "GIT_COMMITTER_EMAIL": "prepare@example.invalid",
        },
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def test_the_binding_names_the_commit_a_branch_base_resolves_to(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``base_commit='main'`` is resolved once, and the approval names the commit, not the name.

    The task text keeps saying 'main' - its digest is the task's own - but the binding carries the
    SHA 'main' pointed at when it was resolved. A branch that moves between ``prepare`` and ``run``
    is a different base, so the approval written for the old one stops applying.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    _git(project_root, "init", "-q", "-b", "main")
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", "base")
    first = _git(project_root, "rev-parse", "main").strip()
    task = worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    def resolve():
        return resolve_run(
            task_path=task_file,
            project_root=project_root,
            data_dir=data_dir,
            project_path=project_file,
            profile_id="dsh-local",
        )

    def expected_for(resolved):
        return current_binding(
            mode="m2-live-change",
            driver=resolved.effective.role("implementer").driver,
            project=resolved.project,
            request=resolved.request(),
            spec_path=task_file,
            effective=resolved.effective,
        )

    resolved = resolve()
    assert resolved.spec.workspace.base_commit == "main", "the task text is not rewritten"
    report = build_prepare_report(resolved)
    assert report.authorization.binding["base_commit"] == first

    approval = AuthorizationRecord(
        authorization_id="AUTH-base-1",
        user_text="I approve one run of this task from the commit main points at now.",
        authorized_at="2026-10-01T00:00:00Z",
        max_top_level_submissions=2,
        binding=AuthorizationBinding.model_validate(report.authorization.binding),
    )
    verify_authorization(approval, expected=expected_for(resolve()))

    (project_root / "notes.txt").write_text("the user keeps working\n", encoding="utf-8")
    _git(project_root, "add", "notes.txt")
    _git(project_root, "commit", "-q", "-m", "main moves on")
    second = _git(project_root, "rev-parse", "main").strip()
    moved = resolve()
    assert moved.spec.spec_digest() == resolved.spec.spec_digest(), "same task text, same digest"
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(approval, expected=expected_for(moved))
    assert "base_commit" in excinfo.value.message
    assert first in excinfo.value.message and second in excinfo.value.message


def test_an_offline_profile_needs_no_authorization_and_resolves_the_fake_driver(
    tmp_path: Path, project, task_spec, project_root: Path, profile
) -> None:
    data_dir = tmp_path / "data"
    write_profile(data_dir, profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)

    report = build_prepare_report(
        resolve_run(
            task_path=task_file,
            project_root=project_root,
            data_dir=data_dir,
            project_path=project_file,
            profile_id="dsh-local",
        )
    )
    assert report.driver_mode == "offline"
    assert report.authorization.required is False
    assert report.authorization.creates_authorization is False
    assert report.effective_config.driver_ids() == ["fake", "fake"]
    assert report.model_calls_made == 0


def test_role_drivers_builds_one_instance_per_distinct_binding(
    tmp_path: Path, project, task_spec, project_root: Path, live_profile, profile, acpx_client
) -> None:
    """Same binding, one driver; different bindings, different drivers - and no guessing."""
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    seen: list[tuple[str, str, bool]] = []

    def factory(binding, *, data_dir, launch=None):  # noqa: ANN001, ANN202
        seen.append((binding.driver, binding.model_selection, launch is not None))
        return FakeDriver(project_root, FakeScript())

    live = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id="dsh-local",
    )
    drivers = role_drivers(live, factory=factory)
    assert drivers["implementer"] is not drivers["reviewer"]
    assert sorted(seen) == [
        ("acpx-dsh", "implementer-model", True),
        ("acpx-dsh", "reviewer-model", True),
    ], "a real driver is built from the launch the configuration resolved, not a fresh one"

    write_profile(data_dir, profile)  # both roles on one agent
    shared = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id="dsh-local",
    )
    seen.clear()
    drivers = role_drivers(shared, factory=factory)
    assert drivers["implementer"] is drivers["reviewer"]
    assert seen == [("fake-offline", "native_profile", False)], "nothing is launched offline"


def test_run_refuses_a_real_profile_without_an_authorization_before_reading_the_task(
    tmp_path: Path, live_profile, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing approval is never reported as a malformed task, or as a fallback."""
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)

    code = main(
        [
            "run",
            "--task", str(tmp_path / "absent-task.json"),
            "--project-root", str(tmp_path),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    assert code == EXIT_REFUSED
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["reason"] == "live_authorization_missing"
    assert "absent-task.json" not in json.dumps(payload)
    assert not (data_dir / "hflow.sqlite").exists()


def test_a_legacy_authorization_cannot_authorize_a_resolved_configuration(
    tmp_path: Path, project, task_spec, project_root: Path, live_profile, acpx_client
) -> None:
    """An artifact from before config binding loads, but does not cover a config-bound run."""
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    resolved = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id="dsh-local",
    )

    legacy = AuthorizationRecord(
        authorization_id="AUTH-legacy-1",
        user_text="I approve running this task on the configured driver.",
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=2,
        binding=current_binding(
            mode="m2-live-change",
            driver="acpx-dsh",
            project=project,
            request=resolved.request(),
            spec_path=task_file,
        ),
    )
    assert legacy.binding.effective_config_digest == ""

    expected = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=resolved.request(),
        spec_path=task_file,
        effective=resolved.effective,
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(legacy, expected=expected)
    assert "no effective-configuration binding" in excinfo.value.message

    # Switching the profile after approval is refused for the same reason, with both sides named.
    other_profile = live_profile.model_copy(
        update={
            "agents": {
                **live_profile.agents,
                "dsh-reviewer": live_profile.agents["dsh-reviewer"].model_copy(
                    update={"model_selection": "another-model"}
                ),
            }
        }
    )
    write_profile(data_dir, other_profile.model_copy(update={"profile_id": "dsh-other"}))
    switched = resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id="dsh-other",
    )
    bound = AuthorizationRecord(
        authorization_id="AUTH-bound-1",
        user_text="I approve running this task on the dsh-local profile.",
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=2,
        binding=expected,
    )
    switched_binding = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=switched.request(),
        spec_path=task_file,
        effective=switched.effective,
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(bound, expected=switched_binding)
    assert "different configuration" in excinfo.value.message
    assert "dsh-local" in excinfo.value.message and "dsh-other" in excinfo.value.message


# --------------------------------------------------------------------------
# the launch, not the name: which programs an approval actually covers
# --------------------------------------------------------------------------


def _resolve_live(tmp_path: Path, project, task, project_root: Path, profile, **overrides):
    data_dir = tmp_path / "data"
    write_profile(data_dir, profile)
    task_file = write_task(tmp_path / "task.json", task)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    return resolve_run(
        task_path=task_file,
        project_root=project_root,
        data_dir=data_dir,
        project_path=project_file,
        profile_id=profile.profile_id,
        **overrides,
    )


def test_changing_the_node_interpreter_changes_the_bound_configuration(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile,
    acpx_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repro: only ``HFLOW_ACPX_NODE`` differs, and the approval digest must differ too.

    The logical binding is identical either way - same profile, same agent, same driver name.
    What changes is which program would run, which is exactly what an approval has to cover.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    first = _resolve_live(
        tmp_path, live_project, worktree_task, project_root, live_profile
    )
    before = first.effective.role("implementer").launch  # type: ignore[union-attr]
    assert before is not None and before.resolvable

    monkeypatch.setenv("HFLOW_ACPX_NODE", str(tmp_path / "another-node.exe"))
    second = _resolve_live(
        tmp_path, live_project, worktree_task, project_root, live_profile
    )
    after = second.effective.role("implementer").launch  # type: ignore[union-attr]
    assert after is not None
    assert after.node == str(tmp_path / "another-node.exe")
    assert after != before

    # The stand-in client is a ``.py`` file, so *this* build starts it with the Python
    # interpreter and the node path is recorded without being used. It is recorded anyway -
    # over-binding the approval is the safe direction - and for the real ``.js`` client the
    # prefix is the node path, which is the case the report was filed against.
    assert after.client_argv_prefix == before.client_argv_prefix
    assert Path(acpx_client).suffix == ".py"
    assert after.client_argv_prefix[0] == after.python

    # Same logical binding...
    assert second.effective.role("implementer").driver_id == "acpx-dsh-acp"  # type: ignore[union-attr]
    assert second.effective.profile_digest == first.effective.profile_digest
    # ...different configuration, so a different approval is required.
    assert second.effective.digest() != first.effective.digest()

    def binding(resolved):
        return current_binding(
            mode="m2-live-change",
            driver="acpx-dsh",
            project=resolved.project,
            request=resolved.request(),
            spec_path=resolved.spec_path,
            effective=resolved.effective,
        )

    assert binding(second).digest() != binding(first).digest()
    approved_for_first = AuthorizationRecord(
        authorization_id="AUTH-node-1",
        user_text="I approve this task on this machine configuration.",
        authorized_at="2026-09-20T00:00:00Z",
        max_top_level_submissions=2,
        binding=binding(first),
    )
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(approved_for_first, expected=binding(second))
    assert "different configuration" in excinfo.value.message


def test_the_driver_consumes_the_resolved_launch_instead_of_re_resolving_it(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution happens once. After the approval, nothing may pick a different program."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    resolved = _resolve_live(
        tmp_path, live_project, worktree_task, project_root, live_profile
    )
    launch = resolved.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None

    from hflow.drivers.acpx_dsh import AcpxDshDriver

    instance = AcpxDshDriver(data_dir=resolved.data_dir, launch=launch)
    assert instance.acpx_cli == Path(launch.client_entry)
    assert instance.node_executable == launch.node
    assert instance.python_executable == launch.python
    assert instance.dsh_executable == launch.dsh_executable
    assert instance.profile == launch.profile
    assert instance._agent_argv() == launch.agent_argv
    assert instance._client_prefix() == launch.client_argv_prefix

    # The environment changes *after* the launch was resolved; the driver keeps the resolved
    # one, so what was approved is still what runs.
    monkeypatch.setenv("HFLOW_ACPX_NODE", "some-other-node")
    monkeypatch.setenv("HFLOW_ACPX_CLI", "some-other-client")
    assert instance.node_executable == launch.node
    assert instance.acpx_cli == Path(launch.client_entry)


def test_an_unresolvable_launch_is_reported_rather_than_hidden(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No client on this machine: the preview must still print, and must not say "ready"."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(tmp_path / "not-installed.js"))
    resolved = _resolve_live(
        tmp_path, live_project, worktree_task, project_root, live_profile
    )
    launch = resolved.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None
    assert launch.resolvable is False
    assert "acpx" in launch.detail
    assert launch.client_entry == ""

    report = build_prepare_report(resolved)
    assert report.admission.ok is True, "the task itself is fine"
    assert [issue.code.value for issue in report.dispatch_preconditions] == [
        "not_implemented"
    ]
    assert "could not be resolved" in report.dispatch_preconditions[0].detail
    assert resolved.ready_to_dispatch is False
    assert report.model_calls_made == 0
    assert any("dispatch precondition" in note for note in report.notes)


def test_each_roles_model_selection_is_bound_into_its_launch(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model a role asks for is part of *its* launch, so the approval digest covers it.

    ``native_profile`` passes nothing: its launch has no model at all, so a configuration that
    never selected a model binds exactly what it bound before model passing existed.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    pair = '["deepseek-official","deepseek-v4-pro"]'
    profile = live_profile.model_copy(
        update={
            "agents": {
                "dsh-implementer": live_profile.agents["dsh-implementer"].model_copy(
                    update={"model_selection": pair}
                ),
                "dsh-reviewer": live_profile.agents["dsh-reviewer"].model_copy(
                    update={"model_selection": "native_profile"}
                ),
            }
        }
    )
    resolved = _resolve_live(tmp_path, live_project, worktree_task, project_root, profile)
    implementer = resolved.effective.role("implementer").launch  # type: ignore[union-attr]
    reviewer = resolved.effective.role("reviewer").launch  # type: ignore[union-attr]
    assert implementer is not None and reviewer is not None
    assert implementer.model == pair
    assert reviewer.model == ""
    assert "model" not in reviewer.model_dump(mode="json"), "native_profile binds no model key"

    # The same configuration without the implementer's model is a different approval.
    without_model = resolved.effective.model_copy(
        update={
            "roles": [
                entry.model_copy(
                    update={"launch": entry.launch.model_copy(update={"model": ""})}  # type: ignore[union-attr]
                )
                for entry in resolved.effective.roles
            ]
        }
    )
    assert without_model.digest() != resolved.effective.digest()

    # The preview names the flag and says the capability is still only documented.
    report = build_prepare_report(resolved)
    note = " ".join(report.notes)
    assert "--model" in note and "documented" in note


def test_a_native_profile_launch_binds_what_it_bound_before_model_passing() -> None:
    """Pinned digest of a fixed ``native_profile`` launch, computed before ``model`` existed."""
    from hflow.contracts import EffectiveConfig, LaunchConfig, RoleConfig, digest_of

    launch = LaunchConfig(
        driver_id="acpx-dsh-acp",
        harness="dsh",
        agent_argv=["dsh", "--profile", "acp"],
        client_argv_prefix=["node"],
        client_entry="cli.js",
        node="node",
        python="python",
        dsh_executable="dsh",
        profile="acp",
    )
    assert digest_of(launch.model_dump(mode="json")) == (
        "sha256:bd08c87743770efa6d8d18191cc54feace405ca5a74e0d64e8f90ed07efbd387"
    )
    effective = EffectiveConfig(
        source="machine_profile",
        profile_id="p",
        roles=[
            RoleConfig(
                role="implementer",
                agent="a",
                harness="dsh",
                driver="acpx-dsh",
                driver_id="acpx-dsh-acp",
                launch=launch,
            )
        ],
    )
    assert effective.digest() == (
        "sha256:f7eaf12696b92310455f8f9ff3d9e047f305063920b818a2e7f89dace7bf8604"
    )
    chosen = launch.model_copy(update={"model": "deepseek-v4-pro"})
    assert digest_of(chosen.model_dump(mode="json")) != digest_of(launch.model_dump(mode="json"))


# --------------------------------------------------------------------------
# the exit code answers "will this run?", not "is the task well formed?"
# --------------------------------------------------------------------------


def test_prepare_refuses_a_write_task_that_the_write_opt_in_does_not_cover(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The repro: writes off, so a run refuses with scope_violation - and prepare must too."""
    monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["admission"]["ok"] is True, "the task itself is admissible"
    assert payload["effective_config"]["implementer_writes"] is False
    assert [issue["code"] for issue in payload["dispatch_preconditions"]] == [
        "scope_violation"
    ]
    assert code == EXIT_REFUSED, "a task the run would refuse must not preview as ready"


def test_prepare_refuses_a_write_task_with_no_worktree(
    tmp_path: Path, live_project, task_spec, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The in-place half of the same rule, through the CLI."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task_spec)  # default: in_place
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    # In place *is* writes-off for this purpose, so both halves of the rule fire - and the
    # first one is the actionable one: an in-place run would write into the user's checkout.
    assert {issue["code"] for issue in payload["dispatch_preconditions"]} == {
        "scope_violation"
    }
    assert "worktree" in payload["dispatch_preconditions"][0]["detail"]


def test_prepare_refuses_a_reviewed_task_whose_budget_covers_one_turn(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The second repro: admission ok, budget short of the review - the run still refuses."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    task = worktree_task.model_copy(
        update={"budget": worktree_task.budget.model_copy(update={"max_agent_turns": 1})}
    )
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["admission"]["ok"] is True
    assert payload["budget"]["within_budget"] is False
    assert payload["budget"]["required_turns"] == 2
    assert [issue["code"] for issue in payload["dispatch_preconditions"]] == [
        "budget_exceeded"
    ]
    assert code == EXIT_REFUSED


def test_prepare_refuses_a_run_whose_starting_workspace_holds_a_client_config(
    tmp_path: Path, live_project, worktree_task, task_spec, project_root: Path, live_profile,
    acpx_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acpx would let ``.acpxrc.json`` replace the agent command, so the driver refuses at spawn.

    Refused there, the dispatch was already reserved and the task cannot simply be submitted
    again. When the file is already in what the run starts from - the base commit's tree for a
    worktree run, the project root for an in-place run - the preview and the run gate say so
    before anything is spent. A file the user has but did not commit is not in the worktree, so
    it does not refuse a worktree run.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    _git(project_root, "init", "-q", "-b", "main")
    (project_root / ".acpxrc.json").write_text('{"agents": {}}', encoding="utf-8")
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", "base with a client config")
    task = worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )

    def codes(resolved) -> list[str]:
        return [issue.code.value for issue in resolved.dispatch_preconditions]

    committed = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert "workspace_client_config" in codes(committed), committed.dispatch_preconditions
    issue = next(
        issue for issue in committed.dispatch_preconditions
        if issue.code.value == "workspace_client_config"
    )
    assert ".acpxrc.json" in issue.detail and "commit" in issue.detail, issue.detail
    assert committed.ready_to_dispatch is False

    # Removed and committed: the base no longer has it. The user's untracked copy does not count.
    _git(project_root, "rm", "-q", "--cached", ".acpxrc.json")
    _git(project_root, "commit", "-q", "-m", "drop the client config")
    removed = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert (project_root / ".acpxrc.json").exists()
    assert "workspace_client_config" not in codes(removed), removed.dispatch_preconditions

    # In place, the project root is the workspace, so the file there refuses the run.
    in_place = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)
    assert "workspace_client_config" in codes(in_place), in_place.dispatch_preconditions


@pytest.mark.parametrize("committed", [".ACPXRC.JSON", ".AcpxRc.json"])
def test_prepare_refuses_a_base_commit_with_any_spelling_of_the_client_config(
    tmp_path: Path, live_project, worktree_task, task_spec, project_root: Path, live_profile,
    acpx_client, monkeypatch: pytest.MonkeyPatch, committed: str,
) -> None:
    """A worktree checks out the committed name; acpx then opens ``<cwd>/.acpxrc.json``.

    On a case-insensitive filesystem that open finds ``.ACPXRC.JSON`` too, and so does the
    driver's spawn gate - after the dispatch was reserved. So admission matches the base tree's
    root entries without regard to case and refuses before anything is spent, naming the
    committed spelling. Only the workspace root counts, as for the exact name.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    _git(project_root, "init", "-q", "-b", "main")
    (project_root / committed).write_text('{"agents": {}}', encoding="utf-8")
    nested = project_root / "src" / committed
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("{}", encoding="utf-8")
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", "base with a mixed-case client config")
    (project_root / committed).unlink()  # only what the base commit holds may decide this
    task = worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )

    resolved = _resolve_live(tmp_path, live_project, task, project_root, live_profile)

    issues = [
        issue for issue in resolved.dispatch_preconditions
        if issue.code.value == "workspace_client_config"
    ]
    assert issues, resolved.dispatch_preconditions
    assert committed in issues[0].detail and "commit" in issues[0].detail, issues[0].detail
    assert resolved.ready_to_dispatch is False

    # Only the root entry is acpx's project config: with it removed, the nested one is no issue.
    _git(project_root, "rm", "-q", "--cached", committed)
    _git(project_root, "commit", "-q", "-m", "drop the root client config")
    removed = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert "workspace_client_config" not in [
        issue.code.value for issue in removed.dispatch_preconditions
    ], removed.dispatch_preconditions


def test_prepare_refuses_a_launcher_that_lies_inside_the_workspace(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile,
    acpx_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``dsh`` found on PATH inside the project is a file the agent can write: not resolvable."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    planted = project_root / "node_modules" / ".bin"
    planted.mkdir(parents=True)
    (planted / ("dsh.CMD" if os.name == "nt" else "dsh")).write_text("", encoding="utf-8")
    if os.name != "nt":
        (planted / "dsh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ['PATH']}")

    resolved = _resolve_live(tmp_path, live_project, worktree_task, project_root, live_profile)

    launch = resolved.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None and launch.resolvable is False
    assert "inside the workspace" in launch.detail, launch.detail
    assert resolved.ready_to_dispatch is False


def test_the_dispatch_preconditions_are_the_ones_the_run_gate_raises(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rule, two callers: the preview's list is the gate's list, compared as values."""
    from hflow.admission import predictable_dispatch_problems
    from hflow.controller import Controller
    from hflow.drivers.fake import FakeDriver, FakeScript
    from hflow.store import Store
    from hflow.verify import CheckRunners, FakeCheckRunner

    monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
    resolved = _resolve_live(
        tmp_path, live_project, worktree_task, project_root, live_profile
    )
    report = build_prepare_report(resolved)

    store = Store(tmp_path / "hflow.sqlite")
    check_runner = FakeCheckRunner()
    controller = Controller(
        store,
        FakeDriver(project_root, FakeScript()),
        controller_build="precondition-test",
        runners=CheckRunners({"fake": check_runner, "command": check_runner}),
        production=True,
        effective_config=resolved.effective,
    )
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller._assert_dispatch_preconditions(resolved.spec, resolved.project, 900)
    finally:
        store.close()

    assert report.dispatch_preconditions
    assert excinfo.value.code.value == report.dispatch_preconditions[0].code.value
    assert excinfo.value.message == report.dispatch_preconditions[0].detail
    # And the shared function is what produced them, from the same resolved inputs.
    assert predictable_dispatch_problems(
        resolved.spec,
        resolved.project,
        production=True,
        implementer_writes=resolved.effective.implementer_writes,
        launches=[],
    )[0].detail == report.dispatch_preconditions[0].detail
