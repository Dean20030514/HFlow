"""The saved-plan flow: ``prepare --plan-out`` -> ``approve --plan`` -> ``run --plan``.

A plan writes a task's resolution inputs and its preview down once, so ``run`` no longer needs
every ``prepare`` flag repeated and the operator no longer copies ``authorization.binding`` into a
hand-built artifact. What must stay true is asserted here, next to the behaviour:

* a plan is never an authorization, and ``approve`` writes one only for a person at a terminal:
  piped stdin is refused unread, and the plan's approval code must be typed back before the
  approval text is read;
* ``prepare --plan-out`` replaces an older plan and nothing else;
* a plan whose inputs now resolve to another binding is stale, and ``approve`` and ``run`` refuse
  it rather than approving or running something nobody saw;
* ``run --plan`` takes every resolution input from the plan (a flag given as well is a usage
  error), and every existing check still applies;
* the end-to-end path reaches the same outcome as the hand-built artifact in
  ``test_e2e_cli_loop.py``, through ``python -m hflow`` - with the two approval lines typed at a
  simulated terminal, since piped input is refused.

The offline fake driver and a stand-in acpx client are used throughout; nothing calls a model.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hflow.authorization import load_authorization, verify_authorization
from hflow import cli
from hflow.cli import (
    _STATE_EXIT_CODES,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    build_parser,
    main,
)
from hflow.contracts import PlanArguments, RefusalCode, RefusedError, RunPlan, TaskState
from hflow.hints import next_lines, next_steps
from hflow.hints import shell_word as _shell_word
from hflow.paths import database_path
from hflow.plan import (
    RESOLUTION_FLAGS,
    approval_code,
    default_authorization_path,
    frozen_arguments,
    load_plan,
    prepare_command,
)
from hflow.prepare import pending_binding, resolve_arguments, root_binding_for
from hflow.store import Store

from .conftest import write_profile, write_project, write_task
from .test_e2e_cli_loop import (
    ROOT_LIMITS,
    SUBPROCESS_TIMEOUT,
    Call,
    CliProject,
    git,
    hflow_env,
)

APPROVAL_TEXT = "I approve this one bounded offline task of T-e2e and nothing else."


@pytest.fixture()
def cli_project(tmp_path: Path) -> CliProject:
    """The same Git repository, task, contract and root budget as the hand-built e2e loop."""
    return CliProject(tmp_path, user_branch="work")


class _TerminalInput(io.StringIO):
    """Standard input that reports itself as a terminal."""

    def isatty(self) -> bool:
        return True


class _UnreadableInput(io.StringIO):
    """Standard input that must not be read: the command has to refuse before asking.

    A terminal by default, so a refusal that comes first is shown to come first even when a
    person is there to answer; ``terminal=False`` stands for a pipe or a redirected file.
    """

    def __init__(self, *, terminal: bool = True) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal

    def read(self, *args: object) -> str:  # type: ignore[override]
        raise AssertionError("stdin was read")

    def readline(self, *args: object) -> str:  # type: ignore[override]
        raise AssertionError("stdin was read")


def _typed(plan: Path, text: str = APPROVAL_TEXT) -> _TerminalInput:
    """A person at a terminal typing the plan's approval code, then ``text``."""
    return _TerminalInput(f"{approval_code(load_plan(plan).binding_digest)}\n{text}\n")


def _plan_path(project: CliProject) -> Path:
    return project.tmp_path / "inputs" / "task.plan.json"


def _offline_flags(project: CliProject) -> list[str]:
    """The fake driver without a root budget: a plan that needs no authorization."""
    return [
        "--task", str(project.task_file),
        "--project", str(project.project_file),
        "--project-root", str(project.repo),
        "--driver", "fake",
    ]


def _prepare(project: CliProject, *flags: str, plan: Path | None = None) -> int:
    target = plan or _plan_path(project)
    return main(
        [
            "prepare", *(flags or project.admission_flags()),
            "--plan-out", str(target), "--json",
            "--data-dir", str(project.data_dir),
        ]
    )


def _approve(
    monkeypatch: pytest.MonkeyPatch, plan: Path, stdin: io.StringIO, *extra: str
) -> int:
    monkeypatch.setattr(sys, "stdin", stdin)
    return main(["approve", "--plan", str(plan), *extra])


def _run_plan(project: CliProject, *extra: str) -> int:
    plan = str(_plan_path(project))
    return main(
        ["run", "--plan", plan, *extra, "--fake-write-plan", str(project.plan_file), "--json"]
    )


def _authorizations(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.authorization.json"))


# --------------------------------------------------------------------------
# the plan file
# --------------------------------------------------------------------------


def test_a_plan_round_trips_and_records_absolute_inputs(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    monkeypatch.chdir(project.tmp_path)
    relative = [
        "--task", os.path.relpath(project.task_file),
        "--project", os.path.relpath(project.project_file),
        "--project-root", "project",
        "--driver", "fake",
        "--root-budget-file", os.path.relpath(project.root_file),
    ]
    plan_file = Path("inputs") / "task.plan.json"

    argv = ["prepare", *relative, "--plan-out", str(plan_file), "--json", "--data-dir", "data"]
    assert main(argv) == EXIT_OK
    captured = capsys.readouterr()
    report = json.loads(captured.out)  # --json: stdout is the report alone
    assert f"plan          {os.path.abspath(plan_file)}" in captured.err
    # Offline with a root budget: `run --plan` runs it directly (charging the root to a
    # CLI-minted record that says it is not an approval); approving first stays optional.
    assert "offline root run" in captured.err
    assert f"next: hflow run --plan {_shell_word(os.path.abspath(plan_file))}\n" in captured.err
    assert f"next: hflow approve --plan {_shell_word(os.path.abspath(plan_file))}" in captured.err

    plan = load_plan(plan_file)
    assert RunPlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.kind == "hflow-plan"
    assert plan.prepare_report.model_dump(mode="json") == report
    arguments = plan.arguments
    assert arguments.task == os.path.abspath(project.task_file)
    assert arguments.project == os.path.abspath(project.project_file)
    assert arguments.project_root == os.path.abspath(project.repo)
    assert arguments.root_budget_file == os.path.abspath(project.root_file)
    assert arguments.data_dir == os.path.abspath(project.data_dir)
    assert arguments.driver == "fake"
    assert arguments.authorization_mode == "m2-live-change"
    pending = plan.prepare_report.authorization
    assert pending.required is True
    assert pending.creates_authorization is False
    assert plan.binding_digest == pending.binding_digest
    assert plan.binding == pending.binding
    assert plan.root_limits is not None
    assert plan.root_limits.model_dump(mode="json") == ROOT_LIMITS
    assert not database_path(project.data_dir).exists(), "prepare --plan-out opened no ledger"

    # Re-preparing replaces the plan (atomically); approvals are never replaced.
    argv = ["prepare", *relative, "--plan-out", str(plan_file), "--json", "--data-dir", "data"]
    assert main(argv) == EXIT_OK
    capsys.readouterr()
    assert load_plan(plan_file).binding_digest == plan.binding_digest
    assert [path.name for path in plan_file.resolve().parent.glob(".task.plan.json.*")] == []


def test_a_plan_is_not_written_for_a_task_the_run_would_refuse(
    cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    policy = project.tmp_path / "inputs" / "repair-policy.json"
    policy.write_text(
        json.dumps({"max_attempts": 1, "check_exit_codes": {"unit": [1]}}), encoding="utf-8"
    )
    # max_repairs 0 cannot pay for the armed repair: a dispatch precondition, so exit 2.
    flags = [*project.admission_flags(), "--repair-policy-file", str(policy)]

    assert _prepare(project, *flags) == EXIT_REFUSED
    assert "plan          not written" in capsys.readouterr().err
    assert not _plan_path(project).exists()


@pytest.mark.parametrize(
    "target",
    ["task", "project", "directory", "text", "json-list", "json-object", "binary"],
)
def test_plan_out_refuses_to_replace_a_file_that_is_not_a_plan(
    target: str, cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-out`` replaces its target whole, so a slip (``--plan-out task.json``) would
    destroy the file it names: anything but an older plan is refused before a report prints."""
    project = cli_project
    other = project.tmp_path / "other"
    paths = {
        "task": project.task_file,
        "project": project.project_file,
        "directory": other,
        "text": other.with_suffix(".txt"),
        "json-list": other.with_suffix(".list.json"),
        "json-object": other.with_suffix(".object.json"),
        "binary": other.with_suffix(".bin"),
    }
    path = paths[target]
    if target == "directory":
        path.mkdir()
    elif target == "text":
        path.write_text("notes, not a plan\n", encoding="utf-8")
    elif target == "json-list":
        path.write_text('[{"kind": "hflow-plan"}]', encoding="utf-8")
    elif target == "json-object":
        path.write_text('{"kind": "hflow-planned", "schema_version": 1}', encoding="utf-8")
    elif target == "binary":
        path.write_bytes(b"\xff\xfe\x00not utf-8")
    before = None if path.is_dir() else path.read_bytes()

    assert _prepare(project, plan=path) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == "", "refused before anything was resolved or printed"
    assert "--plan-out" in captured.err and "only ever replaces an older plan" in captured.err
    assert "Nothing was written" in captured.err
    if before is None:
        assert path.is_dir() and list(path.iterdir()) == []
    else:
        assert path.read_bytes() == before
    assert list(path.parent.glob(f".{path.name}.*")) == [], "no temporary file left behind"


def test_plan_out_refuses_to_replace_an_authorization_and_replaces_an_older_plan(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    plan_file = _plan_path(project)
    assert _prepare(project) == EXIT_OK
    assert _approve(monkeypatch, plan_file, _typed(plan_file)) == EXIT_OK
    capsys.readouterr()
    [approval] = _authorizations(plan_file.parent)
    before = approval.read_bytes()

    assert _prepare(project, plan=approval) == EXIT_REFUSED
    assert "is not an HFlow plan" in capsys.readouterr().err
    assert approval.read_bytes() == before
    load_authorization(approval)  # still the approval it was

    # An older plan - here one this build cannot even load - is what re-preparing replaces.
    plan_file.write_text('{"kind": "hflow-plan", "schema_version": 0}', encoding="utf-8")
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    assert load_plan(plan_file).schema_version == 1


def test_a_plan_cannot_be_loaded_or_used_as_an_authorization(
    cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan = _plan_path(project)

    with pytest.raises(RefusedError) as refused:
        load_authorization(plan)
    assert refused.value.code is RefusalCode.INVALID_SPEC
    assert "is not a valid authorization" in str(refused.value)

    for argv in (
        ["run", "--plan", str(plan), "--authorization-file", str(plan)],
        ["run", *project.admission_flags(), "--authorization-file", str(plan)],
    ):
        assert main([*argv, "--json", "--data-dir", str(project.data_dir)]) == EXIT_REFUSED
        assert "is not a valid authorization" in capsys.readouterr().err
    assert not database_path(project.data_dir).exists()


def test_an_explicit_absolute_data_dir_binds_what_the_default_resolution_binds(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plan made without --data-dir records the default as an absolute path, and that path
    resolves to the same binding - the root's ledger_path included - as the default did."""
    project = cli_project
    monkeypatch.chdir(project.tmp_path)
    monkeypatch.setenv("HFLOW_DATA_DIR", "relative-data")  # relative on purpose

    assert main(["prepare", *project.admission_flags(), "--json"]) == EXIT_OK
    default = json.loads(capsys.readouterr().out)
    explicit_dir = os.path.abspath("relative-data")
    argv = ["prepare", *project.admission_flags(), "--json", "--data-dir", explicit_dir]
    assert main(argv) == EXIT_OK
    explicit = json.loads(capsys.readouterr().out)

    assert explicit["authorization"]["binding_digest"] == default["authorization"]["binding_digest"]
    ledger = default["authorization"]["binding"]["root_budget"]["ledger_path"]
    assert explicit["authorization"]["binding"]["root_budget"]["ledger_path"] == ledger
    assert ledger == str(database_path(Path(explicit_dir)).resolve())

    plan_file = _plan_path(project)
    assert main(["prepare", *project.admission_flags(), "--plan-out", str(plan_file)]) == EXIT_OK
    capsys.readouterr()
    plan = load_plan(plan_file)
    assert plan.arguments.data_dir == explicit_dir
    assert plan.binding_digest == default["authorization"]["binding_digest"]


def test_frozen_arguments_name_the_selected_profile_and_absolute_paths(tmp_path: Path) -> None:
    given = PlanArguments(
        task="task.json", project_root=".", data_dir="data", root_budget_file="root.json",
    )
    frozen = frozen_arguments(given, env={"HFLOW_PROFILE": " dsh-local "})
    assert frozen.profile == "dsh-local", "the profile HFLOW_PROFILE selected is recorded"
    assert frozen.task == os.path.abspath("task.json")
    assert frozen.project_root == os.path.abspath(".")
    assert frozen.data_dir == os.path.abspath("data")
    assert frozen.root_budget_file == os.path.abspath("root.json")
    assert frozen.project is None and frozen.repair_policy_file is None
    assert frozen_arguments(given, env={}).profile is None


def test_the_default_authorization_path_carries_the_binding_digest(tmp_path: Path) -> None:
    digest = "sha256:" + "0123456789ab" + "f" * 52
    assert default_authorization_path(tmp_path / "task.plan.json", digest) == (
        tmp_path / "task.0123456789ab.authorization.json"
    )
    assert default_authorization_path(tmp_path / "plan.json", digest).name == (
        "plan.0123456789ab.authorization.json"
    )


# --------------------------------------------------------------------------
# approve
# --------------------------------------------------------------------------


def test_approve_writes_an_authorization_from_the_terminal_and_never_overwrites_it(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    plan = load_plan(plan_file)

    assert _approve(monkeypatch, plan_file, _typed(plan_file, f"  {APPROVAL_TEXT}  ")) == EXIT_OK
    captured = capsys.readouterr()
    written = default_authorization_path(plan_file, plan.binding_digest)
    assert _authorizations(plan_file.parent) == [written]
    record = load_authorization(written)
    assert record.user_text == APPROVAL_TEXT
    assert record.provided_by == "user" and record.origin == "user_artifact"
    assert record.authorization_id.startswith("AUTH-")
    assert record.binding_digest() == plan.binding_digest
    assert record.max_top_level_submissions == 2  # implementer + reviewer
    assert record.root_limits == plan.root_limits
    assert "approving     binding " + plan.binding_digest in captured.err
    # The default file is the one `run --plan` looks up by itself, so it is not named.
    assert f"next: hflow run --plan {_shell_word(str(plan_file))}\n" in captured.out
    assert "--authorization-file" not in captured.out
    assert not database_path(project.data_dir).exists(), "approve opened no ledger"

    before = written.read_bytes()
    assert _approve(monkeypatch, plan_file, _UnreadableInput()) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert "never overwritten" in captured.err and captured.out == ""
    # The approval already there is named with the run that would use it.
    assert f"next: hflow run --plan {_shell_word(str(plan_file))}  # with the existing" in (
        captured.err
    )
    assert written.read_bytes() == before
    other = project.tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    assert _approve(monkeypatch, plan_file, _UnreadableInput(), "--out", str(other)) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "never overwritten" in err
    assert f"--authorization-file {_shell_word(str(other))}" in err
    assert other.read_text(encoding="utf-8") == "{}"


def test_the_approval_code_is_the_first_8_hex_of_the_binding_digest() -> None:
    assert approval_code("sha256:" + "0123ABCDef" + "f" * 54) == "0123abcd"


@pytest.mark.parametrize("stdin_kind", ["pipe", "none"])
def test_approve_refuses_input_that_is_not_a_terminal_without_reading_it(
    stdin_kind: str,
    cli_project: CliProject,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``echo ok | hflow approve`` approves nothing: piped or redirected input is never read, and
    neither the summary nor the approval code is shown to it."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    code = approval_code(load_plan(plan_file).binding_digest)

    for extra in ([], ["--json"]):
        stdin = _UnreadableInput(terminal=False) if stdin_kind == "pipe" else None
        monkeypatch.setattr(sys, "stdin", stdin)
        assert main(["approve", "--plan", str(plan_file), *extra]) == EXIT_REFUSED
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "for a person at a terminal" in captured.err
        assert "nothing was read and nothing was approved or written" in captured.err
        assert "by hand" in captured.err and "docs/operations.md" in captured.err
        assert code not in captured.err and "approving     binding" not in captured.err
    assert _authorizations(plan_file.parent) == []


@pytest.mark.parametrize("typed", ["", "no", "yes", "y", "ok", "approve", "WRONG", "short"])
def test_approve_refuses_a_wrong_code_before_reading_the_approval(
    typed: str,
    cli_project: CliProject,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reflexive answer - ``no`` included - is not the code, so it approves nothing; the
    approval line after it is never read."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    code = approval_code(load_plan(plan_file).binding_digest)
    wrong = code[:-1] if typed == "short" else typed
    assert wrong.strip().lower() != code

    stdin = _TerminalInput(f"{wrong}\n{APPROVAL_TEXT}\n")
    assert _approve(monkeypatch, plan_file, stdin) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert "approval code did not match" in captured.err
    assert "nothing was approved and nothing was written" in captured.err
    assert "your own words" not in captured.err, "the second prompt was never shown"
    assert stdin.read() == f"{APPROVAL_TEXT}\n", "the approval line was never read"
    assert captured.out == ""
    assert _authorizations(plan_file.parent) == []


def test_a_no_typed_as_the_approval_still_needed_the_code(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``no`` / ``no`` approves nothing. Only after the code was typed back is the second line
    the person's own words, kept as typed."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)

    assert _approve(monkeypatch, plan_file, _TerminalInput("no\nno\n")) == EXIT_REFUSED
    assert "did not match" in capsys.readouterr().err
    assert _authorizations(plan_file.parent) == []

    # The code is compared whitespace-trimmed and case-insensitively.
    code = approval_code(load_plan(plan_file).binding_digest)
    assert _approve(monkeypatch, plan_file, _TerminalInput(f"  {code.upper()}\t\nno\n")) == EXIT_OK
    capsys.readouterr()
    [written] = _authorizations(plan_file.parent)
    assert load_authorization(written).user_text == "no"


def test_approve_puts_summary_code_and_prompts_on_stderr_and_the_result_on_stdout(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``approve --plan p > log.txt`` must not hide what is approved while still asking."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    plan = load_plan(plan_file)
    code = approval_code(plan.binding_digest)

    assert _approve(monkeypatch, plan_file, _typed(plan_file)) == EXIT_OK
    captured = capsys.readouterr()
    written = default_authorization_path(plan_file, plan.binding_digest)
    record = load_authorization(written)
    assert captured.out.splitlines() == [
        f"authorization {written}",
        f"id            {record.authorization_id}",
        f"next: hflow run --plan {_shell_word(str(plan_file))}",
    ]
    err = captured.err
    summary = err.index("approving     binding " + plan.binding_digest)
    shown = err.index(f"approval code {code}")
    first = err.index("Type the approval code shown above")
    second = err.index("Approve this run in your own words")
    assert plan.prepare_report.effective_config_digest in err[:summary], "the prepare summary"
    assert summary < shown < first < second


def test_approve_json_prints_only_the_payload_on_stdout(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    out_file = project.tmp_path / "approval.json"
    plan_file = _plan_path(project)

    stdin = _typed(plan_file, "yes, this one task\nnot this third line")
    code = _approve(monkeypatch, plan_file, stdin, "--out", str(out_file), "--json")
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "approval code" in captured.err
    assert "your own words" in captured.err and "empty cancels" in captured.err
    payload = json.loads(captured.out)  # --json: the payload alone
    assert payload["authorization_file"] == str(out_file)
    assert load_authorization(out_file).user_text == "yes, this one task"
    # Not the default file, so `run --plan` would not find it: the next command names it.
    assert payload["next"] == (
        f"hflow run --plan {_shell_word(str(plan_file))} "
        f"--authorization-file {_shell_word(str(out_file))}"
    )


@pytest.mark.parametrize("text", ["", "   \t", None])
def test_approve_refuses_empty_approval_text_and_writes_nothing(
    text: str | None,
    cli_project: CliProject,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    code = approval_code(load_plan(plan_file).binding_digest)
    # None: the input ends right after the code.
    stdin = _TerminalInput(f"{code}\n") if text is None else _typed(plan_file, text)

    assert _approve(monkeypatch, plan_file, stdin) == EXIT_REFUSED
    assert "no approval text" in capsys.readouterr().err
    assert _authorizations(plan_file.parent) == []


def test_approval_text_and_code_are_never_taken_from_the_command_line(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subcommands = build_parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    dests = {action.dest for action in subcommands["approve"]._actions}
    # The resolution inputs `prepare` takes (for --task) and file paths: none of them is a way
    # to pass the approval text or the code.
    resolution = {dest for dest, _flag in RESOLUTION_FLAGS}
    assert dests == {"help", "plan", "plan_out", "out", "run", "json", "data_dir", *resolution}
    run_flag = next(a for a in subcommands["approve"]._actions if a.dest == "run")
    assert run_flag.nargs == 0, "--run takes no value: it is not a way to pass an approval"

    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    code = approval_code(load_plan(plan_file).binding_digest)
    for words in (
        [APPROVAL_TEXT],
        [code],
        [code, APPROVAL_TEXT],
        ["--code", code],
        ["--text", APPROVAL_TEXT],
        ["--run", code],
        ["--run", "--yes"],
        ["--run=yes"],
    ):
        monkeypatch.setattr(sys, "stdin", _UnreadableInput())
        with pytest.raises(SystemExit) as usage:
            main(["approve", "--plan", str(plan_file), *words])
        assert usage.value.code == EXIT_USAGE
    capsys.readouterr()
    assert _authorizations(plan_file.parent) == []


def test_approve_refuses_a_plan_whose_task_changed(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    task = json.loads(project.task_file.read_text(encoding="utf-8"))
    task["goal"] = "Set value to 3 in app.txt"
    project.task_file.write_text(json.dumps(task), encoding="utf-8")

    assert _approve(monkeypatch, _plan_path(project), _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "is stale" in err and "spec_digest" in err and "hflow prepare" in err
    assert _authorizations(_plan_path(project).parent) == []


def test_approve_refuses_a_plan_whose_base_ref_moved(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A task based on ``HEAD`` keeps the ref; HEAD moving after prepare makes the plan stale."""
    project = cli_project
    task = json.loads(project.task_file.read_text(encoding="utf-8"))
    task["workspace"]["base_commit"] = "HEAD"
    project.task_file.write_text(json.dumps(task), encoding="utf-8")
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    (project.repo / "later.txt").write_text("later\n", encoding="utf-8")
    git(project.repo, "add", "later.txt")
    git(project.repo, "commit", "-q", "-m", "HEAD moves after prepare")

    assert _approve(monkeypatch, _plan_path(project), _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "is stale" in err and "base_commit" in err and project.base in err
    assert _authorizations(_plan_path(project).parent) == []


def test_approve_refuses_a_plan_whose_root_limits_changed(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    project.root_file.write_text(
        json.dumps({"limits": {**ROOT_LIMITS, "max_top_level_submissions": 8}}), encoding="utf-8"
    )

    assert _approve(monkeypatch, _plan_path(project), _UnreadableInput()) == EXIT_REFUSED
    assert "root_limits" in capsys.readouterr().err
    assert _authorizations(_plan_path(project).parent) == []


def test_approve_on_an_offline_plan_writes_nothing(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    err = capsys.readouterr().err
    assert "needs no authorization" in err
    assert f"next: hflow run --plan {_shell_word(str(_plan_path(project)))}" in err
    assert load_plan(_plan_path(project)).prepare_report.authorization.required is False

    assert _approve(monkeypatch, _plan_path(project), _UnreadableInput()) == EXIT_OK
    out = capsys.readouterr().out
    assert "authorization not needed" in out
    assert f"next: hflow run --plan {_shell_word(str(_plan_path(project)))}" in out
    assert _authorizations(_plan_path(project).parent) == []


def test_run_text_output_ends_with_the_next_command_and_never_a_rerun(
    cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    capsys.readouterr()

    assert main(
        ["run", "--plan", str(_plan_path(project)), "--fake-write-plan", str(project.plan_file)]
    ) == EXIT_OK
    steps = [line for line in capsys.readouterr().out.splitlines() if line.startswith("next: ")]

    assert steps, "a finished run names what comes next"
    assert all(" run " not in step and " approve " not in step for step in steps)


# --------------------------------------------------------------------------
# run --plan
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--project-root", "."],
        ["--profile", "dsh-local"],
        ["--driver", "fake"],
        ["--allow-writes"],
        ["--root-budget-file", "root.json"],
        ["--repair-policy-file", "policy.json"],
        ["--project", "project.json"],
    ],
)
def test_run_plan_refuses_a_resolution_flag(
    extra: list[str], cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()

    code = main(
        ["run", "--plan", str(_plan_path(project)), *extra, "--data-dir", str(project.data_dir)]
    )
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "comes from the plan" in err
    plan = load_plan(_plan_path(project))
    assert prepare_command(plan.arguments, _plan_path(project)) in err, err
    assert not database_path(project.data_dir).exists()


def test_run_plan_usage_errors_for_task_and_data_dir(
    cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan = str(_plan_path(project))

    with pytest.raises(SystemExit) as both:
        main(["run", "--plan", plan, "--task", str(project.task_file)])
    assert both.value.code == EXIT_USAGE
    with pytest.raises(SystemExit) as neither:
        main(["run", "--json"])
    assert neither.value.code == EXIT_USAGE
    capsys.readouterr()

    other = project.tmp_path / "other-data"
    assert main(["--data-dir", str(other), "run", "--plan", plan]) == EXIT_USAGE
    assert "the plan's" in capsys.readouterr().err
    assert not other.exists()


def test_run_plan_refuses_a_stale_offline_plan(
    cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline plan needs no approval, and still runs only what it planned."""
    project = cli_project
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    capsys.readouterr()
    contract = json.loads(project.project_file.read_text(encoding="utf-8"))
    contract["checks"][0]["timeout_seconds"] = 60
    project.project_file.write_text(json.dumps(contract), encoding="utf-8")

    code = _run_plan(project)
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "is stale" in err and "project_contract_digest" in err
    # ...and ends with the command that prepares it again, flags and --data-dir included.
    command = prepare_command(load_plan(_plan_path(project)).arguments, _plan_path(project))
    assert err.rstrip().endswith(command), err
    assert "--data-dir" in command and "--driver fake" in command
    assert not database_path(project.data_dir).exists(), "refused before the ledger was opened"


def test_run_plan_refuses_a_stale_approved_plan(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    assert _approve(monkeypatch, _plan_path(project), _typed(_plan_path(project))) == EXIT_OK
    capsys.readouterr()
    task = json.loads(project.task_file.read_text(encoding="utf-8"))
    task["goal"] = "Something else entirely"
    project.task_file.write_text(json.dumps(task), encoding="utf-8")

    code = _run_plan(project)
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "is stale" in err and "hflow approve --plan" in err
    assert not database_path(project.data_dir).exists()


def test_run_plan_refuses_an_approval_issued_for_another_plan(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    other_plan = project.tmp_path / "inputs" / "other.plan.json"
    other_task = project.tmp_path / "inputs" / "other.task.json"
    task = json.loads(project.task_file.read_text(encoding="utf-8"))
    task["goal"] = "Another goal, approved for another plan"
    other_task.write_text(json.dumps(task), encoding="utf-8")
    flags = project.admission_flags()
    flags[flags.index("--task") + 1] = str(other_task)
    assert _prepare(project, *flags, plan=other_plan) == EXIT_OK
    other_approval = project.tmp_path / "other-approval.json"
    stdin = _typed(other_plan)
    assert _approve(monkeypatch, other_plan, stdin, "--out", str(other_approval)) == EXIT_OK
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()

    code = main(
        [
            "run", "--plan", str(_plan_path(project)),
            "--authorization-file", str(other_approval),
            "--fake-write-plan", str(project.plan_file), "--json",
        ]
    )
    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "the authorization does not cover this run" in err and "spec_digest" in err, err
    assert "was not issued for plan" in err


# --------------------------------------------------------------------------
# approve --run
# --------------------------------------------------------------------------


def _scripted_change(monkeypatch: pytest.MonkeyPatch, project: CliProject) -> None:
    """The offline driver writes the task's change, as ``--fake-write-plan`` would make it.

    ``approve --run`` takes no run flag - it runs exactly ``run --plan`` - so the offline change is
    scripted into the driver here instead of being passed on the command line.
    """
    writes = json.loads(project.plan_file.read_text(encoding="utf-8"))
    original = cli.FakeScript

    def scripted(*args: object, **kwargs: object) -> object:
        script = original(*args, **kwargs)  # type: ignore[arg-type]
        script.write_plan = dict(writes)
        return script

    monkeypatch.setattr(cli, "FakeScript", scripted)


def _status_json(project: CliProject, run_id: str, capsys: pytest.CaptureFixture[str]) -> dict:
    argv = ["status", run_id, "--project-root", str(project.repo), "--json"]
    assert main([*argv, "--data-dir", str(project.data_dir)]) == EXIT_OK
    return json.loads(capsys.readouterr().out)


def test_approve_run_approves_then_runs_the_plan_in_the_same_process(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The approval exactly as without ``--run``, then exactly ``run --plan`` with that file: the
    run's outcome, its ``next:`` lines and its exit code."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    plan = load_plan(plan_file)
    _scripted_change(monkeypatch, project)

    assert _approve(monkeypatch, plan_file, _typed(plan_file), "--run") == EXIT_OK
    captured = capsys.readouterr()
    written = default_authorization_path(plan_file, plan.binding_digest)
    record = load_authorization(written)
    assert record.user_text == APPROVAL_TEXT and record.origin == "user_artifact"
    err = captured.err
    assert err.index(f"approval code {approval_code(plan.binding_digest)}") < err.index(
        "Approve this run in your own words"
    )
    lines = captured.out.splitlines()
    assert lines[:3] == [
        f"authorization {written}",
        f"id            {record.authorization_id}",
        f"running       hflow run --plan {_shell_word(str(plan_file))} "
        f"--authorization-file {_shell_word(str(written))}",
    ]
    assert lines[3].startswith("run        ")
    run_id = lines[3].split()[1]
    assert lines[4] == "state      ACCEPTED"

    # The same next: lines `run --plan` prints for this run - never a re-run or a re-approval.
    store = Store(database_path(project.data_dir))
    try:
        expected = next_lines(next_steps(store, run_id, project_file=plan.arguments.project))
    finally:
        store.close()
    assert expected and lines[-len(expected):] == expected
    assert all(" run " not in line and " approve " not in line for line in expected)

    status = _status_json(project, run_id, capsys)
    assert {item["authorization_id"] for item in status["invocations"]} == {record.authorization_id}


@pytest.mark.parametrize("case", ["pipe", "wrong-code", "empty-text", "stale", "file-exists"])
def test_approve_run_runs_nothing_when_the_approval_fails(
    case: str,
    cli_project: CliProject,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    plan = load_plan(plan_file)
    code = approval_code(plan.binding_digest)
    existing: list[Path] = []
    if case == "stale":
        task = json.loads(project.task_file.read_text(encoding="utf-8"))
        task["goal"] = "Set value to 3 in app.txt"
        project.task_file.write_text(json.dumps(task), encoding="utf-8")
    if case == "file-exists":
        existing = [default_authorization_path(plan_file, plan.binding_digest)]
        existing[0].write_text("{}", encoding="utf-8")
    stdin = {
        "pipe": _UnreadableInput(terminal=False),
        "wrong-code": _TerminalInput(f"no\n{APPROVAL_TEXT}\n"),
        "empty-text": _TerminalInput(f"{code}\n\n"),
        "stale": _UnreadableInput(),
        "file-exists": _UnreadableInput(),
    }[case]

    assert _approve(monkeypatch, plan_file, stdin, "--run") == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == "", "no approval result and no run output"
    assert "running" not in captured.err
    assert _authorizations(plan_file.parent) == existing
    assert not database_path(project.data_dir).exists(), "nothing ran: no ledger was opened"


def test_approve_run_interrupted_exits_130_and_keeps_the_approval(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Ctrl+C inside the run reaches the top level exactly as it does for ``run --plan``."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)

    def interrupted(self: object, request: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.Controller, "run_task", interrupted)
    assert _approve(monkeypatch, plan_file, _typed(plan_file), "--run") == EXIT_INTERRUPTED
    captured = capsys.readouterr()
    assert "interrupted: this command stopped where it was" in captured.err
    assert "running       hflow run --plan" in captured.out
    [written] = _authorizations(plan_file.parent)
    assert load_authorization(written).user_text == APPROVAL_TEXT


def test_approve_run_json_prints_two_lines_and_exits_with_the_run(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With ``--out`` the run is handed that file; without a scripted change the offline run
    ends however ``run --plan`` would end it, and that is ``approve``'s exit code."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    out_file = project.tmp_path / "approval.json"

    stdin = _typed(plan_file)
    code = _approve(monkeypatch, plan_file, stdin, "--run", "--out", str(out_file), "--json")
    captured = capsys.readouterr()
    approval_line, run_line = captured.out.splitlines()
    approval = json.loads(approval_line)
    outcome = json.loads(run_line)
    assert approval["authorization_file"] == str(out_file)
    assert "next" not in approval
    assert approval["running"] == (
        f"hflow run --plan {_shell_word(str(plan_file))} "
        f"--authorization-file {_shell_word(str(out_file))}"
    )
    # No change was made, so the run does not end accepted - and approve exits with its code.
    assert outcome["task_state"] != "ACCEPTED", outcome
    assert code == _STATE_EXIT_CODES[TaskState(outcome["task_state"])] != EXIT_OK
    status = _status_json(project, outcome["run_id"], capsys)
    record = load_authorization(out_file)
    assert {item["authorization_id"] for item in status["invocations"]} == {record.authorization_id}


def test_approve_run_on_an_offline_plan_runs_what_run_plan_runs(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline plan needs no approval, so nothing is asked - as for ``run --plan`` itself."""
    project = cli_project
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    capsys.readouterr()
    plan_file = _plan_path(project)
    _scripted_change(monkeypatch, project)

    stdin = _UnreadableInput(terminal=False)
    assert _approve(monkeypatch, plan_file, stdin, "--run") == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("authorization not needed: ")
    assert lines[1] == f"running       hflow run --plan {_shell_word(str(plan_file))}"
    assert "state      ACCEPTED" in lines
    assert _authorizations(plan_file.parent) == []


def test_the_resolution_flags_are_exactly_the_plan_arguments() -> None:
    """``_resolution_arguments`` is derived from ``RESOLUTION_FLAGS``; both cover every input a
    plan records, the data directory aside (it is ``--data-dir``, before or after the verb)."""
    dests = [dest for dest, _flag in RESOLUTION_FLAGS]
    assert len(dests) == len(set(dests))
    # ``authorization_mode`` has no flag (it has one value); it stays so an older plan that
    # recorded ``stop-trial`` still loads.
    assert {*dests, "data_dir", "authorization_mode"} == set(PlanArguments.model_fields)


def test_prepare_records_the_write_opt_in_and_the_binding_covers_it(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
    without = project.tmp_path / "inputs" / "without.plan.json"
    assert _prepare(project, *project.admission_flags(), plan=without) == EXIT_OK
    assert _prepare(project, *project.admission_flags(), "--allow-writes") == EXIT_OK
    capsys.readouterr()
    plain, writing = load_plan(without), load_plan(_plan_path(project))
    assert (plain.arguments.allow_writes, writing.arguments.allow_writes) == (False, True)
    assert plain.prepare_report.effective_config.implementer_writes is False
    assert writing.prepare_report.effective_config.implementer_writes is True
    assert plain.binding_digest != writing.binding_digest, "an approval covers the opt-in"
    assert " --allow-writes " in prepare_command(writing.arguments, _plan_path(project))
    assert "--allow-writes" not in prepare_command(plain.arguments, without)


@pytest.mark.parametrize(
    ("at_prepare", "at_run"), [("1", None), (None, "1")], ids=["set-then-unset", "unset-then-set"]
)
def test_run_plan_uses_the_recorded_write_opt_in_never_the_shell(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    at_prepare: str | None, at_run: str | None,
) -> None:
    project = cli_project

    def shell(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
        else:
            monkeypatch.setenv("HFLOW_ALLOW_WRITES", value)

    shell(at_prepare)
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    capsys.readouterr()
    recorded = load_plan(_plan_path(project))
    assert recorded.arguments.allow_writes is (at_prepare == "1"), "prepare falls back to it"

    shell(at_run)
    assert _run_plan(project) == EXIT_OK, capsys.readouterr().err
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    assert main(["status", run_id, "--json", "--data-dir", str(project.data_dir)]) == EXIT_OK
    stored = json.loads(capsys.readouterr().out)["effective_config"]
    assert stored["implementer_writes"] is (at_prepare == "1"), "the plan's value ran"


@pytest.mark.parametrize(
    ("extra", "env", "expected"),
    [([], None, False), ([], "1", True), (["--allow-writes"], None, True),
     (["--allow-writes"], "false", True)],
    ids=["neither", "variable", "flag", "flag-beats-a-false-variable"],
)
def test_prepare_and_run_task_take_the_flag_or_fall_back_to_the_variable(
    extra: list[str], env: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is None:
        monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("HFLOW_ALLOW_WRITES", env)
    for verb in ("prepare", "run"):
        args = cli.build_parser().parse_args([verb, "--task", "task.json", *extra])
        assert cli._resolution_arguments(args).allow_writes is expected, verb


def test_run_task_honours_the_write_variable_as_a_fallback(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "1")
    argv = [
        "run", *_offline_flags(project), "--fake-write-plan", str(project.plan_file),
        "--json", "--data-dir", str(project.data_dir),
    ]
    assert main(argv) == EXIT_OK, capsys.readouterr().err
    run_id = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["run_id"]
    assert main(["status", run_id, "--json", "--data-dir", str(project.data_dir)]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["effective_config"]["implementer_writes"] is True


def test_a_plan_from_before_allow_writes_keeps_the_permission_it_recorded(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``allow_writes`` in the file: the plan's own recorded write permission - which its
    binding, and any approval of it, already cover - never the environment."""
    project = cli_project
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "1")
    assert _prepare(project, *_offline_flags(project)) == EXIT_OK
    capsys.readouterr()
    raw = json.loads(_plan_path(project).read_text(encoding="utf-8"))
    del raw["arguments"]["allow_writes"]
    _plan_path(project).write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.delenv("HFLOW_ALLOW_WRITES")

    assert load_plan(_plan_path(project)).arguments.allow_writes is True
    assert _run_plan(project) == EXIT_OK, capsys.readouterr().err


@pytest.mark.parametrize(("key", "value", "flag"), [
    ("base_commit", "HEAD", "--base-commit"),
    ("workspace", "in_place", "--workspace"),
])
def test_a_plan_that_set_a_retired_override_is_refused_with_the_re_prepare_command(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    key: str, value: str, flag: str,
) -> None:
    """An older build recorded ``--base-commit`` / ``--workspace``; this one has neither, so such
    a plan is refused, never run with the override silently gone - and never a traceback."""
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    raw = json.loads(_plan_path(project).read_text(encoding="utf-8"))
    raw["arguments"][key] = value
    _plan_path(project).write_text(json.dumps(raw), encoding="utf-8")

    assert _approve(monkeypatch, _plan_path(project), _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert f"was prepared with {flag}, which this build no longer has" in err, err
    assert "re-prepare the plan: hflow prepare --task " in err and flag not in err.split(
        "re-prepare the plan:"
    )[1]
    assert _run_plan(project) == EXIT_REFUSED
    assert "which this build no longer has" in capsys.readouterr().err
    assert not database_path(project.data_dir).exists()

    # The same keys left null, as an older build wrote them when not given: the plan loads.
    raw["arguments"][key] = None
    raw["arguments"]["base_commit" if key == "workspace" else "workspace"] = None
    _plan_path(project).write_text(json.dumps(raw), encoding="utf-8")
    assert load_plan(_plan_path(project)).arguments.task == raw["arguments"]["task"]


# --------------------------------------------------------------------------
# a real-driver plan (stand-in client; nothing is dispatched)
# --------------------------------------------------------------------------


@pytest.fixture()
def live_plan(
    tmp_path: Path,
    live_project,
    worktree_task,
    project_root: Path,
    live_profile,
    acpx_client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Path:
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)
    plan = tmp_path / "live.plan.json"
    code = main(
        [
            "prepare", "--task", str(task_file), "--project", str(project_file),
            "--project-root", str(project_root), "--profile", "dsh-local",
            "--plan-out", str(plan), "--data-dir", str(data_dir),
        ]
    )
    assert code == EXIT_OK, capsys.readouterr()
    capsys.readouterr()
    return plan


def test_a_real_driver_plan_is_approved_into_an_artifact_the_run_check_accepts(
    live_plan: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = load_plan(live_plan)
    assert plan.prepare_report.driver_mode == "live"
    assert plan.binding["driver"] == "acpx-dsh" and plan.binding["profile_id"] == "dsh-local"
    approval = live_plan.parent / "elsewhere.authorization.json"

    stdin = _typed(live_plan, "Run it once.")
    assert _approve(monkeypatch, live_plan, stdin, "--out", str(approval)) == EXIT_OK
    capsys.readouterr()
    record = load_authorization(approval)
    resolution = resolve_arguments(plan.arguments)
    verify_authorization(
        record,
        expected=pending_binding(
            resolution.resolved,
            authorization_mode=plan.arguments.authorization_mode,
            root_binding=root_binding_for(resolution.resolved) if resolution.root_plan else None,
        ),
    )
    assert record.binding.effective_config_digest == plan.prepare_report.effective_config_digest
    assert record.binding.project_contract_digest, "the contract is part of what was approved"

    # The approval is not at the default path, so `run --plan` alone finds none and refuses
    # before reading the task, pointing at `approve`.
    assert main(["run", "--plan", str(live_plan), "--json"]) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["reason"] == "live_authorization_missing"
    assert "hflow approve --plan" in payload["detail"]
    assert not database_path(Path(plan.arguments.data_dir)).exists()


def test_a_stale_plan_names_the_changed_configuration_fields_and_their_variables(
    live_plan: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shell with other environment values resolves another configuration: the refusal names
    the fields and the variables they are read from, not only two digests."""
    plan = load_plan(live_plan)
    assert _approve(monkeypatch, live_plan, _typed(live_plan)) == EXIT_OK  # approved as planned
    capsys.readouterr()
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "false")
    other_home = tmp_path / "another-dsh-home"
    monkeypatch.setenv("DSH_HOME", str(other_home))

    for argv in (["approve", "--plan", str(live_plan)], ["run", "--plan", str(live_plan)]):
        monkeypatch.setattr(sys, "stdin", _UnreadableInput())
        assert main(argv) == EXIT_REFUSED
        err = capsys.readouterr().err
        assert "is stale" in err and "Nothing was written or dispatched" in err
        assert "effective_config_digest: planned " in err
        assert plan.prepare_report.effective_config_digest in err
        # The write opt-in is the plan's own (it recorded it): the variable changes nothing.
        assert "implementer_writes" not in err
        for role in ("implementer", "reviewer"):
            assert f"effective_config.roles[{role}].launch.dsh_home: planned " in err
        assert f"!= now {str(other_home)!r} (read from the environment: DSH_HOME)" in err
        assert "effective_config.profile_id" not in err, "the plan names its profile"
        # The printed re-prepare carries the recorded opt-in as the flag.
        assert " --allow-writes --plan-out " in err
    assert not database_path(Path(plan.arguments.data_dir)).exists()


def test_approve_never_reads_the_write_variable_a_plan_recorded(
    live_plan: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The trap a recorded opt-in closes: a plan prepared where HFLOW_ALLOW_WRITES was set is
    approved in a shell without it - and is current, not stale."""
    plan = load_plan(live_plan)
    assert plan.arguments.allow_writes is True, "prepare recorded the variable's opt-in"
    assert plan.prepare_report.effective_config.implementer_writes is True
    monkeypatch.delenv("HFLOW_ALLOW_WRITES", raising=False)
    assert _approve(monkeypatch, live_plan, _typed(live_plan)) == EXIT_OK, capsys.readouterr()
    assert len(_authorizations(live_plan.parent)) == 1


def test_approve_refuses_a_current_plan_the_run_would_refuse(
    live_plan: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Credential presence is a dispatch precondition, not part of the binding: the plan is
    current, and still nothing is approved for a run that would refuse."""
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    assert _approve(monkeypatch, live_plan, _UnreadableInput()) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == "", "the issues are the summary, on the summary's stream"
    assert "no_credential_source" in captured.err
    assert "would refuse this plan's task" in captured.err
    assert _authorizations(live_plan.parent) == []


# --------------------------------------------------------------------------
# end to end, through `python -m hflow` (the approval typed at a simulated terminal)
# --------------------------------------------------------------------------

#: ``python -m hflow`` with standard input wrapped as a terminal: what a person typing at a console
#: amounts to for ``approve``, which refuses piped input. Only the ``isatty`` answer changes.
_AT_A_TERMINAL = (
    "import io, sys\n"
    "class Terminal(io.StringIO):\n"
    "    def isatty(self):\n"
    "        return True\n"
    "sys.stdin = Terminal(sys.stdin.read())\n"
    "from hflow.cli import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)


def _hflow_with_input(
    project: CliProject, *args: str, text: str, terminal: bool = False
) -> Call:
    entry = ["-c", _AT_A_TERMINAL] if terminal else ["-m", "hflow"]
    argv = [sys.executable, *entry, *args, "--data-dir", str(project.data_dir)]
    completed = subprocess.run(  # noqa: S603 - the interpreter running this suite
        argv,
        cwd=str(project.tmp_path),
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT,
        env=hflow_env(),
        check=False,
    )
    return Call(argv, completed.returncode, completed.stdout, completed.stderr)


def test_prepare_approve_run_through_the_cli_reaches_the_hand_built_outcome(
    cli_project: CliProject,
) -> None:
    """The repository, task and root of the hand-built loop in ``test_e2e_cli_loop.py``, with
    ``prepare --plan-out`` / ``approve`` / ``run --plan`` in place of the hand-built artifact and
    the repeated flags. Piping the approval into ``python -m hflow approve`` is refused; the two
    lines are typed at a (simulated) terminal instead."""
    project = cli_project
    user_before = project.user_state()
    plan_file = _plan_path(project)

    prepared = project.hflow(
        "prepare", *project.admission_flags(), "--plan-out", str(plan_file), "--json"
    )
    assert prepared.returncode == 0, prepared
    report = prepared.json()
    assert report["authorization"]["creates_authorization"] is False
    assert f"next: hflow approve --plan {_shell_word(str(plan_file))}" in prepared.stderr
    assert not database_path(project.data_dir).exists()
    code = approval_code(report["authorization"]["binding_digest"])
    typed = f"{code}\n{APPROVAL_TEXT}\n"

    piped = _hflow_with_input(project, "approve", "--plan", str(plan_file), text=typed)
    assert piped.returncode == EXIT_REFUSED, piped
    assert "for a person at a terminal" in piped.stderr and piped.stdout == ""
    assert _authorizations(plan_file.parent) == []

    approved = _hflow_with_input(
        project, "approve", "--plan", str(plan_file), text=typed, terminal=True
    )
    assert approved.returncode == 0, approved
    assert f"approval code {code}" in approved.stderr
    approval = default_authorization_path(plan_file, report["authorization"]["binding_digest"])
    assert approval.exists(), approved
    record = load_authorization(approval)
    assert record.user_text == APPROVAL_TEXT
    assert record.binding_digest() == report["authorization"]["binding_digest"]
    assert record.root_limits is not None
    assert record.root_limits.model_dump(mode="json") == ROOT_LIMITS
    assert f"next: hflow run --plan {_shell_word(str(plan_file))}\n" in approved.stdout
    assert "--authorization-file" not in approved.stdout, "the default file is found by run"
    assert not database_path(project.data_dir).exists(), "approve wrote no ledger"

    # No resolution flag repeated, and no --authorization-file: the plan's default approval.
    ran = project.hflow(
        "run", "--plan", str(plan_file), "--fake-write-plan", str(project.plan_file), "--json"
    )
    assert ran.returncode == 0, ran
    payload = ran.json()
    assert payload["task_state"] == "ACCEPTED", payload
    assert payload["delivery_state"] == "LOCAL_CANDIDATE", payload
    assert payload["implementer_invocations"] == 1 and payload["reviewer_invocations"] == 1
    assert payload["receipt"]["candidate"]["base_commit"] == project.base
    assert payload["receipt"]["verification"]["status"] == "passed"
    status = project.status_json(payload["run_id"])
    assert {item["authorization_id"] for item in status["invocations"]} == {record.authorization_id}

    integration = project.integrate_prepare(payload["run_id"])["integration"]
    integration_id = integration["integration_id"]
    applied = project.hflow(
        "integrate", "apply", integration_id, "--expect-target", project.base, "--json"
    )
    assert applied.returncode == 0, applied
    assert applied.json()["integration"]["state"] == "integrated"
    assert git(project.repo, "show", f"{project.tip()}:app.txt") == "value = 2\n"
    assert project.user_state() == user_before


def test_run_plan_names_a_plan_the_environment_made_stale_before_asking_for_an_approval(
    cli_project: CliProject,
    live_profile,  # noqa: ANN001 - conftest's two-role DSH profile
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An offline plan that named no profile resolves to a real driver once ``HFLOW_PROFILE`` is
    set. That is a stale plan - the binding names the profile - and it is said so first: telling
    the operator to approve it would only end in the same refusal one step later."""
    project = cli_project
    flags = [
        "--task", str(project.task_file),
        "--project", str(project.project_file),
        "--project-root", str(project.repo),
    ]
    assert _prepare(project, *flags) == EXIT_OK
    capsys.readouterr()
    write_profile(project.data_dir, live_profile)
    monkeypatch.setenv("HFLOW_PROFILE", live_profile.profile_id)

    assert _run_plan(project) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "is stale" in err and "HFLOW_PROFILE" in err, err
    assert "Approve the plan first" not in err
    command = prepare_command(load_plan(_plan_path(project)).arguments, _plan_path(project))
    assert "--profile" not in command, "HFLOW_PROFILE decides again when the plan named none"
    assert err.rstrip().endswith(command), err
    assert not database_path(project.data_dir).exists()


# --------------------------------------------------------------------------
# approve --task: prepare --plan-out and approve --plan in one process
# --------------------------------------------------------------------------


def _approve_task(
    monkeypatch: pytest.MonkeyPatch, project: CliProject, stdin: io.StringIO | None, *extra: str
) -> int:
    monkeypatch.setattr(sys, "stdin", stdin)
    return main(
        ["approve", *project.admission_flags(), *extra, "--data-dir", str(project.data_dir)]
    )


def _code_for(project: CliProject, capsys: pytest.CaptureFixture[str]) -> str:
    """The approval code of the task's plan, from a two-step plan written elsewhere."""
    elsewhere = project.tmp_path / "two-step" / "task.plan.json"
    assert _prepare(project, plan=elsewhere) == EXIT_OK
    capsys.readouterr()
    return approval_code(load_plan(elsewhere).binding_digest)


def test_approve_task_writes_the_plan_and_the_same_artifact_as_the_two_step_path(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    two_step = project.tmp_path / "two-step" / "task.plan.json"
    code = _code_for(project, capsys)
    assert _approve(monkeypatch, two_step, _typed(two_step)) == EXIT_OK
    capsys.readouterr()
    [two_step_approval] = _authorizations(two_step.parent)

    default_plan = project.task_file.with_name("task.plan.json")
    assert not default_plan.exists()
    stdin = _TerminalInput(f"{code}\n{APPROVAL_TEXT}\n")
    assert _approve_task(monkeypatch, project, stdin) == EXIT_OK
    captured = capsys.readouterr()
    plan = load_plan(default_plan)
    assert f"plan          {default_plan}" in captured.err
    assert "approving     binding " + plan.binding_digest in captured.err
    written = default_authorization_path(default_plan, plan.binding_digest)
    assert _authorizations(default_plan.parent) == [written]
    one_step, step_by_step = load_authorization(written), load_authorization(two_step_approval)
    assert one_step.binding == step_by_step.binding, "the same binding as prepare + approve"
    assert one_step.binding_digest() == plan.binding_digest == load_plan(two_step).binding_digest
    assert one_step.root_limits == step_by_step.root_limits
    assert one_step.user_text == APPROVAL_TEXT and one_step.origin == "user_artifact"
    assert f"next: hflow run --plan {_shell_word(str(default_plan))}\n" in captured.out
    assert not database_path(project.data_dir).exists(), "approve opened no ledger"

    # The plan it wrote runs like any other, with the approval found at its default path.
    assert main(
        ["run", "--plan", str(default_plan), "--fake-write-plan", str(project.plan_file), "--json"]
    ) == EXIT_OK, capsys.readouterr().err


@pytest.mark.parametrize("stdin_kind", ["pipe", "none"])
def test_approve_task_refuses_input_that_is_not_a_terminal_and_writes_nothing(
    stdin_kind: str,
    cli_project: CliProject,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = cli_project
    stdin = _UnreadableInput(terminal=False) if stdin_kind == "pipe" else None
    assert _approve_task(monkeypatch, project, stdin) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "for a person at a terminal" in captured.err
    assert "nothing was read and nothing was approved or written" in captured.err
    assert "approving     binding" not in captured.err, "no summary and no code were shown"
    assert not project.task_file.with_name("task.plan.json").exists(), "no plan either"
    assert _authorizations(project.task_file.parent) == []


def test_approve_task_refuses_a_wrong_code_and_writes_no_approval(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    code = _code_for(project, capsys)
    stdin = _TerminalInput(f"{'0' if code[0] != '0' else '1'}{code[1:]}\n{APPROVAL_TEXT}\n")
    assert _approve_task(monkeypatch, project, stdin) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "the approval code did not match" in err
    assert stdin.readline() == f"{APPROVAL_TEXT}\n", "the approval line was never read"
    assert _authorizations(project.task_file.parent) == []
    # The plan is a preview, not an approval: it stays, as `prepare --plan-out` would leave it.
    assert project.task_file.with_name("task.plan.json").exists()


def test_approve_task_never_replaces_a_different_plan_unless_told_where(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    default_plan = project.task_file.with_name("task.plan.json")
    assert _prepare(project, *_offline_flags(project), plan=default_plan) == EXIT_OK
    capsys.readouterr()
    before = default_plan.read_bytes()

    assert _approve_task(monkeypatch, project, _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "already holds another plan" in err
    assert f"--plan-out {_shell_word(str(default_plan))}" in err
    assert default_plan.read_bytes() == before
    assert _authorizations(default_plan.parent) == []

    code = _code_for(project, capsys)
    stdin = _TerminalInput(f"{code}\n{APPROVAL_TEXT}\n")
    assert _approve_task(monkeypatch, project, stdin, "--plan-out", str(default_plan)) == EXIT_OK
    capsys.readouterr()
    replaced = load_plan(default_plan)
    assert replaced.arguments.root_budget_file is not None, "the plan was replaced as asked"
    assert len(_authorizations(default_plan.parent)) == 1

    # The same plan again: kept as it is, and its approval already exists.
    assert _approve_task(monkeypatch, project, _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "already this plan; kept as it is" in err and "never overwritten" in err


def test_approve_task_run_approves_then_runs_in_the_same_process(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    code = _code_for(project, capsys)
    _scripted_change(monkeypatch, project)
    stdin = _TerminalInput(f"{code}\n{APPROVAL_TEXT}\n")
    assert _approve_task(monkeypatch, project, stdin, "--run") == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    default_plan = project.task_file.with_name("task.plan.json")
    assert lines[2].startswith(f"running       hflow run --plan {_shell_word(str(default_plan))}")
    assert lines[4] == "state      ACCEPTED"


def test_approve_task_refuses_a_task_the_run_would_refuse_and_writes_nothing(
    cli_project: CliProject, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    task = json.loads(project.task_file.read_text(encoding="utf-8"))
    task["scope"]["write_allow"] = []
    project.task_file.write_text(json.dumps(task), encoding="utf-8")
    assert _approve_task(monkeypatch, project, _UnreadableInput()) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "`hflow run` would refuse this task" in err and "no plan and no approval" in err
    assert not project.task_file.with_name("task.plan.json").exists()


@pytest.mark.parametrize(
    "extra",
    [["--driver", "fake"], ["--allow-writes"], ["--plan-out", "elsewhere.plan.json"]],
    ids=["resolution-flag", "allow-writes", "plan-out"],
)
def test_approve_plan_takes_no_resolution_flag_and_no_plan_out(
    extra: list[str], cli_project: CliProject, capsys: pytest.CaptureFixture[str]
) -> None:
    project = cli_project
    assert _prepare(project) == EXIT_OK
    capsys.readouterr()
    assert main(["approve", "--plan", str(_plan_path(project)), *extra]) == EXIT_USAGE
    assert "hflow approve: error:" in capsys.readouterr().err
    assert _authorizations(_plan_path(project).parent) == []
