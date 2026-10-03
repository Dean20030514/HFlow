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
    EventKind,
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
    trailing_updates: str | None = None,
    reviewer_trailing_updates: str | None = None,
    reported_usage: bool = False,
    answer_shape: str = "split",
) -> DriverHarness:
    """A driver whose agent answers by role, through the production launch path.

    The stub's marker files are moved *outside* the workspace the controller snapshots: they
    are harness scaffolding, and leaving them inside would be refused as an out-of-scope
    write - a different failure than the one these tests are about.

    ``terminal_responses`` replaces the stub's terminal prompt response with ``id:stopReason``
    pairs (the client sends ``session/prompt`` as id 2); ``reviewer_terminal_responses`` does the
    same for the reviewer only. ``trailing_updates`` / ``reviewer_trailing_updates`` set
    ``STUB_TRAILING_UPDATES`` / ``STUB_REVIEWER_TRAILING_UPDATES`` (updates sent after the
    terminal response), ``reported_usage`` sets ``STUB_REPORTED_USAGE=1`` (agent-reported
    usage and cost), and ``answer_shape`` selects how the reviewer's final message is
    framed (see the stub's ``STUB_ANSWER_SHAPE``).
    """
    harness = DriverHarness(tmp_path, "structured")
    scratch = (tmp_path / "stub-scratch").resolve()
    harness.driver.extra_env["STUB_SCRATCH_DIR"] = str(scratch)
    harness.driver.extra_env["STUB_SPAWN_LOG"] = str(scratch / "agent-spawns.jsonl")
    harness.driver.extra_env["STUB_REVIEW_MODE"] = review_mode
    harness.driver.extra_env["STUB_MESSAGE_IDS"] = "1" if message_ids else "0"
    harness.driver.extra_env["STUB_IMPLEMENTER_PATH"] = "src/parser.py"
    harness.driver.extra_env["STUB_ANSWER_SHAPE"] = answer_shape
    if terminal_responses is not None:
        harness.driver.extra_env["STUB_TERMINAL_RESPONSES"] = terminal_responses
    if reviewer_terminal_responses is not None:
        harness.driver.extra_env["STUB_REVIEWER_TERMINAL_RESPONSES"] = reviewer_terminal_responses
    if trailing_updates is not None:
        harness.driver.extra_env["STUB_TRAILING_UPDATES"] = trailing_updates
    if reviewer_trailing_updates is not None:
        harness.driver.extra_env["STUB_REVIEWER_TRAILING_UPDATES"] = reviewer_trailing_updates
    if reported_usage:
        harness.driver.extra_env["STUB_REPORTED_USAGE"] = "1"
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
        findings=[{"id": "AC-1", "severity": "P2", "body": "empty input returns the agreed result"}],
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

    def bind_session(self, _session_id: str) -> None:
        return None

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


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_an_error_answering_the_prompt_is_recorded_and_stays_unknown(
    tmp_path: Path, role: str
) -> None:
    """ACP v1 reports a failed prompt as a JSON-RPC error answering it, not as a stop reason.

    DSH sends ``-32603 Internal error: turn failed: ...``. The code and the message are recorded,
    but whether a model call was made is not observable, so the outcome stays unknown - the same
    as a turn with no stop reason, only now with its cause named.
    """
    harness = structured_harness(tmp_path, terminal_responses="2:!-32603")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert handle.dispatched is True
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "prompt_error_response", result.error_message
    assert "-32603" in (result.error_message or "")
    assert "turn failed: stub provider error" in (result.error_message or "")
    assert any(note.startswith("prompt_error_response:") for note in result.limitations)
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_an_error_answering_another_request_is_not_attributed_to_the_prompt(
    tmp_path: Path, role: str
) -> None:
    """An error for id 7 answers some other request: the turn simply never settled."""
    harness = structured_harness(tmp_path, terminal_responses="7:!-32603")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    assert "-32603" not in (result.error_message or "")
    assert not any(note.startswith("prompt_error") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("responses", ["2:!-32603,2:end_turn", "2:end_turn,2:!-32603"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_prompt_answered_with_an_error_and_a_stop_reason_is_unknown(
    tmp_path: Path, role: str, responses: str
) -> None:
    """A request is answered once. Two answers, in either order, mean neither is taken.

    Both streams used to be COMPLETED, and the reviewer's verdict was decoded from them.
    """
    harness = structured_harness(tmp_path, terminal_responses=responses)

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "prompt_error_response"
    assert "stopReason='end_turn'" in (result.error_message or "")
    assert result.review is None
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize(
    ("responses", "outcome", "error_code"),
    [
        (
            "2:?session/request_permission,2:!-32603,2:end_turn",
            InvocationOutcome.COMPLETED,
            None,
        ),
        (
            "2:?session/request_permission,2:!-32603",
            InvocationOutcome.OUTCOME_UNKNOWN,
            "no_stop_reason",
        ),
    ],
)
def test_an_error_carrying_an_id_the_agent_reused_is_not_attributed(
    tmp_path: Path, responses: str, outcome: InvocationOutcome, error_code: str | None
) -> None:
    """JSON-RPC ids are per direction, and the stream carries both.

    The ACP SDK DSH uses numbers each side's requests from 0 (``jsonrpc.js`` ``nextRequestId = 0``
    and ``this.nextRequestId++``, lines 372 and 545 of the vendored 1.4.0), so the agent's third
    ``session/request_permission`` carries the prompt's id 2. An error with that id may answer the
    agent's request, so it is recorded as unattributed and never as the prompt's answer. The
    client's exit code is not asserted: the stub exits 1 after any error item.
    """
    harness = structured_harness(tmp_path, terminal_responses=responses)

    result, handle, _ = run_invocation(harness, "implementer", GOALS["implementer"])

    assert result.outcome is outcome, result.error_message
    assert result.error_code == error_code
    assert any(
        note.startswith("prompt_error_unattributed:") and "session/request_permission" in note
        for note in result.limitations
    )
    assert not any(note.startswith("prompt_error_response:") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("reason", ["paused", "error"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_stop_reason_outside_acp_v1_is_unknown_in_the_result_and_the_event(
    tmp_path: Path, role: str, reason: str
) -> None:
    """A stop reason this build cannot read says nothing it can act on: unknown, not FAILED.

    It used to be ``FAILED stop_reason_<reason>`` while the event projection already said
    ``outcome_unknown``; the two now share ACP v1's closed set.
    """
    harness = structured_harness(tmp_path, terminal_responses=f"2:{reason}")

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unknown_stop_reason"
    assert repr(reason) in (result.error_message or "")
    settled = [
        event
        for event in harness.driver.events(handle.invocation_id)
        if event.method == "session/prompt" and "stopReason=" in event.message
    ]
    assert settled and settled[-1].kind is EventKind.OUTCOME_UNKNOWN
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
    assert all(finding.id != "user-text" for finding in result.review.findings)
    assert all(finding.id != "tool-output" for finding in result.review.findings)
    harness.driver.release(handle.invocation_id)


def test_a_reasoning_block_inside_the_final_message_is_skipped_not_a_boundary(
    tmp_path: Path,
) -> None:
    """DSH frames a [text, reasoning, text] message as a same-id thought between two chunks.

    The thought is verdict-shaped: if it were read, the verdict would change or become
    ambiguous. If it were treated as the end of the message, only the second half of the
    answer would be read (an unterminated fence).
    """
    harness = structured_harness(tmp_path, answer_shape="thought-inside")
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review == ReviewOutput(
        verdict="accepted",
        findings=[{"id": "AC-1", "severity": "P2", "body": "empty input returns the agreed result"}],
    )
    assert f"{REVIEW_INPUT_PREFIX}decoded: accepted" in " ".join(result.limitations)
    harness.driver.release(handle.invocation_id)


def test_a_message_id_that_resumes_after_another_message_is_a_wire_failure(
    tmp_path: Path,
) -> None:
    """The answer's second half arrives under the earlier commentary's ``messageId``.

    Which text is the final message is then not identified, so no verdict is decoded. The line
    number is not asserted: it depends on the stub's incidental line layout, not on the
    behaviour under test.
    """
    harness = structured_harness(tmp_path, answer_shape="resumed-id")
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None
    assert any(
        note.startswith("review_invalid:") and "continues message 'm-4'" in note
        for note in result.limitations
    ), result.limitations
    harness.driver.release(handle.invocation_id)


OTHER_SESSION_NOTE = "agent_message_chunk_other_session="


@pytest.mark.parametrize("message_ids", [True, False])
@pytest.mark.parametrize("answer_shape", ["other-session-last", "no-session-last"])
def test_a_message_for_another_session_never_supplies_the_verdict(
    tmp_path: Path, answer_shape: str, message_ids: bool
) -> None:
    """The reviewer rejects; an ``accepted`` message under another session (or none) follows.

    The transcript is bound to the session the prompt named, so that message is counted in a
    limitation and leaves the turn with no verdict at all. Without the binding it was the final
    message, and the rejection became an acceptance.
    """
    harness = structured_harness(
        tmp_path, review_mode="changes", answer_shape=answer_shape, message_ids=message_ids
    )
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None, result.limitations
    assert any(note.startswith(f"{OTHER_SESSION_NOTE}1: ") for note in result.limitations), (
        result.limitations
    )
    assert any(
        note.startswith("review_invalid:") and "names session" in note
        for note in result.limitations
    ), result.limitations
    harness.driver.release(handle.invocation_id)


def test_a_clean_reviewer_turn_excludes_nothing(tmp_path: Path) -> None:
    """Same-session behaviour is unchanged: the verdict decodes and no exclusion is noted."""
    harness = structured_harness(tmp_path)
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.review is not None and result.review.verdict == "accepted"
    assert not any(note.startswith(OTHER_SESSION_NOTE) for note in result.limitations)
    transcript = harness.driver._transcripts[handle.invocation_id]
    assert transcript.session_id and transcript.session_id.startswith("sess-")
    harness.driver.release(handle.invocation_id)


def _rewrite_prompt_observation(harness: DriverHarness, rewrite) -> None:
    """Pass every observed wire message through ``rewrite`` before the driver records it.

    ``rewrite(message)`` returns the messages to record in its place. The fake client always
    writes the prompt before the agent can answer, so this is how a stream with a chunk ahead of
    the prompt, or a prompt without a session, reaches the production reader.
    """
    original = harness.driver._note_message

    def note(invocation_id: str, message: dict, *, line_index: int = -1) -> None:
        for item in rewrite(message):
            original(invocation_id, item, line_index=line_index)

    harness.driver._note_message = note  # type: ignore[method-assign]


def test_a_message_chunk_before_the_prompt_request_leaves_no_verdict(tmp_path: Path) -> None:
    """A chunk observed before any prompt named the turn's session is attributable to nothing.

    It carries a verdict-shaped text under the very session the prompt then names; the turn's
    own answer would decode cleanly. The transcript is unusable instead, so no verdict at all.
    """
    harness = structured_harness(tmp_path)

    def early_chunk(message: dict) -> list[dict]:
        if message.get("method") != "session/prompt":
            return [message]
        chunk = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": message["params"]["sessionId"],
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": "m-0",
                    "content": {"type": "text", "text": '{"verdict": "accepted", "findings": []}'},
                },
            },
        }
        return [chunk, message]

    _rewrite_prompt_observation(harness, early_chunk)
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None
    assert any(
        note.startswith("review_invalid:") and "before the session/prompt request" in note
        for note in result.limitations
    ), result.limitations
    harness.driver.release(handle.invocation_id)


def test_a_prompt_that_names_no_session_leaves_no_verdict(tmp_path: Path) -> None:
    """With no session named, no chunk can be told apart from another session's."""
    harness = structured_harness(tmp_path)

    def without_session(message: dict) -> list[dict]:
        if message.get("method") != "session/prompt":
            return [message]
        params = {key: value for key, value in message["params"].items() if key != "sessionId"}
        return [{**message, "params": params}]

    _rewrite_prompt_observation(harness, without_session)
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert handle.dispatched is True
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None
    assert any(
        note.startswith("review_invalid:") and "named no sessionId" in note
        for note in result.limitations
    ), result.limitations
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
    assert detail["findings"][0]["body"] == "empty input still reaches an invalid index"


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


def test_a_rejection_before_the_final_messages_own_reasoning_never_becomes_an_acceptance(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """One final message holds a rejection block, its own reasoning, then an acceptance.

    The reasoning arrives as a same-id thought. When that thought ended the message, only the
    fragment after it was read, and the run was ACCEPTED with a receipt although the reviewer's
    final message also rejected the candidate. The whole message is now the answer, and two
    verdicts in it are ambiguous: a protocol error, never an acceptance.
    """
    harness = structured_harness(tmp_path, answer_shape="conflict-inside")
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
    assert "ambiguous" in (outcome.block_reason or ""), outcome.block_reason
    assert len(evidence) == 1
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


def test_an_implementer_prompt_error_blocks_as_unknown_and_status_names_the_code(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """The implementer's prompt is answered with an error: blocked, unchecked, and named."""
    from hflow.controller import inspect_run
    from hflow.report import status_text

    harness = structured_harness(tmp_path, terminal_responses="2:!-32603")
    controller, store, runner = controller_for(harness, tmp_path)
    try:
        outcome = _run(controller, project, task_spec, project_root)
        text = status_text(inspect_run(store, outcome.run_id))
        stored = json.loads(store.attempts_for(outcome.run_id)[0]["result_json"])
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert runner.calls == [], "no check may run on a turn whose outcome is unknown"
    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 0
    assert "prompt_error_response" in (outcome.block_reason or "")
    assert "-32603" in (outcome.block_reason or "")
    assert "prompt_error_response" in text
    assert stored["error_code"] == "prompt_error_response"


def test_a_reviewer_prompt_error_blocks_as_unknown_with_its_code(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """The reviewer's prompt is answered with an error: unknown, not a protocol error."""
    harness = structured_harness(tmp_path, reviewer_terminal_responses="2:!-32603")
    controller, store, runner = controller_for(harness, tmp_path)
    try:
        outcome = _run(controller, project, task_spec, project_root)
        review_evidence = store.evidence_for(outcome.run_id, "review")
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert runner.calls == ["unit", "docs-check"]
    assert outcome.reviewer_invocations == 1
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert "prompt_error_response" in (outcome.block_reason or "")
    assert "-32603" in (outcome.block_reason or "")
    assert review_evidence == []


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
