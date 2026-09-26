"""Batch E1 acceptance item 7: ``hflow prepare`` with a root budget file writes nothing.

A preview that created the SQLite ledger it names would be a different command, so this file
pins the three absences and the four reported facts:

* **no SQLite file, no run row, no authorization** - asserted by the absence of the database the
  preview names (a file that does not exist cannot hold a row), plus the report's own pinned
  ``creates_authorization: false`` and ``model_calls_made: 0``;
* the **root binding** it would register, the **limits** from the file, the **required
  submission count** for one accepted delivery, and that **repair is not implemented** (the
  switch is off and the allowance is recorded but never spent) - in both the JSON and the text
  form;
* the **exit code follows ``ready_to_dispatch``**: a task the run would dispatch previews as
  ready, a task the run would refuse previews as refused even though a root file was given.

Everything runs offline through the real CLI entry point: the fake driver, ``tmp_path``, no
profile, no model, no network. The flag this file depends on is ``--root-budget-file`` as it
exists on ``prepare``; the arguments below are exactly the ones that surface accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.cli import EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import root_id_for
from hflow.paths import database_path

from .conftest import write_project, write_task

PROJECT_ID = "demo-project"
#: One implementer + one reviewer: the submission count a preview must report, taken from the
#: approved loop rather than from the ceiling in the file.
REQUIRED_SUBMISSIONS = 2
LIMITS = {"max_top_level_submissions": 4, "max_repairs": 1, "deadline_seconds": 3600}


def _write_root_budget(
    path: Path,
    *,
    limits: dict[str, object] | None = None,
    note: str = "one accepted delivery of T-001, written by the user",
) -> Path:
    """The user's root budget file: ceilings for one root, never an approval."""
    path.write_text(
        json.dumps({"limits": dict(limits or LIMITS), "note": note}), encoding="utf-8"
    )
    return path


def _prepare_argv(
    *,
    task: Path,
    project: Path,
    project_root: Path,
    root_budget: Path | None,
    data_dir: Path,
    json_output: bool = True,
) -> list[str]:
    argv = [
        "prepare",
        "--task", str(task),
        "--project", str(project),
        "--project-root", str(project_root),
    ]
    if root_budget is not None:
        argv += ["--root-budget-file", str(root_budget)]
    if json_output:
        argv.append("--json")
    argv += ["--data-dir", str(data_dir)]
    return argv


def _prepare(
    tmp_path: Path,
    project,
    spec,
    project_root: Path,
    *,
    root_budget: Path | None,
    json_output: bool = True,
) -> tuple[int, Path]:
    """Run the real CLI. Returns ``(exit_code, data_dir)``, the directory it must not write to."""
    task_file = write_task(tmp_path / "task.json", spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    data_dir = tmp_path / "data"
    code = main(
        _prepare_argv(
            task=task_file,
            project=project_file,
            project_root=project_root,
            root_budget=root_budget,
            data_dir=data_dir,
            json_output=json_output,
        )
    )
    return code, data_dir


# --------------------------------------------------------------------------
# 1. zero write, and the four reported facts
# --------------------------------------------------------------------------


def test_prepare_with_a_root_budget_file_creates_no_database_run_or_authorization(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The preview names a ledger, a root and a count - and creates none of the state behind them."""
    root_file = _write_root_budget(tmp_path / "root-budget.json")

    code, data_dir = _prepare(
        tmp_path, project, task_spec, project_root, root_budget=root_file
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK, "a task the run would dispatch must preview as ready"
    assert payload["model_calls_made"] == 0

    root = payload["root_budget"]
    assert root is not None, "the root this run would be spent against must be reported"
    assert root["limits"] == LIMITS, "the preview reports the ceilings from the file, verbatim"
    assert root["required_top_level_submissions"] == REQUIRED_SUBMISSIONS, (
        "one accepted delivery is implementer + reviewer, not the file's ceiling"
    )
    assert root["repair_enabled"] is False, "a budget field is not an implemented repair loop"
    assert "no repair loop" in root["detail"], root["detail"]
    assert any(
        "repair" in note and "not implemented" in note for note in payload["notes"]
    ), payload["notes"]

    # Zero write. The database the preview names must not exist: a file that was never created
    # holds no run row and no authorization, which is a stronger statement than counting rows in
    # a ledger the preview would have had to open.
    ledger = Path(root["binding"]["ledger_path"])
    assert ledger == database_path(data_dir).resolve(), "the preview must name the real ledger path"
    assert not ledger.exists(), "prepare created the SQLite ledger it named"
    assert list(data_dir.rglob("*.sqlite")) == []
    # The artifact is described, never minted.
    assert payload["authorization"]["required"] is True, (
        "a root is charged with an approval: the preview must say one is needed"
    )
    assert payload["authorization"]["creates_authorization"] is False
    assert payload["authorization"]["max_top_level_submissions_required"] == REQUIRED_SUBMISSIONS
    assert any("created no run row" in note for note in payload["notes"]), payload["notes"]


def test_the_reported_root_is_the_binding_the_run_will_use(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Identity, path and the pending binding: the same root ``run`` resolves from the same file.

    A preview that derived a different root than the run would print an artifact digest the run
    then refuses, so this is checked field for field rather than "a root id was printed".
    """
    root_file = _write_root_budget(tmp_path / "root-budget.json")

    code, data_dir = _prepare(
        tmp_path, project, task_spec, project_root, root_budget=root_file
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK

    binding = payload["root_budget"]["binding"]
    assert binding["root_id"] == root_id_for(
        project_id=PROJECT_ID, repo_path=str(project_root), task_id=task_spec.task_id
    ), "the root id is derived mechanically from (project, repository, task), never chosen"
    assert binding["project_id"] == PROJECT_ID
    assert binding["task_id"] == task_spec.task_id
    assert binding["repo_path"] == str(project_root.resolve())
    assert Path(binding["ledger_path"]) == database_path(data_dir).resolve()

    # The pending authorization carries the identical root: prepare and run agree on one binding.
    pending = payload["authorization"]["binding"]
    assert pending["root_budget"] == binding
    assert pending["spec_digest"] == task_spec.spec_digest()


def test_the_text_preview_names_the_root_limits_and_the_off_repair_switch(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human-readable form reports the same facts as the JSON form, not a summary of them."""
    root_file = _write_root_budget(tmp_path / "root-budget.json")

    code, data_dir = _prepare(
        tmp_path, project, task_spec, project_root, root_budget=root_file, json_output=False
    )
    text = capsys.readouterr().out

    assert code == EXIT_OK
    assert "root budget" in text
    assert root_id_for(
        project_id=PROJECT_ID, repo_path=str(project_root), task_id=task_spec.task_id
    ) in text, "the text form must name the binding it would register"
    assert str(database_path(data_dir)) in text
    assert f"repairs {LIMITS['max_repairs']}" in text
    assert f"needs       {REQUIRED_SUBMISSIONS} top-level submission(s)" in text
    assert "repair      OFF" in text and "not implemented in E1" in text, text


# --------------------------------------------------------------------------
# 2. the exit code follows ready_to_dispatch
# --------------------------------------------------------------------------


def test_the_exit_code_follows_ready_to_dispatch_not_the_root_file(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A root file does not make a refused task ready: the preview refuses and still writes nothing."""
    root_file = _write_root_budget(tmp_path / "root-budget.json")
    refused_spec = task_spec.model_copy(update={"dependencies": ["T-000"]})

    code, data_dir = _prepare(
        tmp_path, project, refused_spec, project_root, root_budget=root_file
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED, "the exit code must follow what a run would do, not the root file"
    assert payload["admission"]["ok"] is False
    assert "dependencies" in json.dumps(payload["admission"]["issues"])
    assert payload["root_budget"] is not None, "the root is still reported for a refused task"
    assert not database_path(data_dir).exists(), "a refused preview writes nothing either"


# --------------------------------------------------------------------------
# 3. a root budget file that is not a plan is refused, never defaulted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param({"max_top_level_submissions": 0, "max_repairs": 0, "deadline_seconds": 3600},
                     id="a ceiling this build cannot honour"),
        pytest.param({"max_top_level_submissions": 4, "max_repairs": 9, "deadline_seconds": 3600},
                     id="more repairs than the range allows"),
        pytest.param({"max_top_level_submissions": 4, "max_repairs": 0, "deadline_seconds": 5},
                     id="a deadline shorter than the minimum"),
    ],
)
def test_a_root_budget_file_outside_the_contract_is_refused(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
    limits: dict[str, object],
) -> None:
    """A ceiling the user did not choose must not be replaced by a build default."""
    root_file = _write_root_budget(tmp_path / "root-budget.json", limits=limits)

    code, data_dir = _prepare(
        tmp_path, project, task_spec, project_root, root_budget=root_file
    )
    captured = capsys.readouterr()

    assert code == EXIT_REFUSED, captured
    assert "root budget file" in captured.err, captured.err
    assert not database_path(data_dir).exists()
    assert captured.out.strip() == "", "a refused preview prints no report to parse"
