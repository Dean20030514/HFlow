"""Batch E1 acceptance item 7 and batch E2's admission/preview surface.

E1: ``hflow prepare`` with a root budget file writes nothing. A preview that created the SQLite
ledger it names would be a different command, so this file pins the three absences and the
reported facts:

* **no SQLite file, no run row, no authorization** - asserted by the absence of the database the
  preview names (a file that does not exist cannot hold a row), plus the report's own pinned
  ``creates_authorization: false`` and ``model_calls_made: 0``;
* the **root binding** it would register, the **limits** from the file, the **required
  submission count** for one accepted delivery, and that **no repair is planned** for a task
  that carries no policy (no in-run repair round is armed, but a later revision's first
  implementer on a root that already dispatched one is still charged to the repair counter,
  which the run - not the preview - checks against the ledger) - in both the JSON and the text
  form;
* the **exit code follows ``ready_to_dispatch``**: a task the run would dispatch previews as
  ready, a task the run would refuse previews as refused even though a root file was given.

E2 adds three more properties, all offline:

* a repair policy this **scope cannot support** (no worktree, no review, a run ceiling below the
  worst-case loop) is refused by ``admission.predictable_dispatch_problems`` before any dispatch;
* a policy the scope *can* support is reported by ``prepare`` **as data**: present or absent, the
  policy digest, the enabled triggers with their declared exit codes, the worst-case dispatch
  count, and the statement that one repair is the maximum;
* ``status``/``report`` render **every recorded repair decision** - refusals included - with its
  trigger, round, failed checks and exit codes, and say ``no repair decision recorded`` for a run
  that decided nothing. The attempt line carries the attempt's own ``repair=`` fact.

Everything runs offline through the real CLI entry point or through the pure admission function:
the fake driver, ``tmp_path``, no model, no network. The flags this file depends on are
``--root-budget-file`` and ``--repair-policy-file`` as they exist on ``prepare``; the arguments
below are exactly the ones that surface accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.admission import predictable_dispatch_problems
from hflow.cli import EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import (
    RefusalCode,
    RepairDecision,
    RepairPolicy,
    RepairRecord,
    RepairTrigger,
    ReviewRequirement,
    Scope,
    TaskSpec,
    WorkspaceSpec,
    root_id_for,
)
from hflow.controller import inspect_run
from hflow.paths import database_path
from hflow.report import report_json, report_text, status_text
from hflow.store import Store

from .conftest import write_profile, write_project, write_task

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
    repair_policy: Path | None = None,
) -> list[str]:
    argv = [
        "prepare",
        "--task", str(task),
        "--project", str(project),
        "--project-root", str(project_root),
    ]
    if root_budget is not None:
        argv += ["--root-budget-file", str(root_budget)]
    if repair_policy is not None:
        argv += ["--repair-policy-file", str(repair_policy)]
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
    repair_policy: Path | None = None,
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
            repair_policy=repair_policy,
        )
    )
    return code, data_dir


def _policy_file(path: Path, **overrides: object) -> Path:
    """One repair policy document, as a user would write it: the check and its exit code."""
    document: dict[str, object] = {"max_attempts": 1, "check_exit_codes": {"unit": [1, 3]}}
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _policy(**overrides: object) -> RepairPolicy:
    fields: dict[str, object] = {"check_exit_codes": {"unit": [1]}}
    fields.update(overrides)
    return RepairPolicy(**fields)  # type: ignore[arg-type]


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
    assert root["repair_enabled"] is False, "a budget field is not an armed repair"
    assert root["single_loop_dispatches"] == REQUIRED_SUBMISSIONS
    assert "carries no repair policy" in root["detail"], root["detail"]
    # No in-run round is armed, but the counter is not idle: a later revision's first
    # implementer is charged to it, and only the run (which reads the ledger) refuses on it.
    assert "nothing spends" not in root["detail"], root["detail"]
    assert "no in-run repair round is armed" in root["detail"], root["detail"]
    assert "this revision's first implementer is charged to it" in root["detail"], root["detail"]
    assert "refuses budget_exhausted before writing anything" in root["detail"], root["detail"]
    assert "does not read the ledger" in root["detail"], root["detail"]
    repair_notes = [
        note for note in payload["notes"] if "no repair is planned for this task" in note
    ]
    assert len(repair_notes) == 1, payload["notes"]
    assert "nothing spends" not in repair_notes[0], repair_notes[0]
    assert "no in-run repair round is armed" in repair_notes[0], repair_notes[0]
    assert "first implementer is charged to it as a repair" in repair_notes[0], repair_notes[0]
    assert "refuses budget_exhausted before writing anything" in repair_notes[0]
    assert "prepare does not read the ledger" in repair_notes[0], repair_notes[0]

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
    # The E1 sentence ("not implemented in E1") would now be false: repair exists, and what this
    # task has is the absence of an opt-in - which the text must say in those words.
    assert "repair      none planned - this task carries no repair policy" in text, text
    assert "nothing spends" not in text, text
    assert "no in-run repair round is armed" in text, text
    assert "this revision's first implementer is charged to" in text, text
    assert "budget_exhausted before writing anything when none is left" in text, text
    assert "repair plan" in text and "no repair_policy on this task" in text, text


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


# --------------------------------------------------------------------------
# 4. a repair policy this scope cannot support is refused before any dispatch
# --------------------------------------------------------------------------
#
# Three facts decide whether this scope can carry a repair, and each is knowable from the spec
# and the contract alone: an isolated worktree to start the repair from, a review that can accept
# the repaired candidate, and a run ceiling that covers the worst-case loop. The function under
# test is the one `run`'s own dispatch gate calls, so an offline preview and a live run cannot
# disagree about them.
#
# `production=True` is how the gate is asked the question a real (non-fake) run asks: the fake
# driver's checks are fake by construction and its change is scripted, so these rules never apply
# to an offline run. No model is called by any of this: it is a pure function of two contracts.
# Those calls pass `root_bound=True`, because a real transport's repair also needs a root binding
# (plan 5.1) and that rule is measured on its own below.


def _unwritable(spec: TaskSpec, **updates: object) -> TaskSpec:
    """The same task with no write paths, so a repair-slot rule can be observed on its own."""
    return spec.model_copy(update={"scope": Scope(write_allow=[]), **updates})


def test_a_repair_policy_without_a_worktree_is_refused(project, worktree_task) -> None:
    """A repair starts from the frozen candidate: an in-place run has none to start from."""
    spec = _unwritable(
        worktree_task,
        workspace=WorkspaceSpec(mode="in_place"),
        repair_policy=_policy(),
    )

    issues = predictable_dispatch_problems(
        spec, project, production=True, implementer_writes=True, root_bound=True
    )

    assert [(issue.code, issue.location) for issue in issues] == [
        (RefusalCode.SCOPE_VIOLATION, "workspace.mode")
    ]
    detail = issues[0].detail
    assert "repair_policy" in detail, detail
    assert "in_place" in detail, "the message must name the value it refuses"
    assert "worktree" in detail and "drop repair_policy" in detail, (
        "the message must say what to do about it"
    )


def test_a_repair_policy_without_a_review_is_refused(project, worktree_task) -> None:
    """A repaired candidate must be reviewed before acceptance, so a policy without one is out."""
    spec = worktree_task.model_copy(
        update={"review": ReviewRequirement(required=False), "repair_policy": _policy()}
    )

    issues = predictable_dispatch_problems(
        spec,
        project.model_copy(update={"review_required": False}),
        production=True,
        implementer_writes=True,
        root_bound=True,
    )

    assert [(issue.code, issue.location) for issue in issues] == [
        (RefusalCode.NOT_IMPLEMENTED, "review.required")
    ]
    detail = issues[0].detail
    assert "repair_policy" in detail and "review.required=False" in detail, detail
    assert "require a review" in detail and "drop repair_policy" in detail, detail


def test_a_repair_policy_the_run_ceiling_cannot_cover_is_refused(project, worktree_task) -> None:
    """A reviewed repair's worst case is I1 + R1 + I2 + R2 = 4, and the run's ceiling must hold it."""
    spec = worktree_task.model_copy(
        update={
            "repair_policy": _policy(),
            "budget": worktree_task.budget.model_copy(update={"max_agent_turns": 3}),
        }
    )

    issues = predictable_dispatch_problems(
        spec, project, production=True, implementer_writes=True, root_bound=True
    )

    assert [(issue.code, issue.location) for issue in issues] == [
        (RefusalCode.BUDGET_EXCEEDED, "budget.max_agent_turns")
    ]
    detail = issues[0].detail
    assert "max_agent_turns=3" in detail, "the message must name the value it refuses"
    assert "I1 + R1 + I2 + R2" in detail and "at least 4" in detail, detail


def test_a_repair_policy_without_review_needs_only_the_two_dispatch_loop(
    project, worktree_task
) -> None:
    """Without a review the worst case is I1 + I2 = 2, so the ceiling rule uses 2, not 4 - and
    the review rule is what refuses the policy, not a fabricated turns shortage."""
    spec = worktree_task.model_copy(
        update={
            "review": ReviewRequirement(required=False),
            "repair_policy": _policy(),
            "budget": worktree_task.budget.model_copy(update={"max_agent_turns": 2}),
        }
    )
    unreviewed_project = project.model_copy(update={"review_required": False})

    issues = predictable_dispatch_problems(
        spec, unreviewed_project, production=True, implementer_writes=True, root_bound=True
    )

    assert [(issue.code, issue.location) for issue in issues] == [
        (RefusalCode.NOT_IMPLEMENTED, "review.required")
    ], "a ceiling of 2 covers I1 + I2: only the missing review is wrong here"

    # One turn short of that two-dispatch loop, and the message prices the loop it actually needs.
    short = spec.model_copy(
        update={"budget": spec.budget.model_copy(update={"max_agent_turns": 1})}
    )
    short_issues = predictable_dispatch_problems(
        short, unreviewed_project, production=True, implementer_writes=True, root_bound=True
    )
    turns = [issue for issue in short_issues if issue.code == RefusalCode.BUDGET_EXCEEDED]
    assert len(turns) == 1, short_issues
    assert "I1 + I2" in turns[0].detail and "of 2 top-level dispatch(es)" in turns[0].detail


def test_a_repair_policy_this_scope_supports_is_not_refused(project, worktree_task) -> None:
    """The rules above must not refuse the configuration they describe: worktree, review, 4 turns."""
    spec = worktree_task.model_copy(update={"repair_policy": _policy()})

    assert (
        predictable_dispatch_problems(
            spec, project, production=True, implementer_writes=True, root_bound=True
        )
        == []
    )


def test_a_repair_policy_the_scope_cannot_support_is_refused_offline_too(
    project, worktree_task
) -> None:
    """`production` decides the machine rules, not whether a task can carry a repair at all.

    The controller honours ``spec.repair_policy`` whatever driver is bound, so an offline repair
    in place would write into the user's own checkout exactly as a live one would - and an
    offline run with a two-turn ceiling could never buy the loop its policy needs. Both are
    refused with the same message a live run gets.
    """
    in_place = _unwritable(
        worktree_task,
        workspace=WorkspaceSpec(mode="in_place"),
        repair_policy=_policy(),
    )
    offline_issues = predictable_dispatch_problems(
        in_place, project, production=False, implementer_writes=False
    )
    assert [(issue.code, issue.location) for issue in offline_issues] == [
        (RefusalCode.SCOPE_VIOLATION, "workspace.mode")
    ]

    too_few_turns = worktree_task.model_copy(
        update={
            "repair_policy": _policy(),
            "budget": worktree_task.budget.model_copy(update={"max_agent_turns": 2}),
        }
    )
    turn_issues = predictable_dispatch_problems(
        too_few_turns, project, production=False, implementer_writes=False
    )
    assert [(issue.code, issue.location) for issue in turn_issues] == [
        (RefusalCode.BUDGET_EXCEEDED, "budget.max_agent_turns")
    ]

    supported = worktree_task.model_copy(update={"repair_policy": _policy()})
    assert (
        predictable_dispatch_problems(
            supported, project, production=False, implementer_writes=False
        )
        == []
    ), "the offline path must not refuse a policy the scope does support"


def test_a_repair_policy_on_a_real_transport_needs_a_root_binding(project, worktree_task) -> None:
    """Plan 5.1: a real transport's repair is charged to a root, so a rootless one is refused.

    Without a root the repair would be bought outside every cross-revision counter - each new
    revision could buy its own. The offline fake driver reaches no model and keeps its rootless
    repair, which is why the rule follows the transport rather than the policy alone.
    """
    spec = worktree_task.model_copy(update={"repair_policy": _policy()})

    rootless = predictable_dispatch_problems(
        spec, project, production=True, implementer_writes=True
    )
    assert [(issue.code, issue.location) for issue in rootless] == [
        (RefusalCode.BUDGET_EXCEEDED, "root_budget")
    ]
    detail = rootless[0].detail
    assert "repair_policy" in detail and "--root-budget-file" in detail, detail
    assert "drop repair_policy" in detail, "the message must say what to do about it"

    assert (
        predictable_dispatch_problems(
            spec, project, production=True, implementer_writes=True, root_bound=True
        )
        == []
    ), "a bound root is exactly what the rule asks for"
    assert (
        predictable_dispatch_problems(
            spec, project, production=False, implementer_writes=False
        )
        == []
    ), "a fully offline run may still repair without a root"
    assert predictable_dispatch_problems(
        spec, project, production=False, implementer_writes=False, real_transport=True
    ), "the transport decides, not the production label: a real driver needs the root offline too"


def test_prepare_refuses_a_live_repair_policy_without_a_root_budget_file(
    tmp_path: Path,
    live_project,
    worktree_task,
    project_root: Path,
    live_profile,
    acpx_client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the real command surface: a live policy without a root previews as refused."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    policy_file = _policy_file(tmp_path / "repair-policy.json")
    task_file = write_task(tmp_path / "task.json", worktree_task)
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)
    argv = [
        "prepare",
        "--task", str(task_file),
        "--project", str(project_file),
        "--project-root", str(project_root),
        "--profile", "dsh-local",
        "--repair-policy-file", str(policy_file),
        "--json",
        "--data-dir", str(data_dir),
    ]

    code = main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED, "a task the live gate refuses must not preview as ready"
    root_issues = [
        issue for issue in payload["dispatch_preconditions"] if issue["location"] == "root_budget"
    ]
    assert len(root_issues) == 1, payload["dispatch_preconditions"]
    assert "--root-budget-file" in root_issues[0]["detail"], root_issues[0]["detail"]

    root_file = _write_root_budget(tmp_path / "root-budget.json")
    main(argv + ["--root-budget-file", str(root_file)])
    bound = json.loads(capsys.readouterr().out)
    assert [
        issue for issue in bound["dispatch_preconditions"] if issue["location"] == "root_budget"
    ] == [], "with a root budget file the root rule is satisfied"
    assert not database_path(data_dir).exists(), "a preview writes nothing either way"


def test_prepare_reports_a_root_budget_file_with_no_repair_for_an_armed_policy(
    tmp_path: Path,
    project,
    worktree_task,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``max_repairs`` 0 with a repair policy: the run refuses it, so the preview does too.

    Decidable from the two files alone - the repair needs at least one - and still zero-write.
    The ledger-dependent part (a later revision's first implementer also counts as a repair) is
    the run's own gate, which a preview cannot see without opening the database.
    """
    policy_file = _policy_file(tmp_path / "repair-policy.json")
    no_repair = _write_root_budget(
        tmp_path / "root-budget-0.json", limits={**LIMITS, "max_repairs": 0}
    )

    code, data_dir = _prepare(
        tmp_path, project, worktree_task, project_root,
        root_budget=no_repair, repair_policy=policy_file,
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED, "a root the run refuses must not preview as ready"
    root_issues = [
        issue for issue in payload["dispatch_preconditions"] if issue["location"] == "root_budget"
    ]
    assert [issue["code"] for issue in root_issues] == [RefusalCode.BUDGET_EXHAUSTED.value]
    assert "max_repairs" in root_issues[0]["detail"], root_issues[0]["detail"]
    assert not database_path(data_dir).exists()

    one_repair = _write_root_budget(tmp_path / "root-budget-1.json")
    code, data_dir = _prepare(
        tmp_path, project, worktree_task, project_root,
        root_budget=one_repair, repair_policy=policy_file,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK, payload["dispatch_preconditions"]
    assert payload["dispatch_preconditions"] == []
    assert not database_path(data_dir).exists()

    code, _ = _prepare(tmp_path, project, worktree_task, project_root, root_budget=no_repair)
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK, (
        "without a repair policy the preview cannot refuse on max_repairs 0: whether a later "
        "revision's first implementer is charged as a repair depends on the ledger, which only "
        "the run reads"
    )


# --------------------------------------------------------------------------
# 5. the repair plan a preview reports: present or absent, and never a promise
# --------------------------------------------------------------------------


def test_prepare_without_a_policy_reports_that_no_repair_is_planned(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absent policy: the preview says so in data, and the worst case stays the fixed loop."""
    code, data_dir = _prepare(
        tmp_path, project, task_spec, project_root, root_budget=None
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    plan = payload["repair_plan"]
    assert plan["enabled"] is False
    assert plan["policy_digest"] == ""
    assert plan["check_exit_codes"] == {}
    assert plan["allow_reviewer_changes"] is False
    assert plan["triggers"] == []
    assert plan["single_loop_dispatches"] == 2
    assert plan["worst_case_dispatches"] == 2
    assert "no repair_policy on this task" in plan["detail"], plan["detail"]
    assert payload["budget"]["required_turns"] == 2
    assert payload["budget"]["repair_cycles"] == 0
    assert not database_path(data_dir).exists()


def test_prepare_with_a_policy_file_reports_the_triggers_digest_and_worst_case(
    tmp_path: Path,
    project,
    worktree_task,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flag's policy becomes the effective spec: digest, triggers, and the numbers to cover.

    The task is the worktree shape a policy requires: a preview of an in-place policy would be a
    preview of a task every run refuses, which proves nothing about the one that runs.
    """
    policy_file = _policy_file(
        tmp_path / "repair-policy.json",
        check_exit_codes={"unit": [1, 3]},
        allow_reviewer_changes=True,
    )
    root_file = _write_root_budget(tmp_path / "root-budget.json")
    policy = RepairPolicy.model_validate(json.loads(policy_file.read_text(encoding="utf-8")))

    code, data_dir = _prepare(
        tmp_path,
        project,
        worktree_task,  # the task itself carries no policy: the flag is the opt-in
        project_root,
        root_budget=root_file,
        repair_policy=policy_file,
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    plan = payload["repair_plan"]
    assert plan["enabled"] is True
    assert plan["policy_digest"] == policy.digest()
    assert plan["check_exit_codes"] == {"unit": [1, 3]}
    assert plan["allow_reviewer_changes"] is True
    assert plan["triggers"] == [
        RepairTrigger.BUSINESS_CHECK_FAILED.value,
        RepairTrigger.REVIEW_CHANGES_REQUESTED.value,
    ]
    assert plan["single_loop_dispatches"] == 2
    assert plan["worst_case_dispatches"] == 4
    assert "one repair is the maximum" in plan["detail"], plan["detail"]
    assert "ceiling, not a quota" in plan["detail"], plan["detail"]
    assert "declared business check failure" in plan["detail"], plan["detail"]
    assert "reviewer changes_requested" in plan["detail"], plan["detail"]

    # The three numbers a run needs before I1: the run's own budget, the authorization and the
    # root. All three must cover the worst case, and all three must still say what the fixed loop
    # is - one number alone would read as if every delivery cost four dispatches.
    assert payload["budget"]["required_turns"] == 4
    assert payload["budget"]["repair_cycles"] == 1
    assert payload["budget"]["within_budget"] is True
    assert payload["authorization"]["max_top_level_submissions_required"] == 4
    assert payload["root_budget"]["required_top_level_submissions"] == 4
    assert payload["root_budget"]["single_loop_dispatches"] == 2
    assert payload["root_budget"]["repair_enabled"] is True
    assert "worst case of 4" in payload["root_budget"]["detail"], payload["root_budget"]["detail"]
    assert any(
        "repair policy" in note and "4 top-level dispatch" in note for note in payload["notes"]
    ), payload["notes"]

    # The opt-in is inside the spec digest, so the approval and the idempotency key cover it.
    assert payload["spec_digest"] == worktree_task.model_copy(
        update={"repair_policy": policy}
    ).spec_digest()
    assert payload["spec_digest"] != worktree_task.spec_digest()
    # Still zero-write: naming a root and a policy creates neither.
    assert not database_path(data_dir).exists()
    assert list(data_dir.rglob("*.sqlite")) == []


def test_the_text_preview_states_the_one_repair_maximum_and_what_may_spend_it(
    tmp_path: Path,
    project,
    worktree_task,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The text form carries the same facts as the JSON form, in the same words."""
    policy_file = _policy_file(tmp_path / "repair-policy.json")
    policy = RepairPolicy.model_validate(json.loads(policy_file.read_text(encoding="utf-8")))

    code, data_dir = _prepare(
        tmp_path,
        project,
        worktree_task,
        project_root,
        root_budget=None,
        json_output=False,
        repair_policy=policy_file,
    )
    text = capsys.readouterr().out

    assert code == EXIT_OK
    assert "repair plan" in text
    assert f"enabled     policy digest {policy.digest()}" in text
    assert "trigger     business_check_failed: unit -> exit code(s) 1, 3" in text
    assert "trigger     review_changes_requested: not allowed" in text
    assert "maximum     one repair, worst case 4 top-level dispatch(es)" in text
    assert "one repair is the maximum for this run" in text
    assert "ceiling, not a quota" in text
    assert not database_path(data_dir).exists()


def test_a_policy_already_in_the_task_needs_no_flag_and_an_identical_flag_agrees(
    tmp_path: Path,
    project,
    worktree_task,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two sources naming the same policy are not a disagreement; the resolution is idempotent."""
    policy = _policy(check_exit_codes={"unit": [1, 3]}, allow_reviewer_changes=True)
    spec = worktree_task.model_copy(update={"repair_policy": policy})
    policy_file = tmp_path / "repair-policy.json"
    policy_file.write_text(json.dumps(policy.model_dump(mode="json")), encoding="utf-8")

    without_flag = _prepare(tmp_path, project, spec, project_root, root_budget=None)
    first = json.loads(capsys.readouterr().out)
    with_flag = _prepare(
        tmp_path, project, spec, project_root, root_budget=None, repair_policy=policy_file
    )
    second = json.loads(capsys.readouterr().out)

    assert without_flag[0] == EXIT_OK and with_flag[0] == EXIT_OK
    assert first["repair_plan"]["enabled"] is True
    assert first["repair_plan"]["policy_digest"] == policy.digest()
    assert second["repair_plan"] == first["repair_plan"]
    assert second["spec_digest"] == first["spec_digest"], (
        "the same policy written twice must resolve to one task document"
    )


def test_a_flag_that_contradicts_the_task_policy_is_refused_before_anything_is_read(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two policies that disagree refuse rather than one silently winning."""
    task_policy = _policy(check_exit_codes={"unit": [1]})
    other = _policy(check_exit_codes={"unit": [2]})
    spec = task_spec.model_copy(update={"repair_policy": task_policy})
    policy_file = tmp_path / "repair-policy.json"
    policy_file.write_text(json.dumps(other.model_dump(mode="json")), encoding="utf-8")

    code, data_dir = _prepare(
        tmp_path, project, spec, project_root, root_budget=None, repair_policy=policy_file
    )
    captured = capsys.readouterr()

    assert code == EXIT_REFUSED, captured
    assert "repair policy" in captured.err and "another" in captured.err, captured.err
    assert task_policy.digest() in captured.err and other.digest() in captured.err
    assert captured.out.strip() == "", "a refused preview prints no report to parse"
    assert not database_path(data_dir).exists()


def test_prepare_refuses_a_repair_policy_the_live_scope_cannot_support(
    tmp_path: Path,
    live_project,
    task_spec,
    project_root: Path,
    live_profile,
    acpx_client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the real command surface: a live run's gate and the preview refuse the same thing."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    policy_file = _policy_file(tmp_path / "repair-policy.json")
    task_file = write_task(tmp_path / "task.json", task_spec)  # in_place: no frozen candidate
    project_file = write_project(tmp_path / "hflow" / "project.json", live_project)

    code = main(
        [
            "prepare",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--profile", "dsh-local",
            "--repair-policy-file", str(policy_file),
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_REFUSED, "a task the live gate refuses must not preview as ready"
    assert payload["repair_plan"]["enabled"] is True, (
        "the policy was read; it is the scope that cannot carry it"
    )
    assert any(
        "repair_policy" in issue["detail"] and issue["location"] == "workspace.mode"
        for issue in payload["dispatch_preconditions"]
    ), payload["dispatch_preconditions"]
    assert not database_path(data_dir).exists(), "a refused preview writes nothing"


# --------------------------------------------------------------------------
# 6. status/report render every recorded repair decision, or say there is none
# --------------------------------------------------------------------------


def _accepted_offline_run(
    tmp_path: Path, project, spec, project_root: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[str, Path]:
    """One accepted offline run through the real CLI. Returns ``(run_id, data_dir)``."""
    task_file = write_task(tmp_path / "run-task.json", spec)
    project_file = write_project(tmp_path / "run-hflow" / "project.json", project)
    data_dir = tmp_path / "run-data"
    code = main(
        [
            "run",
            "--task", str(task_file),
            "--project", str(project_file),
            "--project-root", str(project_root),
            "--driver", "fake",
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK, payload
    assert payload["task_state"] == "ACCEPTED", payload
    return payload["run_id"], data_dir


def test_status_says_no_repair_decision_recorded_and_never_prints_an_empty_table(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run that decided nothing says so in words; a blank section would read as a lost fact."""
    run_id, data_dir = _accepted_offline_run(
        tmp_path, project, task_spec, project_root, capsys
    )

    store = Store(data_dir / "hflow.sqlite")
    try:
        inspection = inspect_run(store, run_id)
        assert inspection.repair_records == []
        assert store.repair_records_for(run_id) == []
    finally:
        store.close()

    rendered = status_text(inspection)
    assert "repair        no repair decision recorded" in rendered, rendered
    assert "repair decisions" not in rendered, "no empty table for a run that decided nothing"
    assert report_json(inspection)["repair_records"] == []
    # Every attempt line carries its own repair fact, and this run has no repair attempt.
    attempt_lines = [line for line in rendered.splitlines() if "revision=" in line]
    assert attempt_lines and all("repair=False" in line for line in attempt_lines), rendered


def test_status_and_report_render_every_recorded_repair_decision_including_refusals(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Decision, trigger, round, failed checks with exit codes, and the reason - for each row.

    The records are written through the store's own append path and read back before rendering,
    so this pins the report over recorded facts rather than over a locally-invented list.
    """
    run_id, data_dir = _accepted_offline_run(
        tmp_path, project, task_spec, project_root, capsys
    )
    allowed = RepairRecord(
        decision=RepairDecision.ALLOWED,
        trigger=RepairTrigger.BUSINESS_CHECK_FAILED,
        reason="check 'unit' exited 1, the code the policy declares as its business assertion failing",
        policy_digest="sha256:policy-digest",
        failed_checks=["unit", "docs-check"],
        exit_codes={"unit": 1, "docs-check": 3},
        round=2,
        decided_at="2026-01-01T00:00:02Z",
    )
    refused = RepairRecord(
        decision=RepairDecision.NOT_A_BUSINESS_FAILURE,
        trigger=None,
        reason="check 'unit' was killed (exit 137): a forced stop is not a business assertion",
        policy_digest="sha256:policy-digest",
        failed_checks=["unit"],
        exit_codes={"unit": 137},
        round=1,
        decided_at="2026-01-01T00:00:01Z",
    )

    store = Store(data_dir / "hflow.sqlite")
    try:
        assert store.repair_records_for(run_id) == []
        store.record_repair_record(run_id, allowed)
        store.record_repair_record(run_id, refused)
        stored = store.repair_records_for(run_id)
        assert stored == [allowed, refused], "the store keeps decisions in order, verbatim"

        inspection = inspect_run(store, run_id)
    finally:
        store.close()

    assert inspection.repair_records == [allowed, refused], (
        "the read model `status`/`report` render must carry the run's recorded decisions"
    )
    rendered = status_text(inspection)

    assert "repair decisions" in rendered
    assert "decision=allowed trigger=business_check_failed round=2" in rendered
    assert "policy    sha256:policy-digest" in rendered
    assert "failed    unit exit=1, docs-check exit=3" in rendered
    assert "reason    check 'unit' exited 1" in rendered
    assert "decision=not_a_business_failure trigger=none round=1" in rendered
    assert "failed    unit exit=137" in rendered
    assert "reason    check 'unit' was killed" in rendered
    assert rendered.index("decision=allowed") < rendered.index(
        "decision=not_a_business_failure"
    ), "the decisions render in the order they were recorded"

    # `report` is the same projection plus the receipt: the decisions must survive there too.
    full = report_text(inspection)
    assert "decision=allowed trigger=business_check_failed" in full
    assert "decision=not_a_business_failure" in full

    payload = report_json(inspection)
    assert [entry["decision"] for entry in payload["repair_records"]] == [
        "allowed",
        "not_a_business_failure",
    ]
    assert payload["repair_records"][0]["failed_checks"] == ["unit", "docs-check"]
    assert payload["repair_records"][0]["exit_codes"] == {"unit": 1, "docs-check": 3}
    assert payload["repair_records"][0]["decided_at"] == "2026-01-01T00:00:02Z"


def test_the_attempt_line_reports_whether_that_attempt_was_the_repair(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`repair=` comes from the attempt's own row, so the two rounds are told apart without counting."""
    run_id, data_dir = _accepted_offline_run(
        tmp_path, project, task_spec, project_root, capsys
    )

    store = Store(data_dir / "hflow.sqlite")
    try:
        inspection = inspect_run(store, run_id)
    finally:
        store.close()

    assert inspection.attempts, "the accepted run has attempt rows"
    # Both values on one page: each real row twice, once as a first attempt and once as the
    # repair round it would be, so the line is proven to follow the row rather than the position.
    seeded = [
        attempt.model_copy(update={"attempt_id": f"{attempt.attempt_id}-round1", "is_repair": False})
        for attempt in inspection.attempts
    ] + [
        attempt.model_copy(update={"attempt_id": f"{attempt.attempt_id}-round2", "is_repair": True})
        for attempt in inspection.attempts
    ]

    rendered = status_text(inspection.model_copy(update={"attempts": seeded}))
    lines = [line for line in rendered.splitlines() if "revision=" in line]
    for attempt in seeded:
        matching = [line for line in lines if attempt.attempt_id in line]
        assert matching, f"no attempt line for {attempt.attempt_id}"
        assert any(f"repair={attempt.is_repair}" in line for line in matching), matching
    assert any("repair=True" in line for line in lines), rendered
    assert any("repair=False" in line for line in lines), rendered
