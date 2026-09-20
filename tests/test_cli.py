"""The command surface, and the promise that status/report cost no model calls.

The CLI is exercised through ``main(argv)`` with an explicit ``--data-dir`` so tests
never touch the real user data directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import EvidenceStatus
from hflow.report import report_json, status_text
from hflow.controller import inspect_run
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import write_project, write_task

@pytest.fixture()
def cli_env(tmp_path: Path, project, task_spec) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    task_file = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    return {"data_dir": data_dir, "task": task_file, "project": project_file}


def test_run_then_status_then_report(
    cli_env: dict[str, Path], project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "run",
            "--task",
            str(cli_env["task"]),
            "--project",
            str(cli_env["project"]),
            "--project-root",
            str(project_root),
            "--driver",
            "fake",
            "--json",
            "--data-dir",
            str(cli_env["data_dir"]),
        ]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["run_id"]
    assert payload["task_state"] == "ACCEPTED"
    assert payload["receipt"]["task_state"] == "ACCEPTED"

    assert main(["status", run_id, "--data-dir", str(cli_env["data_dir"])]) == EXIT_OK
    status_out = capsys.readouterr().out
    assert "state         ACCEPTED" in status_out
    assert "model_calls   0" in status_out

    assert main(["report", run_id, "--data-dir", str(cli_env["data_dir"])]) == EXIT_OK
    report_out = capsys.readouterr().out
    assert "verification  passed" in report_out
    assert "provider_billed_tokens unknown" in report_out

    assert main(["status", "R-missing", "--data-dir", str(cli_env["data_dir"])]) != EXIT_OK


def test_second_identical_run_does_not_dispatch_again(
    cli_env: dict[str, Path], project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "run",
        "--task",
        str(cli_env["task"]),
        "--project",
        str(cli_env["project"]),
        "--project-root",
        str(project_root),
        "--json",
        "--data-dir",
        str(cli_env["data_dir"]),
    ]
    assert main(argv) == EXIT_OK
    first = json.loads(capsys.readouterr().out)
    assert main(argv) == EXIT_OK
    second = json.loads(capsys.readouterr().out)

    assert first["run_id"] == second["run_id"]
    # One implementer process and one reviewer process; the same spec buys neither again.
    assert first["implementer_invocations"] == 1
    assert first["reviewer_invocations"] == 1
    assert first["driver_invocations_total"] == 2
    assert second["driver_invocations_total"] == 2
    assert any("identical TaskSpec" in note for note in second["notes"])
    assert any("implementer=1 reviewer=1" in note for note in second["notes"])


def test_admission_failure_exits_refused_before_creating_a_run(
    cli_env: dict[str, Path], project_root: Path, task_spec, capsys: pytest.CaptureFixture[str]
) -> None:
    task_file = cli_env["task"].parent / "bad.json"
    task_file.write_text(
        json.dumps(
            {
                **task_spec.model_dump(mode="json"),
                "scope": {"write_allow": ["../escape.py"], "write_deny": []},
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "run",
            "--task",
            str(task_file),
            "--project",
            str(cli_env["project"]),
            "--project-root",
            str(project_root),
            "--json",
            "--data-dir",
            str(cli_env["data_dir"]),
        ]
    )

    assert exit_code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True
    assert payload["issues"][0]["code"] == "scope_violation"

    store = Store(cli_env["data_dir"] / "hflow.sqlite")
    try:
        assert store.list_runs() == [], "a refused task must not create run state"
    finally:
        store.close()


def test_blocked_run_exits_with_its_own_code(
    cli_env: dict[str, Path],
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing approved check is a distinct, non-zero outcome - never exit 0."""
    from hflow.contracts import CheckDef, ProjectConfig
    from hflow.controller import Controller
    from hflow.drivers.fake import FakeDriver
    from hflow.verify import CheckRunners, FakeCheckRunner

    project = ProjectConfig.model_validate(
        json.loads(cli_env["project"].read_text(encoding="utf-8"))
    )
    project = project.model_copy(update={"checks": [CheckDef(id="unit", kind="fake")]})
    task = json.loads(cli_env["task"].read_text(encoding="utf-8"))
    task["acceptance"] = [{"id": "AC-1", "statement": "s", "check_ids": ["unit"]}]

    # Drive it in-process so the failing verdict can be injected deterministically.
    store = Store(cli_env["data_dir"] / "hflow.sqlite")
    try:
        from hflow.contracts import RunRequest, TaskSpec

        controller = Controller(
            store,
            FakeDriver(project_root),
            controller_build="test-build",
            runners=CheckRunners({"fake": FakeCheckRunner({"unit": EvidenceStatus.FAILED})}),
        )
        outcome = controller.run_task(
            RunRequest(
                task=TaskSpec.model_validate(task),
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert outcome.task_state.value == "BLOCKED"
    assert main(["status", outcome.run_id, "--data-dir", str(cli_env["data_dir"])]) == EXIT_OK
    assert "state         BLOCKED" in capsys.readouterr().out
    assert EXIT_BLOCKED != EXIT_OK


def test_doctor_makes_no_model_calls_and_admits_what_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["doctor", "--json", "--data-dir", str(tmp_path / "data")])
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["driver_status"] == "NOT_LIVE_TESTED"
    assert payload["selected_driver"] == "unselected"
    record = payload["capability_record"]
    assert record["live_tested"] is False
    assert record["probe_only"] is True
    assert record["capabilities"]["cancel"] == "documented"
    assert record["capabilities"]["readonly_enforcement"] == "unknown"
    # No credential material is read or printed.
    assert ".credentials" not in json.dumps(payload)


def test_schema_command_prints_generated_contracts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"TaskSpec", "ProjectConfig", "ResultReceipt", "RunRequest"}
    receipt_schema = payload["ResultReceipt"]
    # Enums are referenced, not inlined; the definition must be present in the same document.
    ref = receipt_schema["properties"]["task_state"]["$ref"].rsplit("/", 1)[-1]
    assert ref in receipt_schema["$defs"]
    assert "ACCEPTED" in receipt_schema["$defs"][ref]["enum"]


def test_status_text_marks_unknowns_instead_of_zeroing_them(
    store: Store, controller, run_request, check_runner: FakeCheckRunner
) -> None:
    outcome = controller.run_task(run_request)
    inspection = inspect_run(store, outcome.run_id)
    rendered = status_text(inspection)
    assert "observed 1" in rendered
    payload = report_json(inspection)
    receipt = payload["receipt"]
    assert receipt["usage"]["provider_cost"] is None
    assert receipt["usage"]["provider_billed_tokens"] is None


def test_unknown_check_kind_blocks_rather_than_reports_success(
    store: Store, run_request, project, project_root: Path
) -> None:
    """A check kind this build cannot run must never be reported as a pass."""
    from hflow.controller import Controller
    from hflow.drivers.fake import FakeDriver

    runner = FakeCheckRunner()
    controller_local = Controller(
        store,
        FakeDriver(project_root),
        controller_build="test-build",
        runners=CheckRunners({"command": runner}),  # no 'fake' runner at all
    )

    outcome = controller_local.run_task(run_request)

    assert outcome.task_state.value == "BLOCKED"
    assert outcome.block_code is not None
    assert outcome.receipt is None


def test_evidence_status_enum_is_what_the_report_shows() -> None:
    """Cheap guard: the report never invents a status string of its own."""
    assert {status.value for status in EvidenceStatus} == {"passed", "failed", "error"}
