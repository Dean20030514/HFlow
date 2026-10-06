"""A settled launch must reach the same budget decision at admission and dispatch."""

import json
from pathlib import Path

import pytest

from hflow.authorization import AuthorizationBinding, AuthorizationRecord
from hflow.cli import EXIT_OK, main
from hflow.contracts import InvocationStartState, RefusalCode, RefusedError, RootBudgetBinding, RootBudgetLimits, TaskState
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import write_project, write_task
from .test_batch_e_repair import (
    FIXED_SOURCE, FailingOnceThenPassing, RepairingDriver, _git, _policy, _project,
    _request, _spec, sample_repo as sample_repo,
)


class InterruptedLaunch(FakeDriver):
    """Interrupt the requested launch before any spawn fact, without creating a process."""

    def __init__(self, repo, store):
        super().__init__(repo)
        self.store = store

    def start(self, request):  # noqa: ANN001, ANN201 - driver protocol shape
        assert self.store.invocation(request.invocation_id).state is InvocationStartState.REQUESTED
        self.started.append(request)
        raise KeyboardInterrupt


def _controller(store, repo, project, spec, binding, limits, authorization_id, driver, runner):
    authorization = AuthorizationRecord(
        authorization_id=authorization_id,
        user_text="Offline fixture: one bounded revision with its own allowance.",
        authorized_at="2026-10-05T00:00:00Z",
        max_top_level_submissions=4,
        binding=AuthorizationBinding(
            mode="m2-live-change", driver="fake", project_id=project.project_id,
            repo_path=str(repo), base_commit=spec.workspace.base_commit,
            spec_digest=spec.spec_digest(), spec_path=str(repo.parent / "task.json"),
            root_budget=binding,
        ),
        root_limits=limits,
    )
    return Controller(
        store, driver, controller_build="void-revision-test", production=False,
        runners=CheckRunners({"fake": runner}), authorization=authorization,
        root_binding=binding, root_limits=limits,
    )


@pytest.mark.parametrize(
    "settled_as,repairs,armed,continuation,via_cli,allowed",
    [
        ("void", 0, False, False, False, True),
        ("void", 0, False, False, True, True),
        ("void", 0, False, True, False, True),
        ("void", 1, True, False, False, True),
        ("void", 1, True, True, False, True),
        ("consumed", 0, False, False, False, False),
        ("consumed", 1, False, False, False, True),
        ("consumed", 1, True, False, False, False),
    ],
)
def test_settlement_then_fresh_revision_preserves_history_and_obeys_budget(
    store, sample_repo: Path, capsys, settled_as, repairs, armed, continuation, via_cli, allowed
):
    project = _project()
    base = _git(sample_repo, "rev-parse", "HEAD").strip()
    first_spec = _spec(policy=None, base_commit=base)
    binding = RootBudgetBinding.derive(
        project_id=project.project_id, repo_path=str(sample_repo),
        task_id=first_spec.task_id, ledger_path=store.path,
    )
    limits = RootBudgetLimits(max_top_level_submissions=4, max_repairs=repairs)
    first_driver = InterruptedLaunch(sample_repo, store)
    first = _controller(
        store, sample_repo, project, first_spec, binding, limits, "AUTH-first",
        first_driver, FakeCheckRunner(),
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            first.run_task(_request(project=project, spec=first_spec, project_root=sample_repo))
        old_run = store.find_run_by_spec_digest(project.project_id, first_spec.spec_digest())
        run_id = old_run["run_id"]
        entries = store.invocations_for(run_id)
        assert len(entries) == 1 and entries[0].state is InvocationStartState.LAUNCH_UNKNOWN
        assert store.root_budget_view(binding.root_id).used_top_level_submissions == 1
        assert len(first_driver.started) == 1
        first.resume(run_id)
        assert store.invocation(entries[0].invocation_id).state is InvocationStartState.LAUNCH_UNKNOWN
        assert len(first_driver.started) == 1, "reconciliation must not replay the prompt"
    finally:
        first.close()

    settlement = store.settle_by_operator(
        entries[0].invocation_id, settled_as=settled_as, attested_by="offline-fixture",
        attestation="The offline driver created no process and invoked no model.",
    )
    prior_run = dict(store.get_run(run_id))
    prior_attempts = [dict(row) for row in store.attempts_for(run_id)]
    prior_authorization = store.authorization_state("AUTH-first")
    prior_invocations = store.invocations_for(run_id)
    prior_root = store.root_budget_view(binding.root_id)
    assert prior_run["task_state"] == TaskState.BLOCKED.value
    assert prior_authorization["used_top_level_submissions"] == 1
    assert prior_root.used_top_level_submissions == (0 if settled_as == "void" else 1)
    assert prior_root.used_repairs == 0
    assert bool(store.charged_implementers_for_root(binding.root_id)) == (settled_as == "consumed")

    spec = _spec(policy=_policy() if armed else None, base_commit=base, revision=2)
    driver = (
        RepairingDriver(sample_repo, first_plan={}, repair_plan={"src/parser.py": FIXED_SOURCE})
        if armed else FakeDriver(sample_repo, FakeScript(write_plan={"src/parser.py": FIXED_SOURCE}))
    )
    runner = FailingOnceThenPassing() if armed else FakeCheckRunner()
    next_controller = _controller(
        store, sample_repo, project, spec, binding, limits, "AUTH-next", driver, runner,
    )
    request = _request(project=project, spec=spec, project_root=sample_repo)
    try:
        if continuation:
            admitted_id = "R-next-admitted"
            store.create_run(
                run_id=admitted_id, project_id=project.project_id, spec=spec,
                spec_digest=spec.spec_digest(), controller_build="void-revision-test",
                checks_digest=project.checks_digest(), turn_limit=4, repair_limit=1,
                admission_binding=next_controller._admission_binding(admitted_id, request),
            )
        if allowed:
            if via_cli:
                task_file = write_task(sample_repo.parent / "task.json", spec)
                project_file = write_project(sample_repo.parent / "project.json", project)
                root_file = sample_repo.parent / "root.json"
                root_file.write_text(json.dumps({"limits": limits.model_dump(mode="json")}), encoding="utf-8")
                plan_file = sample_repo.parent / "write-plan.json"
                plan_file.write_text(json.dumps({"src/parser.py": FIXED_SOURCE}), encoding="utf-8")
                code = main([
                    "run", "--task", str(task_file), "--project", str(project_file),
                    "--project-root", str(sample_repo), "--data-dir", str(store.path.parent),
                    "--root-budget-file", str(root_file), "--fake-write-plan", str(plan_file), "--json",
                ])
                payload = json.loads(capsys.readouterr().out)
                assert code == EXIT_OK, payload
                assert payload["task_state"] == TaskState.ACCEPTED.value
                outcome_run_id = payload["run_id"]
            else:
                outcome = next_controller.run_task(request)
                assert outcome.task_state is TaskState.ACCEPTED
                outcome_run_id = outcome.run_id
                if continuation:
                    assert outcome_run_id == admitted_id
            new_entries = store.invocations_for(outcome_run_id)
            assert [entry.role for entry in new_entries] == (
                ["implementer", "implementer", "reviewer"] if armed
                else ["implementer", "reviewer"]
            )
            assert new_entries[0].is_repair == (settled_as == "consumed")
            if armed:
                assert new_entries[1].is_repair is True
            root = store.root_budget_view(binding.root_id)
            assert root.used_top_level_submissions == prior_root.used_top_level_submissions + len(new_entries)
            assert root.used_repairs == int(armed or settled_as == "consumed")
            new_authorization = store.authorization_state(new_entries[0].authorization_id)
            assert new_authorization["used_top_level_submissions"] == len(new_entries)
            if via_cli:
                assert new_authorization["origin"] == "cli_offline_synthetic"
        else:
            with pytest.raises(RefusedError) as refused:
                next_controller.run_task(request)
            assert refused.value.code is RefusalCode.BUDGET_EXHAUSTED
            assert f"needs {2 if armed else 1} more" in refused.value.message
            assert driver.started == []
            assert store.root_budget_view(binding.root_id) == prior_root
            assert store.authorization_state("AUTH-next") is None
            assert store.find_run_by_spec_digest(project.project_id, spec.spec_digest()) is None
    finally:
        next_controller.close()

    assert dict(store.get_run(run_id)) == prior_run
    assert [dict(row) for row in store.attempts_for(run_id)] == prior_attempts
    assert store.authorization_state("AUTH-first") == prior_authorization
    assert store.invocations_for(run_id) == prior_invocations
    assert store.settlement_for(entries[0].invocation_id) == settlement
    root = store.root_budget_view(binding.root_id)
    assert root.first_dispatch_at == prior_root.first_dispatch_at
    assert root.deadline_at == prior_root.deadline_at
