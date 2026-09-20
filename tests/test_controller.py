"""Controller integration tests: the offline vertical slice and its failure modes.

These are the behaviours the bootstrap document makes non-negotiable. Each test is
fully offline: the only driver is ``FakeDriver`` and the only check runner is a
deterministic fake, so a red test is a controller defect, never model variance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import (
    AttemptState,
    CheckPhase,
    DeliveryState,
    EvidenceStatus,
    InvocationOutcome,
    IsolationLevel,
    RefusalCode,
    RefusedError,
    ReviewOutput,
    ReviewResult,
    RunRequest,
    Scope,
    TaskState,
    VerificationResult,
)
from hflow.controller import Controller, inspect_run
from hflow.drivers.fake import FakeScript
from hflow.ids import utc_now
from hflow.report import status_text
from hflow.store import Store, StoreError
from hflow.verify import CheckRunners, FakeCheckRunner

from .conftest import unit_only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _seed_live_run(
    store: Store, request: RunRequest, *, turn_limit: int, reserve: int
) -> str:
    """Create a claimed run with a chosen ceiling, optionally pre-consuming budget.

    Used to reach budget states deterministically instead of hoping a real run
    happens to run out at the right moment.
    """
    spec = request.task
    run = store.create_run(
        run_id="R-seeded",
        project_id=request.project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="test-build",
        checks_digest=request.project.checks_digest(),
        turn_limit=turn_limit,
        repair_limit=spec.budget.max_repair_cycles,
    )
    run_id = run["run_id"]
    assert store.claim_run(run_id, "local-controller")
    if reserve:
        store.reserve_turn(run_id, "local-controller", turns=reserve)
    store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)
    return run_id


def _attempt(store: Store, run_id: str) -> dict:
    row = store.open_attempt(run_id)
    assert row is not None
    return dict(row)


def _implementer_turns(driver) -> list:
    """Invocations that could edit the workspace. Review is a separate, read-only pass."""
    return [request for request in driver.started if request.role == "implementer"]


# --------------------------------------------------------------------------
# 1. the slice works end to end
# --------------------------------------------------------------------------


def test_normal_completion_produces_controller_generated_receipt(
    controller: Controller, store: Store, driver, run_request: RunRequest
) -> None:
    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.ACCEPTED
    assert outcome.delivery_state is DeliveryState.LOCAL_CANDIDATE
    assert outcome.receipt is not None, outcome.block_reason
    receipt = outcome.receipt

    # The receipt is generated and persisted by the controller, not by the worker.
    stored = json.loads(store.get_run(outcome.run_id)["receipt_json"])
    assert stored["task_state"] == "ACCEPTED"
    assert stored["run_id"] == outcome.run_id
    assert receipt.harness_outcome is InvocationOutcome.COMPLETED
    assert receipt.verification.status == "passed"
    assert receipt.verification.evidence_ids
    assert receipt.review.status == "accepted"
    assert receipt.candidate.tree_hash.startswith("sha256:")
    assert receipt.task_revision == 1
    assert receipt.runtime_build == "test-build"

    # Usage keeps the three counters apart; unobservable billing stays null.
    assert receipt.usage.controller_turns_reserved == 2  # implement + review
    assert receipt.usage.controller_turns_observed == 1
    assert receipt.usage.provider_billed_tokens is None
    assert receipt.usage.provider_cost is None

    workspace_file = run_request.project_root / "src" / "parser.py"
    assert "if not text:" in workspace_file.read_text(encoding="utf-8")

    # Two invocations: the implementer process and a separate reviewer process
    # (the reviewer is not a continuation of the implementer session).
    assert [request.role for request in driver.started] == ["implementer", "reviewer"]


def test_slice_runs_once_per_check_and_status_costs_no_model_calls(
    controller: Controller, store: Store, check_runner: FakeCheckRunner, run_request: RunRequest
) -> None:
    outcome = controller.run_task(run_request)
    assert check_runner.calls == ["unit", "docs-check"]
    assert outcome.task_state is TaskState.ACCEPTED

    inspection = inspect_run(store, outcome.run_id)
    assert inspection.model_calls_made == 0
    assert inspection.run.task_state is TaskState.ACCEPTED
    assert inspection.receipt is not None
    assert len(inspection.evidence) == 3  # unit, docs-check, review
    assert {e.kind for e in inspection.evidence} == {"verification", "review"}


def test_invocation_counts_separate_implementer_from_reviewer(
    controller: Controller, store: Store, driver, run_request: RunRequest
) -> None:
    """Two reserved turns means two driver processes, counted per role."""
    outcome = controller.run_task(run_request)

    assert [request.role for request in driver.started] == ["implementer", "reviewer"]
    assert outcome.implementer_invocations == 1
    assert outcome.reviewer_invocations == 1
    assert outcome.driver_invocations == 2
    assert outcome.turns_reserved == 2
    # Turn 1 was dispatched and observed; turn 2 was reserved for the review process.
    assert outcome.receipt is not None
    assert outcome.receipt.usage.controller_turns_reserved == 2

    attempt = _attempt(store, outcome.run_id)
    assert attempt["invocation_id"] != attempt["review_invocation_id"]
    implementer, reviewer = store.invocation_counts(outcome.run_id)
    assert (implementer, reviewer) == (1, 1)


def test_historical_accepted_is_flagged_when_the_workspace_drifts(
    controller: Controller, store: Store, driver, run_request: RunRequest
) -> None:
    """A stored ACCEPTED must not read as verification of the current files."""
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED
    assert outcome.workspace_matches_receipt is True

    target = run_request.project_root / "src" / "parser.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# manual drift\n", encoding="utf-8")

    drifted = inspect_run(store, outcome.run_id, project_root=run_request.project_root)
    assert drifted.run.task_state is TaskState.ACCEPTED, "history is not rewritten"
    assert drifted.run.workspace_matches_receipt is False
    assert "DRIFTED" in status_text(drifted)

    # Re-submitting the identical spec still returns the historical run, and the
    # duplicate path keeps reporting the drift instead of implying a fresh pass.
    again = controller.run_task(run_request)
    assert again.run_id == outcome.run_id
    assert again.workspace_matches_receipt is False
    assert len(_implementer_turns(driver)) == 1


# --------------------------------------------------------------------------
# 2. identical submission does not dispatch twice (A01)
# --------------------------------------------------------------------------


def test_identical_spec_is_not_dispatched_twice(
    controller: Controller, driver, run_request: RunRequest
) -> None:
    first = controller.run_task(run_request)
    second = controller.run_task(run_request)

    assert first.receipt is not None
    assert second.run_id == first.run_id
    assert second.task_state is TaskState.ACCEPTED
    assert second.receipt is not None
    assert second.receipt.attempt_id == first.receipt.attempt_id
    assert len(_implementer_turns(driver)) == 1, (
        "the same TaskSpec must not buy a second worker turn"
    )
    assert any("identical TaskSpec" in note for note in second.notes)


def test_identical_spec_inside_a_loop_never_creates_a_second_accepted_run(
    controller: Controller, store: Store, driver, run_request: RunRequest
) -> None:
    """Acceptance A01 repeated: reruns must not accumulate accepted results."""
    outcomes = [controller.run_task(run_request) for _ in range(3)]
    assert {outcome.run_id for outcome in outcomes} == {outcomes[0].run_id}
    assert len(_implementer_turns(driver)) == 1
    accepted = [
        row for row in store.list_runs() if row["task_state"] == TaskState.ACCEPTED.value
    ]
    assert len(accepted) == 1


# --------------------------------------------------------------------------
# 3. budget gate is in front of the driver (A03, plan 10.3)
# --------------------------------------------------------------------------


def test_insufficient_budget_blocks_before_the_driver_starts(
    controller: Controller, driver, run_request: RunRequest, store: Store
) -> None:
    run_id = _seed_live_run(store, run_request, turn_limit=1, reserve=1)

    outcome = controller.run_task(run_request)

    assert outcome.run_id == run_id
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED
    assert "budget exhausted" in (outcome.block_reason or "")
    assert driver.started == [], "no worker process may start without reserved budget"
    assert outcome.receipt is None
    assert store.attempts_for(run_id) == []
    assert store.turns_remaining(run_id) == 0


def test_review_is_refused_when_only_the_implementation_turn_fits(
    controller: Controller, driver, run_request: RunRequest, store: Store
) -> None:
    """Having one turn left is not enough if acceptance also requires a review turn."""
    run_id = _seed_live_run(store, run_request, turn_limit=3, reserve=2)

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED
    # The implementation turn ran; the review turn was never bought.
    assert len(driver.started) == 1
    assert store.turns_remaining(run_id) == 0


# --------------------------------------------------------------------------
# 4. exit code 0 is not acceptance (A06)
# --------------------------------------------------------------------------


def test_successful_process_with_failing_verification_is_not_accepted(
    controller: Controller,
    store: Store,
    check_runner: FakeCheckRunner,
    driver,
    run_request: RunRequest,
) -> None:
    check_runner.verdicts = {"unit": EvidenceStatus.FAILED}

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.VERIFICATION_FAILED
    assert outcome.receipt is None
    assert "acceptance is not met" in (outcome.block_reason or "")
    assert len(driver.started) == 1, "a verification failure must not silently re-run the worker"


def test_candidate_change_after_verification_invalidates_evidence(
    controller: Controller,
    store: Store,
    driver,
    run_request: RunRequest,
    project,
    check_runner: FakeCheckRunner,
) -> None:
    """Acceptance A09: evidence is bound to the candidate fingerprint it was produced on."""
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED
    attempt = _attempt(store, outcome.run_id)

    # The run is accepted; now simulate a second acceptance attempt against a mutated
    # candidate, which must be refused as stale rather than reused.
    target = run_request.project_root / "src" / "parser.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    verification = VerificationResult(
        status="passed",
        evidence_ids=[e["evidence_id"] for e in store.evidence_for(outcome.run_id, "verification")],
    )
    stale = controller._accept(
        run_id=outcome.run_id,
        attempt_id=attempt["attempt_id"],
        spec=run_request.task,
        project_root=run_request.project_root,
        verification=verification,
        review=ReviewResult(status="accepted", isolation=IsolationLevel.PROMPT_ONLY),
        observed_turns=1,
        base_ref="base",
        limitations=[],
    )
    assert "no receipt" not in " ".join(stale.notes)
    assert stale.block_code is RefusalCode.EVIDENCE_STALE
    assert "no longer applies" in (stale.block_reason or "")


# --------------------------------------------------------------------------
# 5. late results cannot overwrite a newer attempt (A05)
# --------------------------------------------------------------------------


def test_late_result_from_an_old_attempt_is_refused(
    controller: Controller, store: Store, run_request: RunRequest
) -> None:
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED
    attempt = _attempt(store, outcome.run_id)
    assert attempt["state"] == AttemptState.SUCCEEDED.value

    # Model "the run already moved on": a newer revision owns the current attempt slot.
    with store.transaction() as conn:
        conn.execute(
            "UPDATE runs SET current_attempt_id = ?, task_revision = ? WHERE run_id = ?",
            ("A-newer", 2, outcome.run_id),
        )

    with pytest.raises(StoreError) as excinfo:
        store.finish_attempt(
            run_id=outcome.run_id,
            attempt_id=attempt["attempt_id"],
            state=AttemptState.SUCCEEDED,
            outcome=InvocationOutcome.COMPLETED,
            result={"late": True},
        )
    assert "stale result rejected" in str(excinfo.value)

    row = store.get_run(outcome.run_id)
    assert row["task_state"] == TaskState.ACCEPTED.value
    assert row["task_revision"] == 2
    assert json.loads(row["receipt_json"])["attempt_id"] == attempt["attempt_id"]


def test_newer_revision_and_attempt_supersede_the_previous_one(
    controller: Controller, store: Store, driver, run_request: RunRequest, task_spec
) -> None:
    """A revised task is a new run; the old attempt stays historical, not overwritten."""
    first = controller.run_task(run_request)
    revised = task_spec.model_copy(update={"revision": 2, "goal": "narrower fix after review"})
    second_request = run_request.model_copy(update={"task": revised})

    second = controller.run_task(second_request)

    assert second.run_id != first.run_id
    assert second.task_state is TaskState.ACCEPTED
    assert second.receipt is not None and second.receipt.task_revision == 2
    first_row = store.get_run(first.run_id)
    assert json.loads(first_row["receipt_json"])["task_revision"] == 1
    assert len(_implementer_turns(driver)) == 2, "a revised task is a genuinely new worker turn"


# --------------------------------------------------------------------------
# 6. an unknown outcome is not retried automatically (A04)
# --------------------------------------------------------------------------


def test_unknown_outcome_blocks_and_never_re_dispatches(
    controller: Controller,
    store: Store,
    driver,
    run_request: RunRequest,
    fake_script: FakeScript,
) -> None:
    fake_script.unknown_invocations = 1

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN
    attempt = _attempt(store, outcome.run_id)
    assert attempt["state"] == AttemptState.OUTCOME_UNKNOWN.value
    assert attempt["reservation_id"], "the reservation must survive: the model may have run"
    assert len(driver.started) == 1

    again = controller.run_task(run_request)
    assert again.run_id == outcome.run_id
    assert len(driver.started) == 1, "re-submitting the same spec must not re-dispatch"

    resumed = controller.resume(outcome.run_id)
    assert driver.reconciled, "resume must reconcile the interrupted attempt"
    assert len(driver.started) == 1, "resume must not start a new invocation"
    assert resumed.task_state is TaskState.BLOCKED


def test_resume_does_not_redispatch_after_a_driver_failure(
    controller: Controller, driver, run_request: RunRequest, fake_script: FakeScript
) -> None:
    fake_script.outcome = InvocationOutcome.FAILED
    fake_script.error_code = "transport_not_entered"
    fake_script.error_message = "server never accepted the task"

    outcome = controller.run_task(run_request)
    assert outcome.block_code is RefusalCode.DRIVER_FAILED

    controller.resume(outcome.run_id)
    assert len(driver.started) == 1


# --------------------------------------------------------------------------
# 7. state + budget move in one transaction
# --------------------------------------------------------------------------


def test_reservation_and_attempt_are_written_atomically(
    store: Store, run_request: RunRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If dispatch intent cannot be recorded, the reservation must not survive."""
    run_id = _seed_live_run(store, run_request, turn_limit=4, reserve=0)
    boot_count = {"value": 0}

    def explode(**_kwargs: object) -> None:
        boot_count["value"] += 1
        raise StoreError("simulated crash while recording dispatch intent")

    monkeypatch.setattr(store, "create_attempt", explode)

    with pytest.raises(StoreError):
        store.dispatch_attempt(
            run_id=run_id,
            controller_id="local-controller",
            attempt_id="A-atomic",
            role="implementer",
            reservation_id="B-atomic",
            reserved_turns=1,
            reservation_expires_at=utc_now(),
        )

    assert boot_count["value"] == 1
    row = store.get_run(run_id)
    assert row["turns_reserved"] == 0, "the reservation must roll back with the attempt"
    assert row["turns_remaining"] == 4
    assert row["task_state"] == TaskState.READY.value
    assert store.attempts_for(run_id) == []


def test_claim_is_exclusive_across_two_connections(
    tmp_path: Path, run_request: RunRequest, store: Store
) -> None:
    """Acceptance A02: two controllers may not both own the same run."""
    run_id = _seed_live_run(store, run_request, turn_limit=4, reserve=0)
    other = Store(tmp_path / "data" / "hflow.sqlite")
    try:
        assert other.claim_run(run_id, "controller-two") is False
        assert other.get_run(run_id)["claimed_by"] == "local-controller"
        with pytest.raises(StoreError):
            other.reserve_turn(run_id, "controller-two", turns=1)
    finally:
        other.close()


def test_budget_ceiling_is_enforced_by_the_database_itself(
    store: Store, run_request: RunRequest
) -> None:
    run_id = _seed_live_run(store, run_request, turn_limit=1, reserve=0)
    store.reserve_turn(run_id, "local-controller", turns=1)
    with pytest.raises(StoreError):
        store.reserve_turn(run_id, "local-controller", turns=1)
    with pytest.raises(StoreError):
        store.release_reservation(run_id, 5)


# --------------------------------------------------------------------------
# extra refusal paths that share the same gate
# --------------------------------------------------------------------------


def test_review_rejection_blocks_acceptance(
    controller: Controller,
    driver,
    run_request: RunRequest,
    fake_script: FakeScript,
) -> None:
    fake_script.review = ReviewOutput(
        verdict="changes_requested",
        findings=[
            {
                "id": "F-1",
                "location": "src/parser.py:2",
                "impact": "empty input still reaches an invalid index",
                "evidence": "reproduced against AC-1",
                "required_fix": "return the agreed empty result first",
            }
        ],
    )

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED
    assert outcome.receipt is None


def test_reviewer_isolation_is_recorded_from_enforcement_not_claims(
    controller: Controller,
    store: Store,
    driver,
    run_request: RunRequest,
    fake_script: FakeScript,
) -> None:
    """Acceptance A08: text that says 'read-only' never upgrades the recorded level."""
    fake_script.review = ReviewOutput(verdict="accepted", findings=[])
    fake_script.claimed_isolation = "readonly_enforced"

    outcome = controller.run_task(run_request)

    assert outcome.receipt is not None
    assert outcome.receipt.review.isolation is IsolationLevel.PROMPT_ONLY
    review_evidence = store.evidence_for(outcome.run_id, "review")
    assert len(review_evidence) == 1
    assert "readonly_enforced" not in review_evidence[0]["detail"]


def test_write_outside_declared_scope_refuses_acceptance(
    controller: Controller,
    store: Store,
    driver,
    run_request: RunRequest,
    fake_script: FakeScript,
) -> None:
    fake_script.write_plan = {
        "src/parser.py": "def parse(text):\n    return text or None\n",
        "src/hidden/backdoor.py": "import os\n",
    }

    outcome = controller.run_task(run_request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.SCOPE_VIOLATION
    assert outcome.receipt is None
    # The evidence of the violation is the file itself, not a claim about it.
    assert (run_request.project_root / "src" / "hidden" / "backdoor.py").is_file()
    assert (run_request.project_root / "src" / "hidden" / "backdoor.py").read_text(
        encoding="utf-8"
    ) == "import os\n"


def test_delivery_never_exceeds_local_candidate(
    controller: Controller, store: Store, run_request: RunRequest
) -> None:
    outcome = controller.run_task(run_request)
    assert outcome.delivery_state is DeliveryState.LOCAL_CANDIDATE
    assert store.get_run(outcome.run_id)["delivery_state"] == "LOCAL_CANDIDATE"


def test_checks_digest_change_invalidates_acceptance(
    controller: Controller, store: Store, driver, run_request: RunRequest
) -> None:
    """Acceptance A11: a different approved command set means the evidence is stale."""
    outcome = controller.run_task(run_request)
    assert outcome.task_state is TaskState.ACCEPTED
    attempt = _attempt(store, outcome.run_id)

    with store.transaction() as conn:
        conn.execute(
            "UPDATE runs SET checks_digest = ? WHERE run_id = ?", ("sha256:changed", outcome.run_id)
        )
        conn.execute(
            "UPDATE runs SET task_state = ? WHERE run_id = ?",
            (TaskState.CHECKING.value, outcome.run_id),
        )

    with pytest.raises(StoreError) as excinfo:
        store.finalize_acceptance(
            outcome.run_id,
            outcome.receipt,
            checks_digest=run_request.project.checks_digest(),
        )
    assert "checks digest" in str(excinfo.value)
    assert attempt["state"] == AttemptState.SUCCEEDED.value


def test_check_runner_without_a_kind_is_refused_not_guessed(
    store: Store, project, task_spec, project_root: Path, driver
) -> None:
    from hflow.contracts import CheckDef

    project_with_command = project.model_copy(
        update={"checks": [CheckDef(id="unit", kind="command", argv=["python", "-c", "pass"])]}
    )
    controller = Controller(
        store,
        driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": FakeCheckRunner()}),  # no 'command' runner registered
    )
    request = RunRequest(
        task=unit_only(task_spec),
        project=project_with_command,
        project_root=project_root,
        workspace_root=project_root,
    )

    outcome = controller.run_task(request)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.VERIFICATION_FAILED
    assert "cannot execute" in (outcome.block_reason or "")


def test_command_check_runs_through_the_real_runner(
    store: Store, task_spec, project_root: Path, project, driver
) -> None:
    """One command check is exercised on purpose: exit 0 means passed, non-zero failed."""
    from hflow.contracts import CheckDef

    project_with_command = project.model_copy(
        update={"checks": [CheckDef(id="unit", kind="command", argv=["python", "-c", "raise SystemExit(0)"])]}
    )
    controller = Controller(
        store, driver, controller_build="test-build", runners=CheckRunners.offline_default()
    )
    request = RunRequest(
        task=unit_only(task_spec),
        project=project_with_command,
        project_root=project_root,
        workspace_root=project_root,
    )

    outcome = controller.run_task(request)

    assert outcome.task_state is TaskState.ACCEPTED
    evidence = store.evidence_for(outcome.run_id, "verification")
    assert evidence[0]["exit_code"] == 0
    assert evidence[0]["command_json"] != "[]"


def test_phase_field_tracks_the_checking_stage(
    controller: Controller, store: Store, run_request: RunRequest
) -> None:
    """CHECKING carries its stage in `phase` instead of inventing new global states."""
    seen: list[str] = []
    original = store.advance_to_checking

    def spy(*, run_id: str, attempt_id: str, phase: CheckPhase):
        row = original(run_id=run_id, attempt_id=attempt_id, phase=phase)
        seen.append(row["phase"])
        return row

    store.advance_to_checking = spy  # type: ignore[method-assign]
    outcome = controller.run_task(run_request)

    assert seen == [CheckPhase.VERIFICATION.value]
    # Acceptance clears the phase: CHECKING is over, and ACCEPTED has no sub-stage.
    assert store.get_run(outcome.run_id)["phase"] is None


def test_scope_denied_path_is_refused_at_admission(
    run_request: RunRequest, task_spec, project_root: Path
) -> None:
    """A denied path inside the allow list is refused up front, not silently dropped."""
    from hflow.admission import assert_admissible

    spec = task_spec.model_copy(
        update={"scope": Scope(write_allow=["src/parser.py", ".github/x.yml"])}
    )
    with pytest.raises(RefusedError) as excinfo:
        assert_admissible(spec, run_request.project, project_root)
    assert excinfo.value.code is RefusalCode.SCOPE_VIOLATION
