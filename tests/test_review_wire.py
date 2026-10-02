"""The production wire: a reviewer's structured verdict must reach acceptance.

Everything here runs offline. "Production path" means the real :class:`AcpxDshDriver`
launched through a client that reproduces acpx's contract, and the real controller - not a
fake driver and not a pre-filled ``InvocationResult``. That distinction is the point of the
file: the defect this covers was precisely that every ``collect()`` returned
``review=None``, so a valid reviewer verdict could never reach the acceptance checks.

``test_structured_review_reaches_the_controller`` fails on that unpatched code and passes
once the driver decodes the verdict from the reviewer's own final message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hflow.drivers.acpx_dsh as driver_module
import hflow.review as review_module
from hflow.contracts import (
    DeliveryState,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    InvocationStartState,
    RefusalCode,
    ReviewOutput,
    RunRequest,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.review import REVIEW_INPUT_PREFIX
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from . import test_batch_e_dispatch as dispatch_helpers
from .test_driver_acpx_dsh import DriverHarness

#: The reviewer's answer as the recorded live reviewer wrote it: prose, a code sample, then
#: one fenced verdict object. Parsed from the stub's own output, never injected directly.
REVIEW_GOAL = (
    "Review the frozen candidate against the acceptance criteria. "
    "Report findings against the candidate fingerprint; do not edit files."
)
IMPLEMENTER_GOAL = "Fix the empty-input crash in src/parser.py and keep valid input working"


def structured_harness(
    tmp_path: Path,
    *,
    review_mode: str = "fenced",
    message_ids: bool = True,
    terminal_responses: str | None = None,
    reviewer_terminal_responses: str | None = None,
) -> DriverHarness:
    """A driver whose agent answers by role, through the production launch path.

    The stub's marker files are moved *outside* the workspace the controller snapshots: they
    are harness scaffolding, and leaving them inside would be refused as an out-of-scope
    write - a different failure than the one these tests are about.

    ``terminal_responses`` replaces the stub's terminal prompt response with ``id:stopReason``
    pairs (the client sends ``session/prompt`` as id 2); ``reviewer_terminal_responses`` does the
    same for the reviewer only.
    """
    harness = DriverHarness(tmp_path, "structured")
    scratch = (tmp_path / "stub-scratch").resolve()
    harness.driver.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    harness.driver.extra_env["STUB_SPAWN_LOG"] = str(scratch / "agent-spawns.jsonl")
    harness.driver.extra_env["STUB_REVIEW_MODE"] = review_mode
    harness.driver.extra_env["STUB_MESSAGE_IDS"] = "1" if message_ids else "0"
    harness.driver.extra_env["STUB_IMPLEMENTER_PATH"] = "src/parser.py"
    if terminal_responses is not None:
        harness.driver.extra_env["STUB_TERMINAL_RESPONSES"] = terminal_responses
    if reviewer_terminal_responses is not None:
        harness.driver.extra_env["STUB_REVIEWER_TERMINAL_RESPONSES"] = reviewer_terminal_responses
    return harness


def run_invocation(harness: DriverHarness, role: str, goal: str, invocation_id: str = "I-1"):
    """Start one invocation through the driver and fold it with the real ``collect``."""
    request = InvocationRequest(
        invocation_id=invocation_id,
        attempt_id="A-1",
        run_id="R-1",
        role=role,
        task_id="T-1",
        task_revision=1,
        goal=goal,
        acceptance=[],
        write_allow=["src/parser.py"],
        write_deny=[],
        workspace=str(harness.workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        # A reviewer is read-only, exactly as the controller requests it.
        writes_allowed=False,
        data_dir=str(harness.data_dir),
    )
    handle = harness.driver.start_handle(request)
    for _ in harness.driver.observe(handle):
        pass
    return harness.driver.collect(handle), handle, request


def controller_for(harness: DriverHarness, data_dir: Path) -> tuple[Controller, Store, FakeCheckRunner]:
    store = Store(data_dir / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=harness.data_dir,
        # The transport is the production one, but the *run* is offline: its approved checks
        # are `kind=fake`. Saying so explicitly is what keeps the production rules (real
        # checks, isolated worktree, effective write permission) from being silently disabled
        # by a test that only wanted a working wire.
        production=False,
    )
    return controller, store, runner


# --------------------------------------------------------------------------
# 1. the wire itself
# --------------------------------------------------------------------------


def test_a_reviewer_verdict_is_decoded_from_the_final_message(tmp_path: Path) -> None:
    """The regression: ``collect`` must carry the verdict, not ``review=None``."""
    harness = structured_harness(tmp_path)
    result, handle, request = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert request.role == "reviewer"
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is not None, "the reviewer's structured verdict was discarded"
    assert result.review == ReviewOutput(
        verdict="accepted",
        findings=[{"id": "AC-1", "status": "pass", "detail": "empty input returns the agreed result"}],
    )
    assert f"{REVIEW_INPUT_PREFIX}decoded: accepted" in " ".join(result.limitations)
    harness.driver.release(handle.invocation_id)


def test_the_verdict_comes_from_the_reviewers_own_message_not_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for the regression: break the answer path, get no review.

    The final NDJSON line of the stream is the terminal prompt response, not the answer. If
    dropping the collected answer text did not remove the verdict, some other part of the
    stream would be supplying it - which is exactly what must not happen.
    """
    harness = structured_harness(tmp_path)
    monkeypatch.setattr(driver_module, "AnswerTranscript", _SilentTranscript)

    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED, "the turn still settled normally"
    assert result.review is None, "no verdict may be available without the reviewer's answer"
    assert any(note.startswith("review_missing") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


class _SilentTranscript:
    """Stands in for a runtime that delivered no assistant text to this build."""

    skipped_other_session = 0
    truncated = False
    rejected = ""

    def __init__(self, **_: object) -> None:
        pass

    def observe_update(self, *_: object, **__: object) -> None:
        return None

    def final_answer(self) -> None:
        return None


def test_a_non_reviewer_invocation_gets_no_review_authority(tmp_path: Path) -> None:
    """Dropping the role check must not be able to hand an implementer a verdict."""
    harness = structured_harness(tmp_path)
    result, handle, _ = run_invocation(harness, "implementer", IMPLEMENTER_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None
    assert (harness.workspace / "src" / "parser.py").is_file()
    harness.driver.release(handle.invocation_id)


GOALS = {"implementer": IMPLEMENTER_GOAL, "reviewer": REVIEW_GOAL}


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_completion_that_answers_another_request_is_unknown_for_every_role(
    tmp_path: Path, role: str
) -> None:
    """A JSON-RPC response is not task completion unless it answers *this* prompt.

    The client sends ``session/prompt`` as id 2 and the stub settles id 7 instead, so nothing in
    the stream settles this invocation's turn. That is an unknown outcome for both roles: an
    implementer's unbound ``end_turn`` used to come back COMPLETED and be frozen, checked and
    possibly delivered, and a reviewer's was COMPLETED with only a note attached.
    """
    harness = structured_harness(tmp_path, terminal_responses="7:end_turn")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert handle.dispatched is True, "the prompt was sent; only its answer is missing"
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unbound_completion"
    assert result.review is None
    assert any(note.startswith("unbound_completion:") for note in result.limitations)
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_the_prompts_own_response_settles_the_turn_not_a_later_one(
    tmp_path: Path, role: str
) -> None:
    """The prompt settles as ``max_tokens``; a later ``end_turn`` for id 7 does not overrule it.

    The stop reason used to be the *last* one in the stream, whatever request it answered, and the
    binding check accepted any terminal id that matched. Together they turned this stream into a
    clean COMPLETED with no limitation - and a reviewer's verdict was still decoded.
    """
    harness = structured_harness(tmp_path, terminal_responses="2:max_tokens,7:end_turn")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.FAILED
    assert result.error_code == "stop_reason_max_tokens"
    assert result.review is None, "a turn that did not complete carries no verdict"
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_cancelled_turn_nobody_asked_to_stop_is_a_failure_not_a_cancellation(
    tmp_path: Path, role: str
) -> None:
    """DSH also settles a prompt as ``cancelled`` when it disposes of a session on its own.

    No stop was requested for this invocation, so reporting CANCELLED would describe an operator
    action that never happened. It is the harness ending the turn: a failure, named as such.
    """
    harness = structured_harness(tmp_path, terminal_responses="2:cancelled")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.FAILED
    assert result.error_code == "cancelled_unrequested"
    assert result.review is None
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("message_ids", [True, False])
def test_multi_chunk_answers_reassemble_with_and_without_message_ids(
    tmp_path: Path, message_ids: bool
) -> None:
    """``messageId`` is optional in ACP; the answer must survive either way."""
    harness = structured_harness(tmp_path, message_ids=message_ids)
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.review is not None and result.review.verdict == "accepted"
    harness.driver.release(handle.invocation_id)


def test_user_and_tool_text_cannot_supply_the_verdict(tmp_path: Path) -> None:
    """The stub emits conflicting verdicts as user text and tool output before the answer.

    Only the reviewer's own final message may win; if either of the others were read, the
    verdict would be ``changes_requested``.
    """
    harness = structured_harness(tmp_path)
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.review is not None
    assert result.review.verdict == "accepted"
    assert all(finding.get("id") != "user-text" for finding in result.review.findings)
    assert all(finding.get("id") != "tool-output" for finding in result.review.findings)
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# 2. transport validity stays separate from content
# --------------------------------------------------------------------------


def test_cancelled_turn_keeps_its_verdict_out_of_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation wins over content: an accepted-looking answer in a stopped turn is not a pass."""
    harness = structured_harness(tmp_path)
    handle, _ = harness.start("I-1", goal=REVIEW_GOAL)
    assert harness.wait_for_dispatch(handle), "the prompt was never dispatched"

    receipt = harness.driver.cancel_handle(handle)
    assert receipt.status == "confirmed_stopped"

    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.CANCELLED
    assert result.review is None
    harness.driver.release(handle.invocation_id)


def test_an_over_budget_answer_never_yields_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict read from an answer that did not fit in memory is not trustworthy."""
    # Patched where the transcript enforces the bound, not where the message formats it.
    monkeypatch.setattr(review_module, "MAX_ANSWER_BYTES", 64)
    harness = structured_harness(tmp_path)

    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None, "the answer did not fit, so its verdict cannot be trusted"
    assert any("was not retained" in note for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_a_turn_without_a_stop_reason_never_yields_a_verdict(tmp_path: Path) -> None:
    """The reviewer writes its whole answer and exits, but nothing ever settles the prompt."""
    harness = structured_harness(tmp_path, terminal_responses="")

    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    assert result.review is None
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# 3. the controller: a decoded verdict reaches the acceptance checks
# --------------------------------------------------------------------------


def test_structured_review_reaches_the_controller(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """End to end: candidate + fixed checks + decoded verdict => a controller receipt.

    This is the case that the recorded live attempt could not reach: the verdict now travels
    through the real ``collect`` and the real ``_review``, with no fixture standing in for
    either. It fails on the unpatched code (``review=None`` => no receipt).
    """
    harness = structured_harness(tmp_path)
    controller, store, runner = controller_for(harness, tmp_path)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        review_evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")]
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.delivery_state is DeliveryState.LOCAL_CANDIDATE
    assert outcome.receipt is not None
    assert outcome.receipt.review.status == "accepted"
    assert outcome.receipt.review.evidence_ids, "the verdict must be recorded as review evidence"
    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 1
    # The verdict travelled as data, not as a pre-filled result object.
    assert runner.calls == ["unit", "docs-check"]

    assert len(review_evidence) == 1
    assert review_evidence[0]["status"] == EvidenceStatus.PASSED.value
    stored = json.loads(review_evidence[0]["detail"])
    assert stored["verdict"] == "accepted"
    assert stored["findings"][0]["id"] == "AC-1"


def test_changes_requested_keeps_its_findings_and_blocks(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """A genuine rejection stays a rejection, with its findings preserved."""
    harness = structured_harness(tmp_path, review_mode="changes")
    controller, store, _ = controller_for(harness, tmp_path)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")]
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED
    assert outcome.receipt is None
    assert len(evidence) == 1
    detail = json.loads(evidence[0]["detail"])
    assert detail["verdict"] == "changes_requested"
    assert detail["findings"][0]["detail"] == "empty input still reaches an invalid index"


@pytest.mark.parametrize(
    "review_mode",
    ["invalid", "duplicate", "wrong-shape", "ambiguous", "prose", "silent"],
)
def test_unusable_verdicts_block_as_a_protocol_error_not_a_rejection(
    tmp_path: Path, project, task_spec, project_root: Path, review_mode: str
) -> None:
    """Missing, malformed, ambiguous or absent verdicts are wire failures, not judgments."""
    harness = structured_harness(tmp_path, review_mode=review_mode)
    controller, store, _ = controller_for(harness, tmp_path)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")]
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_PROTOCOL_ERROR
    assert outcome.receipt is None
    assert "review" in (outcome.block_reason or "").lower()
    assert "requested changes" not in (outcome.block_reason or ""), (
        "a wire failure must not be described as the reviewer's substantive rejection"
    )
    assert len(evidence) == 1, "the failure is recorded as review evidence"
    assert evidence[0]["status"] == EvidenceStatus.ERROR.value


def test_the_controller_still_refuses_when_the_driver_loses_the_verdict(
    tmp_path: Path, project, task_spec, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original defect's controller-side symptom, reproduced deliberately.

    With verdict extraction removed the run blocks and produces no receipt - acceptance is
    never available without a validated verdict.
    """
    harness = structured_harness(tmp_path)
    controller, store, _ = controller_for(harness, tmp_path)
    monkeypatch.setattr(driver_module, "AnswerTranscript", _SilentTranscript)

    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.receipt is None
    assert outcome.block_code is RefusalCode.REVIEW_PROTOCOL_ERROR


def test_review_evidence_references_the_candidate_it_reviewed(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """The verdict is bound to a fingerprint and a checks digest, never free-floating."""
    harness = structured_harness(tmp_path)
    controller, store, _ = controller_for(harness, tmp_path)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")][0]
        run = dict(store.get_run(outcome.run_id))
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.receipt is not None
    assert evidence["candidate_fingerprint"] == outcome.receipt.candidate.fingerprint
    assert evidence["checks_digest"] == run["checks_digest"]
    assert evidence["attempt_id"] == outcome.receipt.attempt_id
    assert evidence["kind"] == "review"


def test_a_verdict_cannot_override_the_controller_state(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """A late verdict after a recorded stop must not become acceptance."""
    harness = structured_harness(tmp_path)
    controller, store, _ = controller_for(harness, tmp_path)
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        assert outcome.task_state is TaskState.ACCEPTED

        # A stop request after acceptance records the fact and cannot un-accept it.
        receipt = controller.cancel(outcome.run_id)
        row = store.get_run(outcome.run_id)
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert receipt.status == "confirmed_stopped"
    assert row["task_state"] == TaskState.ACCEPTED.value
    assert row["receipt_json"]


# --------------------------------------------------------------------------
# 4. a completion that is not bound to its prompt, as the controller and the ledger see it
# --------------------------------------------------------------------------


def _request(project, spec, project_root: Path) -> RunRequest:
    return RunRequest(
        task=spec, project=project, project_root=project_root, workspace_root=project_root
    )


def _run(controller: Controller, project, spec, project_root: Path):
    return controller.run_task(_request(project, spec, project_root))


def test_an_unbound_implementer_completion_is_never_frozen_or_checked(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """End to end through the production driver: the implementer's ``end_turn`` answers id 7.

    It used to be reported COMPLETED, so the controller froze the tree, ran the checks and could
    deliver it. An unknown outcome stops the run before any of that, and nothing re-dispatches.
    """
    harness = structured_harness(tmp_path, terminal_responses="7:end_turn")
    controller, store, runner = controller_for(harness, tmp_path)
    try:
        outcome = _run(controller, project, task_spec, project_root)
        review_evidence = store.evidence_for(outcome.run_id, "review")
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert runner.calls == [], "no check may run on a turn whose completion is unknown"
    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 0
    assert review_evidence == []


def test_an_unbound_reviewer_completion_blocks_as_unknown_not_as_a_protocol_error(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """The reviewer alone answers id 7: the run is unknown, and no review evidence is invented.

    ``review_protocol_error`` says the wire delivered nothing usable; here the turn's own outcome
    is not known, which is what ``outcome_unknown`` means and what ``resume`` reconciles.
    """
    harness = structured_harness(tmp_path, reviewer_terminal_responses="7:end_turn")
    controller, store, runner = controller_for(harness, tmp_path)
    try:
        outcome = _run(controller, project, task_spec, project_root)
        review_evidence = store.evidence_for(outcome.run_id, "review")
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert runner.calls == ["unit", "docs-check"], "the implementer's candidate was checked"
    assert outcome.reviewer_invocations == 1
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert review_evidence == [], "an unknown turn is not a failed review either"


#: The two shapes an unbound reviewer can arrive in: the production driver's (an unknown outcome
#: naming the cause) and that of a driver that reports the turn completed and only says, in a
#: limitation, that the completion is unbound - the shape that used to settle the ledger as done.
UNBOUND_REVIEWS = {
    "driver_reports_unknown": FakeScript(
        outcome=InvocationOutcome.OUTCOME_UNKNOWN,
        review=None,
        error_code="unbound_completion",
        error_message="no response answers the observed session/prompt request",
        limitations=["unbound_completion: no response answers the observed session/prompt"],
    ),
    "driver_reports_completed": FakeScript(
        outcome=InvocationOutcome.COMPLETED,
        review=None,
        limitations=[
            "review_unbound: the terminal response does not answer the observed session/prompt "
            "request; the turn's completion is not bound to this invocation's prompt"
        ],
    ),
}


@pytest.mark.parametrize("shape", sorted(UNBOUND_REVIEWS))
def test_an_unbound_review_keeps_the_root_blocked_for_the_next_revision(
    store: Store, project, task_spec, project_root: Path, tmp_path: Path, shape: str
) -> None:
    """The run says unknown, so the ledger says unknown, so revision 2 cannot dispatch.

    The ``finally`` that closes the reviewer's ledger row used to write the driver's raw outcome.
    For a turn the controller refuses as unknown that was ``settled/completed``: the root carried
    nothing unresolved, ``resume`` could not reopen the row, and a new revision of the same task
    was admitted and accepted on the root the unknown turn should have kept blocked.
    """
    binding = dispatch_helpers._binding(store, task_spec, project_root)
    limits = dispatch_helpers._limits()
    implementer = FakeDriver(
        project_root, FakeScript(write_plan=dict(dispatch_helpers.FAKE_WRITE_PLAN))
    )
    first = dispatch_helpers._root_controller(
        store,
        implementer,
        reviewer_driver=FakeDriver(project_root, UNBOUND_REVIEWS[shape]),
        binding=binding,
        limits=limits,
        authorization=dispatch_helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        data_dir=tmp_path / "data",
    )

    outcome = _run(first, project, task_spec, project_root)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    reviewer_rows = [row for row in store.invocations_for(outcome.run_id) if row.role == "reviewer"]
    assert len(reviewer_rows) == 1
    assert reviewer_rows[0].state is InvocationStartState.UNKNOWN, (
        "the ledger must record the controller's classification, not the driver's raw outcome"
    )
    assert reviewer_rows[0].outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert [row.role for row in store.pending_invocations(binding.root_id)] == ["reviewer"]

    first.resume(outcome.run_id)
    assert [row.role for row in store.pending_invocations(binding.root_id)] == ["reviewer"], (
        "reconciling observes the unknown turn; it does not resolve it"
    )

    next_spec = task_spec.model_copy(update={"revision": 2})
    second_driver = FakeDriver(project_root)
    second = dispatch_helpers._root_controller(
        store,
        second_driver,
        binding=binding,
        limits=limits,
        authorization=dispatch_helpers._authorization(
            spec=next_spec,
            binding=binding,
            limits=limits,
            project_root=project_root,
            authorization_id="AUTH-unbound-r2",
        ),
        data_dir=tmp_path / "data",
    )
    result, refusal = dispatch_helpers._run_or_refusal(
        second, _request(project, next_spec, project_root)
    )

    reason = str(refusal) if refusal is not None else str(result.block_reason)
    assert second_driver.started == [], f"revision 2 dispatched past an unknown review: {reason}"
    assert "unresolved invocation" in reason, reason
