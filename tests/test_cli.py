"""The command surface, and the promise that status/report cost no model calls.

The CLI is exercised through ``main(argv)`` with an explicit ``--data-dir`` so tests
never touch the real user data directory.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

import hflow.drivers.acpx_dsh as acpx_dsh_module
from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import (
    EffectiveConfig,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    InvocationStartState,
    MachineProfile,
    RefusalCode,
    RoleConfig,
    RunRequest,
)
from hflow.report import report_json, status_text
from hflow.controller import Controller, inspect_run
from hflow.drivers.acpx_dsh import DRIVER_ID as ACPX_DSH_DRIVER_ID
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.drivers.fake import FakeDriver
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import write_profile, write_project, write_task

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

    # No profile was named, and doctor now says exactly that instead of reporting a driver
    # status that reads as if a binding had been selected and tested.
    assert payload["driver_status"] == "NO_PROFILE_SELECTED"
    assert payload["selected_driver"] == "none"
    assert payload["profile"]["selected"] is False
    assert payload["profile"]["usable"] is False
    assert "no machine profile selected" in payload["profile"]["detail"]
    record = payload["capability_record"]
    assert record["live_tested"] is False
    assert record["probe_only"] is True
    assert record["driver_id"] == "acpx-dsh-acp", "the M0-selected transport"
    # Cancel is unsupported on the one-shot exec path (no queue owner to reach), and the
    # read-only boundary is not enforced by anything here.
    assert record["capabilities"]["cancel"] == "unsupported"
    assert record["capabilities"]["process_boundary_teardown"] == "probed"
    assert record["capabilities"]["readonly_enforcement"] == "unsupported"
    assert record["capabilities"]["billing_usage"] == "unknown"
    # Verified and unverified capabilities are separate lists, so one cannot be read as the
    # other: the states come from the recorded table, and doctor re-states their meaning.
    states = payload["capabilities"]["states"]
    assert "process_boundary_teardown" in states["probed"]
    assert "billing_usage" in states["unknown"]
    assert states["enforced"] == []
    assert payload["capabilities"]["live_tested"] is False
    assert "not a live compatibility proof" in " ".join(payload["notes"])
    # No credential material is read or printed.
    assert ".credentials" not in json.dumps(payload)


def test_doctor_resolves_a_selected_profile_per_role(
    tmp_path: Path, profile: MachineProfile, capsys: pytest.CaptureFixture[str]
) -> None:
    """A named profile is resolved and reported, role by role, without launching anything."""
    data_dir = tmp_path / "data"
    write_profile(data_dir, profile)

    exit_code = main(["doctor", "--json", "--profile", "dsh-local", "--data-dir", str(data_dir)])
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["profile"]["selected"] is True
    assert payload["profile"]["requested_via"] == "--profile"
    assert payload["profile"]["usable"] is True
    assert payload["profile"]["profile_id"] == "dsh-local"
    assert payload["driver_status"] == "RESOLVED_FROM_PROFILE"
    assert payload["selected_driver"] == "fake"
    for role in ("implementer", "reviewer"):
        entry = payload["profile"]["roles"][role]
        assert entry["usable"] is True
        assert entry["driver"] == "fake-offline"
        assert entry["driver_id"] == "fake"


def test_doctor_shows_the_model_flag_and_keeps_model_selection_documented(
    tmp_path: Path, live_profile: MachineProfile, acpx_client, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each role's ``--model`` (or its absence) is shown, and the capability is not upgraded."""
    data_dir = tmp_path / "data"
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
    write_profile(data_dir, profile)

    exit_code = main(["doctor", "--json", "--profile", "dsh-local", "--data-dir", str(data_dir)])
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    implementer = " ".join(payload["profile"]["roles"]["implementer"]["dependencies"])
    reviewer = " ".join(payload["profile"]["roles"]["reviewer"]["dependencies"])
    assert f"--model {pair}" in implementer
    assert "no --model flag" in reviewer
    assert "documented only" in implementer
    assert "model_selection" in payload["capabilities"]["states"]["documented"]


def test_doctor_names_the_dsh_home_a_child_actually_uses(
    tmp_path: Path,
    live_profile: MachineProfile,
    acpx_client,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dsh_home`` is your shell's DSH home; ``child_dsh_home`` is the one an HFlow child gets."""
    data_dir = tmp_path / "data"
    monkeypatch.delenv("DSH_HOME", raising=False)
    assert main(["doctor", "--json", "--data-dir", str(data_dir)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    child = payload["child_dsh_home"]
    assert child["kind"] == "per_invocation"
    assert child["path"].endswith(os.path.join("invocations", "<invocation-id>", "home", ".dsh"))
    assert child["detail"].endswith("inferred from upstream source, not observed")
    assert ".credentials" not in json.dumps(payload)
    notes = " ".join(payload["notes"])
    assert "not a live compatibility proof" in notes
    assert "no .env or credential file was opened" in notes
    assert main(["doctor", "--data-dir", str(data_dir)]) == EXIT_OK
    text = capsys.readouterr().out
    assert "your dsh home" in text
    assert f"child dsh home per_invocation {child['path']}" in text

    bound = tmp_path / "bound"
    bound.mkdir()
    (bound / "cordis.patch.yml").write_text("approval: on-request\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(bound))
    write_profile(data_dir, live_profile)
    assert main(
        ["doctor", "--json", "--profile", "dsh-local", "--data-dir", str(data_dir)]
    ) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["child_dsh_home"]["kind"] == "bound"
    assert payload["child_dsh_home"]["path"] == str(bound)
    dependencies = " ".join(payload["profile"]["roles"]["implementer"]["dependencies"])
    assert "DSH home: bound" in dependencies
    assert ".credentials" not in json.dumps(payload)


def test_doctor_refuses_a_profile_it_cannot_resolve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable configuration is reported in full and exits non-zero: scriptable and readable."""
    data_dir = tmp_path / "data"
    write_profile(
        data_dir,
        MachineProfile(
            profile_id="broken",
            role_bindings={"implementer": "a", "reviewer": "a"},
            agents={"a": {"harness": "other", "driver": "some-other-harness"}},
        ),
    )

    exit_code = main(["doctor", "--json", "--profile", "broken", "--data-dir", str(data_dir)])
    assert exit_code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["usable"] is False
    assert payload["driver_status"] == "PROFILE_NOT_USABLE"
    assert "no runtime fallback" in payload["profile"]["detail"]
    assert payload["profile"]["roles"]["implementer"]["driver_id"] is None
    # The rest of the probe is still reported: doctor answers even when the config is unusable.
    assert payload["executables"]["python"]["available"] is True


def test_doctor_reports_an_unknown_profile_without_creating_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    exit_code = main(["doctor", "--json", "--profile", "ghost", "--data-dir", str(data_dir)])
    assert exit_code == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["usable"] is False
    assert "profile 'ghost' not found" in payload["profile"]["detail"]
    assert not (data_dir / "hflow.sqlite").exists()


def test_schema_command_prints_generated_contracts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "TaskSpec",
        "ProjectConfig",
        "ResultReceipt",
        "RunRequest",
        # A profile is a document a person writes by hand, so its schema is part of the
        # command surface rather than something to reverse-engineer from the loader.
        "MachineProfile",
        "EffectiveConfig",
        "PrepareReport",
        # The other documents a person writes by hand: the authorization artifact
        # (--authorization-file), the root budget plan (--root-budget-file) and the bare repair
        # policy (--repair-policy-file). Generated from the models their loaders validate with.
        "AuthorizationRecord",
        "RootBudgetPlan",
        "RepairPolicy",
        # Output a script reads back, like PrepareReport: `hflow cancel --json`.
        "CancellationReceipt",
    }
    receipt_schema = payload["ResultReceipt"]
    # Enums are referenced, not inlined; the definition must be present in the same document.
    ref = receipt_schema["properties"]["task_state"]["$ref"].rsplit("/", 1)[-1]
    assert ref in receipt_schema["$defs"]
    assert "ACCEPTED" in receipt_schema["$defs"][ref]["enum"]
    profile_schema = payload["MachineProfile"]
    assert set(profile_schema["required"]) == {"profile_id", "role_bindings", "agents"}


def test_status_text_marks_unknowns_instead_of_zeroing_them(
    store: Store, controller, run_request, check_runner: FakeCheckRunner
) -> None:
    outcome = controller.run_task(run_request)
    inspection = inspect_run(store, outcome.run_id)
    rendered = status_text(inspection)
    # The implementer's own count is labelled as a self-report, never as the run total.
    assert "implementer self-reported 1" in rendered
    assert "deterministic dispatch count" in rendered
    payload = report_json(inspection)
    receipt = payload["receipt"]
    assert receipt["usage"]["provider_cost"] is None
    assert receipt["usage"]["provider_billed_tokens"] is None


def test_status_says_dsh_context_was_not_recorded_without_a_frozen_candidate(
    store: Store, controller, run_request
) -> None:
    """An in-place run freezes no Git candidate, so nothing was classified: said in words."""
    outcome = controller.run_task(run_request)
    inspection = inspect_run(store, outcome.run_id)
    assert "dsh context   not recorded" in status_text(inspection)
    assert report_json(inspection)["dsh_context"] == []


@pytest.mark.parametrize(
    ("note", "message"),
    [
        ("dsh_context: {not json", "unreadable dsh_context"),
        ('dsh_context: {"paths": []}', "not a valid DshContextRecord"),
    ],
    ids=["not-json", "not-a-record"],
)
def test_an_unreadable_dsh_context_record_raises_instead_of_being_skipped(
    store: Store, controller, run_request, note: str, message: str
) -> None:
    """A dropped record would make a partial list look complete, so status fails loudly."""
    outcome = controller.run_task(run_request)
    store.record_note(outcome.run_id, note)
    with pytest.raises(StoreError, match=message):
        store.dsh_context_for(outcome.run_id)


def test_schema_covers_the_hand_written_input_documents(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each hand-written input file has its own top-level schema, not only a nested $def."""
    assert main(["schema"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    authorization = payload["AuthorizationRecord"]
    # The binding's shape is spelled out, not left as an untyped object.
    binding_ref = authorization["properties"]["binding"]["$ref"].rsplit("/", 1)[-1]
    assert binding_ref in authorization["$defs"]
    assert "limits" in payload["RootBudgetPlan"]["properties"]
    assert payload["RepairPolicy"]["title"] == "RepairPolicy"
    assert "run_already_ended" in payload["CancellationReceipt"]["properties"]


def test_run_has_no_force_flag(
    cli_env: dict[str, Path], project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--force`` bypassed nothing (the controller re-checks admission) and only hid the issues.

    It is gone: asking for it is a usage error, not a silent no-op.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "run",
                "--task",
                str(cli_env["task"]),
                "--project",
                str(cli_env["project"]),
                "--project-root",
                str(project_root),
                "--data-dir",
                str(cli_env["data_dir"]),
                "--force",
            ]
        )
    assert excinfo.value.code == 2
    assert "--force" in capsys.readouterr().err
    assert not cli_env["data_dir"].exists()


def test_top_level_help_names_every_subcommand_and_the_live_driver(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The description prose must not claim everything is offline or omit a subcommand."""
    import argparse

    from hflow.cli import build_parser

    parser = build_parser()
    description = parser.description or ""
    subcommands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ).choices
    assert len(subcommands) == 9
    for name in subcommands:
        assert name in description, name
    assert "offline in M1" not in description
    assert "acpx-dsh" in description


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


# --------------------------------------------------------------------------
# --repair-policy-file: one explicit opt-in, refused before anything exists
# --------------------------------------------------------------------------


def test_run_with_a_repair_policy_file_applies_that_policy_to_its_own_gate(
    cli_env: dict[str, Path], project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag reaches `run` as part of the effective spec, so its own gate refuses what it cannot carry.

    The frozen-checkout rule is what makes this observable offline: the fixture task is in-place,
    a repair policy needs the frozen candidate an isolated worktree keeps, and the message can
    only come from the policy having been applied to the spec this run resolved.
    """
    policy_file = cli_env["task"].parent / "repair-policy.json"
    policy_file.write_text(
        json.dumps({"max_attempts": 1, "check_exit_codes": {"unit": [1]}}), encoding="utf-8"
    )

    exit_code = main(
        [
            "run",
            "--task", str(cli_env["task"]),
            "--project", str(cli_env["project"]),
            "--project-root", str(project_root),
            "--driver", "fake",
            "--repair-policy-file", str(policy_file),
            "--json",
            "--data-dir", str(cli_env["data_dir"]),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == EXIT_REFUSED, captured
    assert "repair_policy" in captured.err, captured.err
    assert "workspace.mode='in_place'" in captured.err, captured.err
    assert "Nothing was dispatched" in captured.err, (
        "the refusal must say that no dispatch and no allowance happened"
    )


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"max_attempts": 2, "check_exit_codes": {"unit": [1]}},
                     id="more than the one repair this build implements"),
        pytest.param({"check_exit_codes": {}}, id="no trigger at all"),
        pytest.param({"check_exit_codes": {"unit": [0]}}, id="a passing code declared a failure"),
        pytest.param([{"check_exit_codes": {"unit": [1]}}], id="not a RepairPolicy document"),
    ],
)
def test_an_invalid_repair_policy_file_is_refused_before_a_run_row_exists(
    cli_env: dict[str, Path],
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
    document: object,
) -> None:
    """A policy is the user's opt-in: an unreadable one is refused, never defaulted or repaired."""
    policy_file = cli_env["task"].parent / "bad-policy.json"
    policy_file.write_text(json.dumps(document), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--task", str(cli_env["task"]),
            "--project", str(cli_env["project"]),
            "--project-root", str(project_root),
            "--driver", "fake",
            "--repair-policy-file", str(policy_file),
            "--json",
            "--data-dir", str(cli_env["data_dir"]),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == EXIT_REFUSED, captured
    assert "repair policy file" in captured.err, captured.err
    assert captured.out.strip() == "", "nothing was dispatched, so nothing is reported as run"
    assert not (cli_env["data_dir"] / "hflow.sqlite").exists(), (
        "a refused policy must be refused before the store, the run row or a workspace exists"
    )


def test_a_missing_repair_policy_file_is_refused_like_every_other_missing_input(
    cli_env: dict[str, Path], project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "prepare",
            "--task", str(cli_env["task"]),
            "--project", str(cli_env["project"]),
            "--project-root", str(project_root),
            "--repair-policy-file", str(cli_env["task"].parent / "ghost.json"),
            "--json",
            "--data-dir", str(cli_env["data_dir"]),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == EXIT_REFUSED, captured
    assert "repair policy file" in captured.err and "not found" in captured.err, captured.err
    assert not (cli_env["data_dir"] / "hflow.sqlite").exists()


# --------------------------------------------------------------------------
# cancel / resume from a process that does not own the invocation
# --------------------------------------------------------------------------


def _recorded_config(implementer_driver: str, reviewer_driver: str) -> EffectiveConfig:
    """An effective configuration naming one driver per role, as `hflow run` records it."""
    return EffectiveConfig(
        source="command_line",
        roles=[
            RoleConfig(
                role="implementer", agent="dsh", harness="dsh",
                driver=implementer_driver, driver_id=implementer_driver,
            ),
            RoleConfig(
                role="reviewer", agent="dsh", harness="dsh",
                driver=reviewer_driver, driver_id=reviewer_driver,
            ),
        ],
    )


def test_cli_cancel_never_confirms_a_production_child_it_does_not_own(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path, monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hflow cancel` runs in its own process and holds no handle to the reviewer's child.

    The run is driven by another controller whose reviewer is the production driver over the
    stub client and the ``stubborn`` agent (no model). While that child is alive, the CLI's
    answer must be ``unknown``: nothing it can reach confirmed the stop. The run therefore blocks
    ``outcome_unknown``, the ledger entry stays open, and the record names the recorded driver -
    never the offline fake that used to answer ``confirmed_stopped`` for it.
    """
    from .test_batch_e_dispatch import _authorization, _binding, _limits
    from .test_cancel_routing import RunningRun, _pair
    from .test_driver_acpx_dsh import FAKE_CLIENT, STUB_AGENT

    binding, limits = _binding(store, run_request.task, project_root), _limits()
    implementer, _ = _pair(project_root)
    reviewer = AcpxDshDriver(
        data_dir=tmp_path / "driver",
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        completion_timeout_seconds=15,
        agent_argv_override=[sys.executable, "-u", str(STUB_AGENT), "stubborn"],
    )
    scratch = tmp_path / "stub-scratch"
    scratch.mkdir()
    reviewer.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    runner = FakeCheckRunner()
    owner = Controller(
        store,
        implementer,
        reviewer_driver=reviewer,
        controller_build="cli-cancel-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=tmp_path / "data",
        production=False,
        effective_config=_recorded_config("fake", ACPX_DSH_DRIVER_ID),
        # Root-bound, so the ledger entry the stop must not settle exists.
        authorization=_authorization(
            spec=run_request.task, binding=binding, limits=limits, project_root=project_root
        ),
        preflight=lambda: (True, "the zero-model pre-flight is not what this test measures"),
        root_binding=binding,
        root_limits=limits,
    )
    spawned = threading.Event()
    original_popen = acpx_dsh_module.popen_in_boundary

    def popen_then_signal(*args, **kwargs):  # noqa: ANN002, ANN003
        child = original_popen(*args, **kwargs)
        spawned.set()
        return child

    monkeypatch.setattr(acpx_dsh_module, "popen_in_boundary", popen_then_signal)
    try:
        with RunningRun(owner, run_request) as running:
            assert spawned.wait(timeout=30), "the reviewer process was never created"
            run_id = running.run_id()
            running.wait_until(lambda: bool(reviewer._handles))
            review_invocation = next(iter(reviewer._handles))

            exit_code = main(
                [
                    "cancel", run_id, "--json",
                    "--project-root", str(project_root),
                    "--data-dir", str(tmp_path / "data"),
                ]
            )
            receipt = json.loads(capsys.readouterr().out)
            row = store.get_run(run_id)
            entry = store.invocation(review_invocation)
            child_alive = reviewer._processes[review_invocation].poll() is None

            # The owner stops its own child; the CLI never could.
            for handle in list(reviewer._handles.values()):
                reviewer.cancel_handle(handle)

        assert exit_code == EXIT_OK
        assert child_alive, "the child must still be running when the CLI answers"
        assert receipt["invocation_id"] == review_invocation
        assert receipt["status"] == "unknown", receipt
        assert receipt["mechanism"] == "none"
        assert receipt["local_process_stopped"] is None, "nothing here observed the process"
        assert row["task_state"] == "BLOCKED"
        assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, row["block_reason"]
        assert f"driver={ACPX_DSH_DRIVER_ID}" in row["block_reason"], row["block_reason"]
        assert "fake" not in row["block_reason"], row["block_reason"]
        assert entry is not None and entry.state is InvocationStartState.STARTED, (
            "an unconfirmed stop settles nothing: the entry stays open and keeps blocking"
        )
        assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
            review_invocation
        ]
        assert store.get_run(run_id)["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, (
            "the owner's late result must not relabel the stop the CLI recorded"
        )
        # The owner's run thread has returned by now, with whatever its force-stopped reviewer
        # gave back (a cancelled result or a driver error, depending on where the child was
        # stopped). Neither closes the entry or frees the root after the CLI's unconfirmed stop.
        assert running.error is None, running.error
        after_owner = store.invocation(review_invocation)
        assert after_owner is not None and after_owner.state is InvocationStartState.STARTED, (
            f"the owner's late return closed the entry: {after_owner}"
        )
        assert [e.invocation_id for e in store.pending_invocations(binding.root_id)] == [
            review_invocation
        ]
    finally:
        for invocation_id in list(reviewer._handles):
            reviewer.release(invocation_id)


def test_cli_cancel_of_an_offline_run_owned_by_another_fake_driver_is_unknown(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The offline fake answers only for invocations its own instance started.

    The run is held inside the implementer's ``start`` of one fake driver; the CLI builds its own
    and must not report a stop it never performed.
    """
    from .test_cancel_routing import RunningRun, _pair

    implementer, reviewer = _pair(project_root)
    implementer.gate = True
    runner = FakeCheckRunner()
    owner = Controller(
        store,
        implementer,
        reviewer_driver=reviewer,
        controller_build="cli-cancel-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=tmp_path / "data",
        production=False,
        effective_config=_recorded_config("fake", "fake"),
    )
    with RunningRun(owner, run_request) as running:
        request = running.wait_for_role(implementer, "implementer")
        assert implementer.entered.wait(timeout=30)
        run_id = running.run_id()
        exit_code = main(["cancel", run_id, "--json", "--data-dir", str(tmp_path / "data")])
        receipt = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_OK
    assert receipt["invocation_id"] == request.invocation_id
    assert receipt["status"] == "unknown", receipt
    assert receipt["mechanism"] == "none"
    assert implementer.cancel_calls == [], "the CLI's own driver answered, not the owner's"
    row = store.get_run(run_id)
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value, row["block_reason"]


def test_an_interrupted_controller_leaves_a_run_resume_reconciles(
    store: Store, project_root: Path, run_request: RunRequest, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C while the implementer runs: the run blocks ``outcome_unknown`` and `resume` works.

    Before, the interrupt escaped with the run still ``RUNNING`` and its ledger entry
    ``started``, and `hflow resume` answered "no-op for a run in state RUNNING".
    """

    class InterruptedDriver(FakeDriver):
        def start(self, request):  # noqa: ANN001, ANN201 - the driver protocol's own shape
            self.started.append(request)
            self._report_spawn(request, created=True, detail="the invocation began")
            raise KeyboardInterrupt

    runner = FakeCheckRunner()
    owner = Controller(
        store,
        InterruptedDriver(project_root),
        controller_build="cli-resume-test",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=tmp_path / "data",
        production=False,
    )
    with pytest.raises(KeyboardInterrupt):
        owner.run_task(run_request)

    run_id = str(store.list_runs()[0]["run_id"])
    row = store.get_run(run_id)
    assert row["task_state"] == "BLOCKED"
    assert row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value
    assert "controller interrupted during implementer invocation" in row["block_reason"]
    attempt = store.open_attempt(run_id)
    assert attempt["outcome"] == InvocationOutcome.OUTCOME_UNKNOWN.value
    assert attempt["reconcile_json"] is None

    exit_code = main(["resume", run_id, "--json", "--data-dir", str(tmp_path / "data")])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == EXIT_BLOCKED, payload
    assert any("reconciled an interrupted attempt" in note for note in payload["notes"]), payload
    reconciled = json.loads(store.open_attempt(run_id)["reconcile_json"])
    assert reconciled["invocation_id"] == attempt["invocation_id"]
    assert reconciled["outcome"] == "unknown"
    assert store.get_run(run_id)["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value


def test_the_fake_driver_confirms_only_the_stops_of_its_own_invocations(
    project_root: Path,
) -> None:
    owner, bystander = FakeDriver(project_root), FakeDriver(project_root)
    request = InvocationRequest.model_construct(invocation_id="I-own", role="implementer")
    owner.started.append(request)

    assert owner.cancel("I-own").status == "confirmed_stopped"
    foreign = bystander.cancel("I-own")
    assert foreign.status == "unknown"
    assert foreign.mechanism == "none"
    assert foreign.local_process_stopped is None


def test_zero_model_preflight_refuses_an_unanswered_client_exit() -> None:
    """``process_gone`` may now be ``None`` (unanswered): the pre-flight refuses it like False."""
    from hflow.cli import _zero_model_preflight

    class ProbeOnly:
        driver_id = "probe-only"

        def readonly_client_check(self, args=None, *, timeout_seconds=60):
            return {
                "returncode": 0,
                "stdout": "9.9.9\n",
                "stderr": "",
                "boundary_kind": "stub",
                "boundary_empty": True,
                "process_gone": None,
            }

    ok, detail = _zero_model_preflight({"implementer": ProbeOnly()})()

    assert ok is False
    assert "role implementer" in detail
    assert "did not settle" in detail
