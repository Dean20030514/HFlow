"""Batch K: the next operator command, derived from stored facts only (``hflow.hints``).

The pure half (``plan_next``) is checked over every state it distinguishes with hand-built facts;
the stored half (``next_steps``) and the ``next:`` lines of ``status``, ``resume``,
``ledger settle`` and ``integrate`` are checked on real stored runs: an offline fake-driver run in
a real Git repository walked to a released worktree by pasting the printed commands, and a seeded
ledger whose run was interrupted mid-turn. Nothing here calls a model.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from hflow.authorization import project_contract_digest
from hflow.cli import EXIT_BLOCKED, EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import (
    IntegrationRecord,
    IntegrationState,
    RefusalCode,
    RefusedError,
    RootBudgetLimits,
    TaskSpec,
    TaskState,
)
from hflow.hints import (
    ATTEST_PLACEHOLDER,
    BRANCH_PLACEHOLDER,
    PROJECT_PLACEHOLDER,
    RunFacts,
    named_data_dir,
    next_lines,
    next_steps,
    plan_next,
    shell_word,
)
from hflow.integrate import prepare_integration
from hflow.store import Store
from hflow.verify import CheckRunners

from tests.store_helpers import mark_invocation_started
from tests.test_batch_e_ledger import _binding, _register_authorization, _reserve, _seed_ready_run
from tests.test_integrate import Scene

DATA = "C:\\ledger dir" if os.name == "nt" else "/srv/ledger dir"
TIP = "a" * 40
ATTEST = "Checked the provider console: no request from this launch appears."


def _record(state: IntegrationState, integration_id: str = "G-1", **fields) -> IntegrationRecord:
    return IntegrationRecord(
        integration_id=integration_id,
        run_id="R-1",
        task_id="T-1",
        attempt_id="A-1",
        git_common_dir="/repo/.git",
        repo_root="/repo",
        target_ref="refs/heads/main",
        target_tip=TIP,
        base_commit="b" * 40,
        candidate_commit="c" * 40,
        state=state,
        checks_digest="sha256:checks",
        created_at="2026-10-13T00:00:00Z",
        updated_at="2026-10-13T00:00:00Z",
        **fields,
    )


def _accepted(**fields) -> RunFacts:
    """An accepted Git candidate whose worktree is still there."""
    base = {
        "run_id": "R-1",
        "task_state": TaskState.ACCEPTED,
        "delivery_state": "LOCAL_CANDIDATE",
        "git_candidate": True,
        "worktree_state": "READY",
        "worktree_path": "/repo.hflow-worktrees/R-1",
    }
    return RunFacts(**{**base, **fields})


def _commands(steps: list[str]) -> list[str]:
    """The steps without their trailing ``# ...`` comments."""
    return [step.split("  # ", 1)[0] for step in steps]


# --------------------------------------------------------------------------
# plan_next: one case per state it tells apart
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", [TaskState.READY, TaskState.RUNNING, TaskState.CHECKING])
def test_a_live_run_is_looked_at_again_and_resumed_only_from_a_dead_owner(state) -> None:
    steps = plan_next(RunFacts(run_id="R-1", task_state=state))
    assert _commands(steps) == ["hflow status R-1", "hflow resume R-1"]
    assert "only once its controller has exited" in steps[1]


def test_an_accepted_candidate_with_no_integration_is_prepared() -> None:
    steps = plan_next(_accepted())
    assert steps == [
        f"hflow integrate prepare R-1 --target {BRANCH_PLACEHOLDER} --project {PROJECT_PLACEHOLDER}"
    ], "the worktree is released only after the integration"
    with_project = plan_next(_accepted(), project_file="project.json")
    assert with_project == [
        f"hflow integrate prepare R-1 --target {BRANCH_PLACEHOLDER} "
        f"--project {shell_word(os.path.abspath('project.json'))}"
    ]


def test_a_ready_integration_is_applied_against_the_tip_it_was_checked_on() -> None:
    steps = plan_next(_accepted(integrations=(_record(IntegrationState.READY),)))
    assert _commands(steps) == [f"hflow integrate apply G-1 --expect-target {TIP}"]
    assert "reconcile" in steps[0], "the hand-off alternative apply prints is named"


@pytest.mark.parametrize(
    "state", [IntegrationState.PREPARING, IntegrationState.CHECKING, IntegrationState.APPLYING]
)
def test_an_active_integration_is_reconciled_once_its_process_is_gone(state) -> None:
    facts = _accepted(integrations=(_record(IntegrationState.SUPERSEDED, "G-0"), _record(state)))
    steps = plan_next(facts)
    assert _commands(steps) == ["hflow integrate reconcile G-1"]
    assert "exit 5" in steps[0]


@pytest.mark.parametrize(
    "state",
    [
        IntegrationState.STALE,
        IntegrationState.INTERRUPTED,
        IntegrationState.FAILED,
        IntegrationState.SUPERSEDED,
    ],
)
def test_a_settled_integration_that_can_succeed_again_is_prepared_on_its_branch(state) -> None:
    steps = plan_next(_accepted(integrations=(_record(state),)), project_file="p.json")
    assert _commands(steps) == [
        "hflow integrate prepare R-1 --target main "
        f"--project {shell_word(os.path.abspath('p.json'))}"
    ]


@pytest.mark.parametrize(
    "records",
    [
        (_record(IntegrationState.INTEGRATED),),
        # The change reached one branch; a later stale attempt does not reopen it.
        (_record(IntegrationState.INTEGRATED, "G-0"), _record(IntegrationState.STALE)),
        # Nothing HFlow can apply: the decision is the operator's, the workspace can go.
        (_record(IntegrationState.CONFLICT),),
        (_record(IntegrationState.CHECKS_FAILED),),
    ],
)
def test_an_integrated_or_undecidable_candidate_releases_its_worktree(records) -> None:
    steps = plan_next(_accepted(integrations=records))
    assert _commands(steps) == ["hflow clean R-1", "hflow clean R-1 --apply"]
    assert "preview" in steps[0]


@pytest.mark.parametrize("worktree_state", ["NONE", "REMOVED"])
def test_nothing_is_left_once_the_worktree_is_gone(worktree_state: str) -> None:
    facts = _accepted(
        integrations=(_record(IntegrationState.INTEGRATED),), worktree_state=worktree_state
    )
    assert plan_next(facts) == []


def test_an_in_place_acceptance_has_nothing_to_integrate() -> None:
    facts = RunFacts(run_id="R-1", task_state=TaskState.ACCEPTED, delivery_state="LOCAL_CANDIDATE")
    assert plan_next(facts) == []


def test_an_interrupted_cleanup_is_reconciled() -> None:
    facts = _accepted(integrations=(_record(IntegrationState.INTEGRATED),), worktree_state="REMOVING")
    assert _commands(plan_next(facts)) == ["hflow clean R-1 --reconcile"]


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (TaskState.BLOCKED, RefusalCode.VERIFICATION_FAILED.value),
        (TaskState.BLOCKED, RefusalCode.REVIEW_REJECTED.value),
        (TaskState.CANCELLED, RefusalCode.CANCELLED_BY_OPERATOR.value),
    ],
)
def test_a_finished_unaccepted_run_only_releases_its_worktree(state, code) -> None:
    facts = RunFacts(
        run_id="R-1", task_state=state, block_code=code, worktree_state="READY",
        worktree_path="/w",
    )
    assert _commands(plan_next(facts)) == ["hflow clean R-1", "hflow clean R-1 --apply"]


@pytest.mark.parametrize("code", [RefusalCode.OUTCOME_UNKNOWN, RefusalCode.OWNER_LOST])
def test_an_unknown_outcome_is_resumed_then_settled_and_cleaned_only_afterwards(code) -> None:
    facts = RunFacts(
        run_id="R-1", task_state=TaskState.BLOCKED, block_code=code.value,
        worktree_state="READY", worktree_path="/w", open_entries=("I-1",), unreconciled=True,
    )
    steps = plan_next(facts)
    assert _commands(steps) == [
        "hflow resume R-1",
        f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER}",
    ], "the workspace waits until the attempt is reconciled"
    assert "never re-dispatches" in steps[0] and "exit 5" in steps[0]

    # Reconciled with no ledger at all (a rootless run): resume is still the next step ...
    rootless = RunFacts(
        run_id="R-1", task_state=TaskState.BLOCKED, block_code=code.value, unreconciled=True
    )
    assert _commands(plan_next(rootless)) == ["hflow resume R-1"]
    # ... and once resume recorded its reconcile, the entry is settled and the worktree released.
    after = RunFacts(
        run_id="R-1", task_state=TaskState.BLOCKED, block_code=code.value,
        worktree_state="READY", worktree_path="/w", settleable_entries=(("I-1", "unknown"),),
    )
    assert _commands(plan_next(after)) == [
        f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER}",
        "hflow clean R-1",
        "hflow clean R-1 --apply",
    ]


def test_a_launch_unknown_entry_names_the_void_choice() -> None:
    facts = RunFacts(
        run_id="R-1", task_state=TaskState.CANCELLED,
        settleable_entries=(("I-1", "launch_unknown"),),
    )
    [step] = plan_next(facts)
    assert step.startswith(f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER}  # ")
    assert "--as void" in step


def test_an_ended_run_with_open_entries_is_resumed_before_anything_else() -> None:
    facts = _accepted(open_entries=("I-1",))
    assert _commands(plan_next(facts)) == [
        "hflow resume R-1",
        f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER}",
    ]


def test_a_pre_v6_owner_needs_the_operators_attestation() -> None:
    ended = RunFacts(
        run_id="R-1", task_state=TaskState.CANCELLED, open_entries=("I-1",), legacy_owner=True
    )
    assert _commands(plan_next(ended)) == [
        f"hflow resume R-1 --legacy-owner-gone --attest {ATTEST_PLACEHOLDER}",
        f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER} --legacy-owner-gone",
    ]
    # An outcome_unknown run is reconciled without waiting on an owner it cannot prove gone; the
    # settlement still needs the attestation.
    unknown = RunFacts(
        run_id="R-1", task_state=TaskState.BLOCKED, block_code=RefusalCode.OUTCOME_UNKNOWN.value,
        open_entries=("I-1",), unreconciled=True, legacy_owner=True,
    )
    assert _commands(plan_next(unknown)) == [
        "hflow resume R-1",
        f"hflow ledger settle I-1 --attest {ATTEST_PLACEHOLDER} --legacy-owner-gone",
    ]


def _every_case() -> list[RunFacts]:
    blocked = [
        RunFacts(
            run_id="R-1", task_state=TaskState.BLOCKED, block_code=code.value,
            open_entries=("I-1",), settleable_entries=(("I-2", "launch_unknown"),),
            unreconciled=True, worktree_state="READY", worktree_path="/w", legacy_owner=legacy,
        )
        for code in RefusalCode
        for legacy in (False, True)
    ]
    live = [RunFacts(run_id="R-1", task_state=state) for state in TaskState]
    integrations = [_accepted(integrations=(_record(state),)) for state in IntegrationState]
    return [*blocked, *live, *integrations, _accepted()]


def test_every_printed_command_names_the_ledger_and_none_re_runs_anything() -> None:
    """Rules 5 and 10: an ended run is reconciled, settled, integrated or released - never run
    again, and never under the same authorization."""
    for facts in _every_case():
        for step in _commands(plan_next(facts, data_dir=DATA)):
            assert step.startswith("hflow "), step
            assert step.endswith(f" --data-dir {shell_word(DATA)}"), step
            argv = step.split()
            assert argv[1] not in {"run", "prepare", "approve"}, step
            assert "--authorization-file" not in argv, step
        for step in plan_next(facts):
            assert "--data-dir" not in step, step


# --------------------------------------------------------------------------
# next_steps on stored runs, and the next: lines of the commands
# --------------------------------------------------------------------------


@pytest.fixture()
def scene(tmp_path: Path) -> Scene:
    scene = Scene(tmp_path, user_branch="work")
    yield scene
    scene.store.close()


def _next(out: str) -> list[str]:
    return [line.removeprefix("next: ") for line in out.splitlines() if line.startswith("next: ")]


def _paste(step: str) -> list[str]:
    """The argv a shell hands ``hflow`` for a printed step (its comment dropped)."""
    argv = shlex.split(step, comments=True)
    assert argv[0] == "hflow", step
    return argv[1:]


def test_the_printed_next_commands_walk_an_accepted_run_to_a_released_worktree(
    scene: Scene, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_id = scene.accept()
    data_dir = os.path.abspath(scene.data_dir)
    assert named_data_dir(scene.store) == data_dir
    data = ["--data-dir", str(scene.data_dir)]
    project_file = tmp_path / "project.json"
    project_file.write_text(scene.project.model_dump_json(), encoding="utf-8")
    worktree = Path(scene.store.get_run(run_id)["worktree_path"])
    scene.store.close()

    assert main(["status", run_id, *data]) == EXIT_OK
    assert _next(capsys.readouterr().out) == [
        f"hflow integrate prepare {run_id} --target {BRANCH_PLACEHOLDER} --project "
        f"{PROJECT_PLACEHOLDER} --data-dir {shell_word(data_dir)}"
    ]
    assert main(["status", run_id, "--json", *data]) == EXIT_OK
    raw = capsys.readouterr().out
    assert "next:" not in raw and "next" not in json.loads(raw)

    assert main(["integrate", "prepare", run_id, "--target", "main", "--project",
                 str(project_file), *data]) == EXIT_OK
    [apply] = _next(capsys.readouterr().out)
    assert main(_paste(apply)) == EXIT_OK
    preview, release = _next(capsys.readouterr().out)
    assert _paste(preview) == ["clean", run_id, "--data-dir", data_dir]
    assert _paste(release) == ["clean", run_id, "--apply", "--data-dir", data_dir]

    assert main(_paste(preview)) == EXIT_OK
    assert "decision      ALLOWED" in capsys.readouterr().out
    assert worktree.exists(), "the preview changes nothing"
    assert main(_paste(release)) == EXIT_OK
    capsys.readouterr()
    assert not worktree.exists()

    assert main(["status", run_id, *data]) == EXIT_OK
    assert _next(capsys.readouterr().out) == [], "nothing is left to do for this run"


def _interrupted_ledger(tmp_path: Path, project, task_spec: TaskSpec, project_root: Path) -> Path:
    """A run whose implementer launch started and whose controller died: blocked
    ``outcome_unknown`` with its ledger entry still open, as a crash leaves it."""
    data_dir = tmp_path / "interrupted"
    store = Store(data_dir / "hflow.sqlite")
    try:
        limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=0)
        binding = _binding(
            store, project_id=project.project_id, repo_path=project_root,
            task_id=task_spec.task_id,
        )
        store.register_root_budget(binding, limits)
        _register_authorization(store, max_submissions=4)
        run_id = _seed_ready_run(
            store, run_id="R-hint", project_id=project.project_id, spec=task_spec
        )
        assert _reserve(
            store, run_id=run_id, invocation_id="I-hint", binding=binding, limits=limits
        ).is_new
        assert mark_invocation_started(store, "I-hint") is True
        store.set_blocked(run_id, RefusalCode.OUTCOME_UNKNOWN, "seeded: the controller died")
        ledger = shell_word(os.path.abspath(data_dir))
        steps = next_steps(store, run_id)
        assert _commands(steps) == [
            f"hflow resume R-hint --data-dir {ledger}",
            f"hflow ledger settle I-hint --attest {ATTEST_PLACEHOLDER} --data-dir {ledger}",
        ]
        assert "after resume" in steps[1]
        assert next_steps(store, "R-no-such-run") == [], "a hint never raises"
    finally:
        store.close()
    return data_dir


def test_resume_and_settle_print_what_comes_next_and_json_stays_as_it_was(
    tmp_path: Path, project, task_spec: TaskSpec, project_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _interrupted_ledger(tmp_path, project, task_spec, project_root)
    data = ["--data-dir", str(data_dir)]
    ledger = shell_word(os.path.abspath(data_dir))

    assert main(["status", "R-hint", *data]) == EXIT_OK
    assert _commands(_next(capsys.readouterr().out))[0] == f"hflow resume R-hint --data-dir {ledger}"

    assert main(["resume", "R-hint", *data]) == EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "task_state: BLOCKED" in out
    assert _next(out) == [
        f"hflow ledger settle I-hint --attest {ATTEST_PLACEHOLDER} --data-dir {ledger}"
    ], "reconciled: resume is not printed again"

    assert main(["resume", "R-hint", "--json", *data]) == EXIT_BLOCKED
    raw = capsys.readouterr().out
    assert "next" not in json.loads(raw) and "next:" not in raw

    settle = _paste(_next(out)[0])
    settle[settle.index("--attest") + 1] = ATTEST
    assert main(settle) == EXIT_OK
    out = capsys.readouterr().out
    assert "prior state   unknown -> operator_settled" in out
    assert _next(out) == [], "a run with no worktree has nothing left once its entry is settled"


# --------------------------------------------------------------------------
# integrate prepare: the contract the run was admitted under, not merely its checks
# --------------------------------------------------------------------------


def test_integrate_prepare_refuses_a_contract_other_than_the_admitted_one(
    scene: Scene, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same checks, weaker ``write_deny``: the checks digest matches, the contract does not."""
    run_id = scene.accept()
    weaker = scene.project.model_copy(update={"write_deny": [".git/**"]})
    assert weaker.checks_digest() == scene.project.checks_digest()
    recorded = scene.store.admission_binding_for(run_id).project_contract_digest
    assert recorded == project_contract_digest(scene.project) != project_contract_digest(weaker)

    with pytest.raises(RefusedError) as caught:
        prepare_integration(
            scene.store, run_id=run_id, target_branch="main", project=weaker,
            runners=CheckRunners.offline_default(), data_dir=scene.data_dir,
        )
    assert caught.value.code is RefusalCode.PROJECT_MISMATCH
    assert recorded in caught.value.message
    assert scene.store.integrations_for(run_id) == [], "refused before anything is recorded"

    weaker_file = tmp_path / "weaker.json"
    weaker_file.write_text(weaker.model_dump_json(), encoding="utf-8")
    scene.store.close()
    assert main(["integrate", "prepare", run_id, "--target", "main", "--project",
                 str(weaker_file), "--data-dir", str(scene.data_dir)]) == EXIT_REFUSED
    assert "project_mismatch" in capsys.readouterr().err
    assert scene.tip() == scene.base


def test_status_compares_the_project_root_the_run_recorded_by_default(
    controller, run_request, project_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An in-place run's drift check used to read the current directory unless told otherwise."""
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = ["--data-dir", str(tmp_path / "data")]

    for verb in ("status", "report"):
        assert main([verb, outcome.run_id, *data]) == EXIT_OK
        assert "workspace still matches the accepted fingerprint" in capsys.readouterr().out
    assert main(["status", outcome.run_id, "--project-root", str(elsewhere), *data]) == EXIT_OK
    assert "DRIFTED" in capsys.readouterr().out, "an explicit --project-root still wins"


@pytest.mark.parametrize(
    "broken",
    [
        # Path.home() raises RuntimeError on POSIX when no home can be named; the default data
        # directory derives from it.
        ("default_data_dir", RuntimeError("Could not determine home directory.")),
        ("plan_next", OSError("anything a hint depends on")),
        ("run_facts", json.JSONDecodeError("unreadable receipt", "{", 0)),
    ],
    ids=["no-home", "plan", "facts"],
)
def test_a_hint_that_cannot_be_worked_out_is_no_hint_and_never_an_error(
    tmp_path: Path, project, task_spec: TaskSpec, monkeypatch: pytest.MonkeyPatch, broken
) -> None:
    """``run`` prints hints after it dispatched: a hint must never change a command's outcome."""
    import hflow.hints as hints_module

    store = Store(tmp_path / "ledger" / "hflow.sqlite")
    try:
        run_id = _seed_ready_run(store, run_id="R-ready", project_id=project.project_id,
                                 spec=task_spec)
        assert _commands(next_steps(store, run_id))[0].startswith("hflow status R-ready")
        name, error = broken

        def fail(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise error

        monkeypatch.setattr(hints_module, name, fail)
        assert next_steps(store, run_id) == []
    finally:
        store.close()


def test_next_lines_are_one_per_step() -> None:
    assert next_lines(["hflow status R-1", "hflow clean R-1"]) == [
        "next: hflow status R-1",
        "next: hflow clean R-1",
    ]
