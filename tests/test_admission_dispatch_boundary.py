"""The admission gate must close *before* anything real is created.

``test_contracts.py`` checks which TaskSpecs are refused. This file checks the other
half: that a refused spec creates no run row, starts no driver invocation and consumes
no authorization allowance. A gate that refuses late is not a gate - it is a report
written after the spending.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import (
    DeliveryRequirement,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    TaskSpec,
)
from hflow.drivers.fake import FakeDriver
from hflow.store import Store

from .conftest import write_project, write_task


def _refused_specs(task_spec: TaskSpec) -> dict[str, TaskSpec]:
    """One spec per admission gap this change closes."""
    return {
        "reuse fit test pending": task_spec.model_copy(
            update={
                "reuse": ReuseDecision(
                    status=ReuseStatus.DECIDED,
                    need="structured automation transport",
                    choice="reuse",
                    required_fit_test="acpx one-shot round trip",
                    fit_test_status="pending",
                    reason="the recorded decision still covers this use",
                )
            }
        ),
        "adapt fit test failed": task_spec.model_copy(
            update={
                "reuse": ReuseDecision(
                    status=ReuseStatus.DECIDED,
                    need="structured automation transport",
                    choice="adapt",
                    required_fit_test="acpx one-shot round trip",
                    fit_test_status="failed",
                    reason="adapt the existing client",
                )
            }
        ),
        "delivery integrated": task_spec.model_copy(
            update={"delivery": DeliveryRequirement(mode="integrated")}
        ),
        "delivery published": task_spec.model_copy(
            update={"delivery": DeliveryRequirement(mode="published")}
        ),
    }


def test_refused_specs_create_no_run_and_start_no_invocation(
    store: Store, driver: FakeDriver, run_request: RunRequest, controller
) -> None:
    """Requirement 4: the refusal path creates no real invocation and spends nothing."""
    for name, spec in _refused_specs(run_request.task).items():
        request = run_request.model_copy(update={"task": spec})

        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(request)

        assert excinfo.value.code in {
            RefusalCode.REUSE_NOT_APPROVED,
            RefusalCode.NOT_IMPLEMENTED,
        }, name
        assert store.list_runs() == [], f"{name}: a refused task must not create run state"
        assert driver.started == [], f"{name}: nothing may be dispatched"


def test_delivery_refusal_names_the_gap_instead_of_promising_a_lower_level(
    controller, run_request: RunRequest, store: Store
) -> None:
    """A success exit must not stand for a delivery level that was never reached."""
    request = run_request.model_copy(
        update={
            "task": run_request.task.model_copy(
                update={"delivery": DeliveryRequirement(mode="published")}
            )
        }
    )
    with pytest.raises(RefusedError) as excinfo:
        controller.run_task(request)

    assert excinfo.value.code is RefusalCode.NOT_IMPLEMENTED
    assert "not implemented" in excinfo.value.message
    assert store.list_runs() == []


def test_refusal_happens_before_an_authorization_is_even_registered(
    tmp_path: Path, project: ProjectConfig, task_spec: TaskSpec, project_root: Path, driver: FakeDriver
) -> None:
    """No credential is read and no allowance is claimed for a spec that cannot run."""
    from hflow.authorization import AuthorizationBinding, AuthorizationRecord
    from hflow.controller import Controller

    store = Store(tmp_path / "data" / "hflow.sqlite")
    try:
        authorization = AuthorizationRecord(
            authorization_id="AUTH-admission-boundary",
            user_text="supervised small task, one implementer and one reviewer",
            authorized_at="2026-09-21T00:00:00Z",
            max_top_level_submissions=2,
            binding=AuthorizationBinding(
                mode="m2-live-change",
                driver="acpx-dsh",
                project_id=project.project_id,
                repo_path=str(project_root),
                base_commit="0" * 40,
                spec_digest=task_spec.spec_digest(),
                spec_path=str(tmp_path / "task.json"),
            ),
        )
        guarded = Controller(
            store,
            driver,
            controller_build="test-build",
            authorization=authorization,
            preflight=lambda: (True, "the zero-model preflight must not be reached"),
        )
        spec = task_spec.model_copy(update={"delivery": DeliveryRequirement(mode="published")})

        with pytest.raises(RefusedError) as excinfo:
            guarded.run_task(
                RunRequest(
                    task=spec,
                    project=project,
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )

        assert excinfo.value.code is RefusalCode.NOT_IMPLEMENTED
        assert store.list_runs() == []
        assert store.authorization_state("AUTH-admission-boundary") is None, (
            "an inadmissible spec must not even register the authorization, let alone claim it"
        )
        assert driver.started == []
    finally:
        store.close()


def test_the_cli_refuses_an_unsupported_delivery_level_without_run_state(
    tmp_path: Path,
    project: ProjectConfig,
    task_spec: TaskSpec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same gate through the entry point a user actually types."""
    from hflow.cli import EXIT_REFUSED, main

    task_file = write_task(
        tmp_path / "task.json",
        task_spec.model_copy(update={"delivery": DeliveryRequirement(mode="integrated")}),
    )
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    data_dir = tmp_path / "data"

    exit_code = main(
        [
            "run",
            "--task",
            str(task_file),
            "--project",
            str(project_file),
            "--project-root",
            str(project_root),
            "--json",
            "--data-dir",
            str(data_dir),
        ]
    )

    assert exit_code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True
    assert payload["issues"][0]["code"] == "not_implemented"
    assert payload["issues"][0]["location"] == "delivery.mode"
    assert payload["warnings"] == []

    store = Store(data_dir / "hflow.sqlite")
    try:
        assert store.list_runs() == []
    finally:
        store.close()
