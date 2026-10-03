"""What settles a turn, what arrives after it, and what its settlement does not carry.

Everything here runs offline, through the production driver, the stand-in acpx client and a stub
agent that is a separate process - the same path ``test_review_wire.py`` uses. Three facts:

1. Stable ACP v1 sends a turn's ``session/update`` notifications before its ``session/prompt``
   response, and ACP issue #554 records agents that do not. The driver records where the bound
   response fell and counts what followed it, for a stream it read to its end; a reviewer message
   that arrived after the response is outside the settled turn, and a stream not read to its end
   does not say what followed, so no verdict is decoded from either turn.
2. An agent-reported ``usage`` on the prompt response (an UNSTABLE ACP field) and the optional
   ``cost`` of a ``usage_update`` are not billing: they never fill a billed field.
3. A prompt response with no ``stopReason`` - such as the insertion acknowledgement an unreleased
   ACP v2 RFD sketches - settles nothing, and a stop reason outside ACP v1's set is never success.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import hflow.drivers.acpx_dsh as driver_module
from hflow.contracts import (
    EventKind,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    RefusalCode,
    RunRequest,
    StreamOrder,
    TaskState,
)
from hflow.review import AnswerTranscript

from .test_review_wire import (
    GOALS,
    REVIEW_GOAL,
    controller_for,
    run_invocation,
    structured_harness,
)


def _run_task(harness, tmp_path: Path, project, task_spec, project_root: Path):
    """One controller run through the production driver; returns (outcome, store-derived facts)."""
    from hflow.controller import inspect_run

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
        evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")]
        inspection = inspect_run(store, outcome.run_id)
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()
    return outcome, evidence, inspection, runner


def _response_line(harness, invocation_id: str) -> int:
    """The 0-based line of the stream's ``stopReason`` response, read from the retained log."""
    log = harness.data_dir / "invocations" / invocation_id / "events.ndjson"
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next(index for index, line in enumerate(lines) if '"stopReason"' in line)


# --------------------------------------------------------------------------
# 1. updates after the prompt response
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_clean_turn_records_its_prompt_response_line_and_nothing_after_it(
    tmp_path: Path, role: str
) -> None:
    harness = structured_harness(tmp_path)
    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.stream_order == StreamOrder(
        prompt_response_line=_response_line(harness, handle.invocation_id),
        updates_after_prompt_response=0,
        message_chunks_after_prompt_response=0,
    )
    assert not any(note.startswith("updates_after_prompt_response") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_updates_after_the_prompt_response_are_counted_and_noted(
    tmp_path: Path, role: str
) -> None:
    """A usage update and a tool update after the response: recorded, and nothing else changes.

    Neither is a message chunk, so neither can change which message is a reviewer's final one:
    the verdict is still decoded from the message the turn ended with.
    """
    harness = structured_harness(tmp_path, trailing_updates="usage,tool")
    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.stream_order is not None
    assert result.stream_order.updates_after_prompt_response == 2
    assert result.stream_order.message_chunks_after_prompt_response == 0
    assert any(
        note.startswith("updates_after_prompt_response=2: ") for note in result.limitations
    ), result.limitations
    if role == "reviewer":
        assert result.review is not None and result.review.verdict == "accepted"
    else:
        assert result.review is None
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("message_ids", [True, False])
def test_a_message_after_the_prompt_response_cannot_supply_or_change_a_verdict(
    tmp_path: Path, message_ids: bool
) -> None:
    """The turn ends with ``changes_requested``; an ``accepted`` verdict follows the response.

    Read as the final message, the trailing text would turn a rejection into an acceptance. It
    arrived outside the settled turn, so the turn's final answer is not identified: no verdict at
    all, reported as an ambiguous answer.
    """
    harness = structured_harness(
        tmp_path, review_mode="changes", message_ids=message_ids, trailing_updates="message"
    )
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.review is None
    assert result.stream_order is not None
    assert result.stream_order.message_chunks_after_prompt_response == 1
    assert any(
        note.startswith("review_ambiguous: 1 agent_message_chunk update(s) arrived after")
        for note in result.limitations
    ), result.limitations
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_without_the_after_response_check_the_trailing_text_would_be_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: blind the check, and the trailing ``accepted`` overturns the rejection."""
    monkeypatch.setattr(AnswerTranscript, "chunks_after", lambda self, line_index: 0)
    harness = structured_harness(tmp_path, review_mode="changes", trailing_updates="message")

    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.review is not None and result.review.verdict == "accepted"
    harness.driver.release(handle.invocation_id)


def test_a_message_for_another_session_after_the_response_leaves_no_verdict(
    tmp_path: Path,
) -> None:
    """The count is per prompt session, and the transcript is bound to that session.

    A trailing ``accepted`` chunk under another ``sessionId`` is not one of the prompt session's
    updates, so the after-response count stays 0. It still leaves the reviewer without a verdict:
    a one-session turn that streams another session's message has no identified final answer.
    Before the binding it was retained and would have been the final message.
    """
    harness = structured_harness(
        tmp_path, review_mode="changes", trailing_updates="message-other-session"
    )
    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert result.stream_order is not None
    assert result.stream_order.updates_after_prompt_response == 0
    assert result.review is None
    assert any(
        note.startswith("agent_message_chunk_other_session=1: ") for note in result.limitations
    ), result.limitations
    assert any(
        note.startswith("review_invalid:") and "names session" in note
        for note in result.limitations
    ), result.limitations
    harness.driver.release(handle.invocation_id)


def test_an_implementer_message_after_the_response_is_recorded_not_judged(tmp_path: Path) -> None:
    harness = structured_harness(tmp_path, trailing_updates="message")
    result, handle, _ = run_invocation(harness, "implementer", GOALS["implementer"])

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.stream_order is not None
    assert result.stream_order.message_chunks_after_prompt_response == 1
    assert any(note.startswith("updates_after_prompt_response=1: ") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("review_mode", ["fenced", "changes"])
def test_a_trailing_reviewer_message_blocks_as_a_protocol_error(
    tmp_path: Path, project, task_spec, project_root: Path, review_mode: str
) -> None:
    """A reviewer that spoke after its response neither accepts nor rejects.

    No verdict was identified, which is a wire failure - and no repair is decided from it.
    """
    harness = structured_harness(
        tmp_path, review_mode=review_mode, reviewer_trailing_updates="message"
    )
    outcome, evidence, inspection, runner = _run_task(
        harness, tmp_path, project, task_spec, project_root
    )

    assert runner.calls == ["unit", "docs-check"], "the implementer's candidate was checked"
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_PROTOCOL_ERROR, outcome.block_reason
    assert outcome.receipt is None
    assert len(evidence) == 1 and evidence[0]["status"] == EvidenceStatus.ERROR.value
    assert "ambiguous" in evidence[0]["detail"]
    assert outcome.implementer_invocations == 1
    assert inspection.repair_records == []


def test_status_and_report_show_what_followed_each_prompt_response(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    """Read from the stored results; a result without the record prints unknown, not 0."""
    from hflow.report import report_json, status_text

    harness = structured_harness(tmp_path, reviewer_trailing_updates="usage")
    outcome, _, inspection, _ = _run_task(harness, tmp_path, project, task_spec, project_root)

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    attempt = inspection.attempts[0]
    assert attempt.stream_order is not None
    assert attempt.stream_order.updates_after_prompt_response == 0
    assert attempt.review_stream_order is not None
    assert attempt.review_stream_order.updates_after_prompt_response == 1
    rendered = status_text(inspection)
    assert (
        "stream        implementer prompt_response_line="
        f"{attempt.stream_order.prompt_response_line} updates_after_prompt_response=0"
    ) in rendered
    assert "stream        reviewer prompt_response_line=" in rendered
    assert "updates_after_prompt_response=1 (agent_message_chunk 0)" in rendered
    payload = report_json(inspection)["attempts"][0]
    assert payload["review_stream_order"]["updates_after_prompt_response"] == 1

    legacy = inspection.attempts[0].model_copy(update={"stream_order": None})
    rendered_legacy = status_text(inspection.model_copy(update={"attempts": [legacy]}))
    assert "stream        implementer updates_after_prompt_response=unknown" in rendered_legacy


def test_stream_positions_keep_counting_past_the_retained_line_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line numbers come from a counter, not from the bounded in-memory line window.

    With the window at 3 lines, the old ``len(window) - 1`` index gave the response and the
    trailing chunk the same number (2), so the chunk did not look later and ``accepted`` was
    decoded from it.
    """
    monkeypatch.setattr(driver_module, "MAX_BUFFERED_LINES", 3)
    harness = structured_harness(tmp_path, review_mode="changes", trailing_updates="message")

    result, handle, _ = run_invocation(harness, "reviewer", REVIEW_GOAL)

    assert len(harness.driver.raw_lines(handle.invocation_id)) == 3
    assert result.stream_order is not None
    assert result.stream_order.prompt_response_line == _response_line(
        harness, handle.invocation_id
    )
    assert result.stream_order.prompt_response_line > 3
    assert result.review is None
    assert any(note.startswith("review_ambiguous:") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


class _NeverDrained(dict):
    """Stands in for a reader that never finished the stream before ``collect`` folded it."""

    def __setitem__(self, key: str, value: bool) -> None:
        super().__setitem__(key, False)


def _undrained_reviewer(harness, invocation_id: str, role: str = "reviewer"):
    """Start an invocation whose reader never reports the stream drained; wait for the reader."""
    request = InvocationRequest(
        invocation_id=invocation_id,
        attempt_id="A-1",
        run_id="R-1",
        role=role,
        task_id="T-1",
        task_revision=1,
        goal=GOALS[role],
        acceptance=[],
        write_allow=["src/parser.py"],
        write_deny=[],
        workspace=str(harness.workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        writes_allowed=False,
        data_dir=str(harness.data_dir),
    )
    harness.driver._stream_drained = _NeverDrained()
    handle = harness.driver.start_handle(request)
    reader: threading.Thread = harness.driver._threads[request.invocation_id]
    reader.join(timeout=30)
    assert not reader.is_alive()
    return handle


UNDRAINED_NOTE = "review_ambiguous: the client's output was not read to its end"


def test_a_stream_not_read_to_its_end_records_no_order_and_gives_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``collect`` folds a result after a bounded drain wait even if the reader is not done.

    "Nothing followed the response" is then not known: no stream order is recorded, and no
    verdict is decoded - the after-response check would otherwise pass on unread lines.
    """
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.1)
    harness = structured_harness(tmp_path)
    handle = _undrained_reviewer(harness, "I-undrained")

    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.stream_order is None
    assert result.review is None
    assert any(note.startswith(UNDRAINED_NOTE) for note in result.limitations), result.limitations
    harness.driver.release(handle.invocation_id)


def test_a_reader_that_finishes_during_collect_still_gives_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain flag is read once: the stream order and the verdict cannot disagree.

    The reader finishes right after the stream order was decided without it. The verdict check
    used to read the flag again, find it set and decode a verdict next to a record that said the
    stream was not read to its end.
    """
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.1)
    harness = structured_harness(tmp_path)
    handle = _undrained_reviewer(harness, "I-late-drain")
    original = harness.driver._stream_order

    def order_then_drained(*args, **kwargs):
        order = original(*args, **kwargs)
        dict.__setitem__(harness.driver._stream_drained, handle.invocation_id, True)
        return order

    harness.driver._stream_order = order_then_drained  # type: ignore[method-assign]
    result = harness.driver.collect(handle)

    assert harness.driver._stream_drained[handle.invocation_id] is True, "the flag did flip"
    assert result.stream_order is None
    assert result.review is None
    assert any(note.startswith(UNDRAINED_NOTE) for note in result.limitations), result.limitations
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_a_chunk_the_reader_adds_during_collect_cannot_leave_a_stale_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain is checked before the transcript is read, so the answer read is final.

    Here the reader handles one more line just after the answer was read: a chunk that resumes
    the commentary's ``messageId`` and rejects the transcript, which also clears the chunks the
    after-response check counts. Then it reports the stream drained. Read in the old order, the
    drain check passed, nothing counted as trailing, and the stale answer's verdict was decoded.
    """
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.1)
    harness = structured_harness(tmp_path)
    handle = _undrained_reviewer(harness, "I-late-reject")
    transcript = harness.driver._transcripts[handle.invocation_id]
    read_answer = transcript.final_answer

    def answer_then_late_line():
        answer = read_answer()
        transcript.observe_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m-4",
                "content": {"type": "text", "text": "late"},
            },
            params={"sessionId": transcript.session_id},
            sequence=10_000,
            line_index=10_000,
        )
        dict.__setitem__(harness.driver._stream_drained, handle.invocation_id, True)
        return answer

    transcript.final_answer = answer_then_late_line  # type: ignore[method-assign]
    result = harness.driver.collect(handle)

    assert result.stream_order is None
    assert result.review is None
    assert any(note.startswith(UNDRAINED_NOTE) for note in result.limitations), result.limitations
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_an_implementer_stream_not_read_to_its_end_is_unknown_not_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settled turn in the prefix read so far is not a settled invocation.

    The implementer's ``drained`` flag used to decide only the stream order, so a stream the
    reader had not finished folded as COMPLETED with nothing saying its tail was unread.
    """
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.1)
    harness = structured_harness(tmp_path)
    handle = _undrained_reviewer(harness, "I-undrained-impl", role="implementer")

    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "stream_not_drained"
    assert result.stream_order is None
    assert any(note.startswith("stream_not_drained:") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# 2. agent-reported usage and cost are never billing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_agent_reported_usage_and_cost_never_fill_billed_fields(tmp_path: Path, role: str) -> None:
    harness = structured_harness(tmp_path, reported_usage=True)
    result, handle, _ = run_invocation(harness, role, GOALS[role])
    raw = harness.driver.raw_lines(handle.invocation_id)

    # Not vacuous: the stream really carried both agent-reported figures.
    assert any('"usage":{"totalTokens":1234' in line for line in raw)
    assert any('"cost":{"amount":0.42' in line for line in raw)
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.provider_billed_tokens is None
    assert result.reported_cost is None
    harness.driver.release(handle.invocation_id)


def test_agent_reported_usage_never_reaches_the_receipt(
    tmp_path: Path, project, task_spec, project_root: Path
) -> None:
    from hflow.report import receipt_text

    harness = structured_harness(tmp_path, reported_usage=True)
    outcome, _, _, _ = _run_task(harness, tmp_path, project, task_spec, project_root)

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.receipt is not None
    usage = outcome.receipt.usage
    assert usage.provider_billed_tokens is None
    assert usage.provider_cost is None
    assert usage.subscription_quota_remaining is None
    text = receipt_text(outcome.receipt)
    assert "provider_billed_tokens unknown" in text
    assert "provider_cost          unknown" in text


# --------------------------------------------------------------------------
# 3. a prompt response without a stop reason settles nothing; an unlisted one is never success
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stop_reason", ["end_turn", "error"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_prompt_response_without_a_stop_reason_settles_nothing(
    tmp_path: Path, role: str, stop_reason: str
) -> None:
    """``{messageId}`` acknowledges insertion; the idle ``state_update`` after it is an update.

    The shape is the insertion acknowledgement an unreleased ACP v2 RFD sketches. The test keys on
    the absence of a bound stop reason, not on v2 field names, which may change.
    """
    harness = structured_harness(tmp_path, terminal_responses=f"2:v2:{stop_reason}")
    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert handle.dispatched is True
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    assert result.review is None
    assert result.stream_order is None, "no bound response, so nothing to count from"
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    assert EventKind.COMPLETED not in {
        event.kind for event in harness.driver.events(handle.invocation_id)
    }
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_an_unlisted_stop_reason_is_never_success(tmp_path: Path, role: str) -> None:
    """``error`` is not an ACP v1 stop reason. Whatever it is classified as, it is not COMPLETED.

    Loose on purpose: the exact classification of unlisted stop reasons is pinned in
    ``test_review_wire.py``, not here.
    """
    harness = structured_harness(tmp_path, terminal_responses="2:error")
    result, handle, _ = run_invocation(harness, role, GOALS[role])

    assert result.outcome is not InvocationOutcome.COMPLETED
    assert result.outcome in {InvocationOutcome.FAILED, InvocationOutcome.OUTCOME_UNKNOWN}
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("unsettled_role", ["implementer", "reviewer"])
def test_a_prompt_response_without_a_stop_reason_blocks_the_run_as_unknown(
    tmp_path: Path, project, task_spec, project_root: Path, unsettled_role: str
) -> None:
    """Unknown means stop: nothing is accepted, re-dispatched or taken as review evidence."""
    settings = (
        {"terminal_responses": "2:v2:end_turn"}
        if unsettled_role == "implementer"
        else {"reviewer_terminal_responses": "2:v2:end_turn"}
    )
    harness = structured_harness(tmp_path, **settings)
    outcome, evidence, _, runner = _run_task(harness, tmp_path, project, task_spec, project_root)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert evidence == []
    assert outcome.implementer_invocations == 1
    assert outcome.reviewer_invocations == (1 if unsettled_role == "reviewer" else 0)
    assert runner.calls == ([] if unsettled_role == "implementer" else ["unit", "docs-check"])


# --------------------------------------------------------------------------
# 4. a line the reader cannot interpret never ends the reader
# --------------------------------------------------------------------------

#: Long enough that a dead reader shows: ``collect`` would wait this long for the drain flag.
SLOW_DRAIN_SECONDS = 60.0


def _the_line_after_was_read(harness, invocation_id: str) -> bool:
    """The trailing ``usage_update`` (the stream's last line) reached the reader."""
    raw = harness.driver.raw_lines(invocation_id)
    return bool(raw) and '"cost"' in raw[-1] and '"usage_update"' in raw[-1]


def _timed_invocation(harness, role: str):
    started = time.monotonic()
    result, handle, request = run_invocation(harness, role, GOALS[role])
    return result, handle, time.monotonic() - started


@pytest.mark.parametrize("kind", ["deep", "bigint"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_line_pythons_json_cannot_decode_is_unparseable_and_reading_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, kind: str
) -> None:
    """Nesting past the decoder's depth raises ``RecursionError``; a 5000-digit integer raises a
    plain ``ValueError``. Only ``JSONDecodeError`` used to be caught, so either killed the reader:
    the following line was never read and an implementer folded as COMPLETED."""
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", SLOW_DRAIN_SECONDS)
    harness = structured_harness(tmp_path, trailing_updates=f"{kind},usage")

    result, handle, elapsed = _timed_invocation(harness, role)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"
    assert harness.driver.unparsed_line_count(handle.invocation_id) == 1
    assert _the_line_after_was_read(harness, handle.invocation_id)
    assert handle.invocation_id not in harness.driver._reader_failures
    assert result.review is None
    assert elapsed < SLOW_DRAIN_SECONDS / 2, "collect waited for a reader that had died"
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_lone_surrogate_in_a_message_chunk_never_ends_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A JSON ``\\uD83D`` escape survives JSON.stringify and decodes to an unpaired surrogate.

    Sizing it with ``encode("utf-8")`` raised ``UnicodeEncodeError`` out of the reader. Now the
    chunk makes the turn's answer unusable (a reviewer gets no verdict, fail closed) and the
    stream is read to its end. An implementer's assistant text decides nothing, so its turn - a
    settled ``end_turn`` with a stream read whole - stays COMPLETED, with the after-response
    updates recorded as before.
    """
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", SLOW_DRAIN_SECONDS)
    harness = structured_harness(tmp_path, trailing_updates="surrogate,usage")

    result, handle, elapsed = _timed_invocation(harness, role)

    assert _the_line_after_was_read(harness, handle.invocation_id)
    assert handle.invocation_id not in harness.driver._reader_failures
    assert "unpaired surrogate" in harness.driver._transcripts[handle.invocation_id].rejected
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    assert result.stream_order is not None, "the stream was read to its end"
    assert result.stream_order.updates_after_prompt_response == 2
    if role == "reviewer":
        assert any(
            note.startswith("review_invalid") and "unpaired surrogate" in note
            for note in result.limitations
        ), result.limitations
    else:
        assert result.outcome is InvocationOutcome.COMPLETED
    assert elapsed < SLOW_DRAIN_SECONDS / 2, "collect waited for a reader that had died"
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_message_the_driver_cannot_interpret_is_counted_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Whatever ``_note_message`` raises is caught per line: counted as unparseable, the answer
    rejected, and the reader goes on to the next line."""
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", SLOW_DRAIN_SECONDS)
    harness = structured_harness(tmp_path, trailing_updates="tool,usage")
    note_message = harness.driver._note_message

    def failing_note(invocation_id, message, *, line_index=-1):
        if "trailing tool output" in str(message):
            raise UnicodeEncodeError("utf-8", chr(0xD83D), 0, 1, "surrogates not allowed")
        return note_message(invocation_id, message, line_index=line_index)

    harness.driver._note_message = failing_note  # type: ignore[method-assign]
    result, handle, elapsed = _timed_invocation(harness, role)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"
    assert harness.driver.unparsed_line_count(handle.invocation_id) == 1
    assert "UnicodeEncodeError" in harness.driver._transcripts[handle.invocation_id].rejected
    assert _the_line_after_was_read(harness, handle.invocation_id)
    assert result.review is None
    assert elapsed < SLOW_DRAIN_SECONDS / 2
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_reader_that_fails_is_reported_and_its_partial_stream_is_never_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The backstop: anything that still escapes the per-line guards ends the reader, which
    records its failure and sets the drained flag in a ``finally``. ``collect`` then neither waits
    for a dead reader nor folds the prefix it read - the response was already read here, so the
    old fold was COMPLETED."""
    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", SLOW_DRAIN_SECONDS)
    harness = structured_harness(tmp_path, trailing_updates="tool,usage")
    project = harness.driver._project_line

    def failing_project(invocation_id, sink, raw):
        if b"trailing tool output" in raw:
            raise RuntimeError("stand-in for a reader bug")
        return project(invocation_id, sink, raw)

    harness.driver._project_line = failing_project  # type: ignore[method-assign]
    result, handle, elapsed = _timed_invocation(harness, role)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "reader_failed"
    assert "RuntimeError" in (result.error_message or "")
    assert "stand-in for a reader bug" not in (result.error_message or ""), "class name only"
    assert any(note.startswith("reader_failed:") for note in result.limitations)
    assert result.stream_order is None, "a partial stream says nothing about what followed"
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    assert elapsed < SLOW_DRAIN_SECONDS / 2, "collect waited for a reader that had died"
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_reader_that_fails_between_collects_reads_is_never_judged_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The reader records its failure, then sets the drained flag; ``collect`` must read the flag
    first and the failure second. Read the other way round, a reader that failed between the two
    reads gave "no failure" and then "finished", and the partial stream folded as whole (an
    implementer COMPLETED with no ``reader_failed``, a reviewer's verdict decoded from a prefix).

    Production calls ``start_handle`` and then ``collect`` with no ``observe`` loop, so a reader
    still running when the bounded drain wait expires is a real state. The interleaving is forced:
    the reader blocks at the trailing line until ``collect`` makes its first read after the
    teardown; that read releases it and waits until the reader has failed and set the flag.
    """
    import sys

    monkeypatch.setattr(driver_module, "STREAM_DRAIN_TIMEOUT_SECONDS", 0.3)
    harness = structured_harness(tmp_path, trailing_updates="tool,usage")
    drv = harness.driver
    release_reader = threading.Event()
    state = {"armed": False, "fired": False}
    project = drv._project_line

    def failing_project(invocation_id, sink, raw):
        if b"trailing tool output" in raw:
            release_reader.wait(30)
            raise RuntimeError("the reader fails between collect's two reads")
        return project(invocation_id, sink, raw)

    class RaceAtFirstRead(dict):
        """Either dict: ``collect``'s first read after the teardown lets the reader fail."""

        def get(self, key, default=None):
            value = dict.get(self, key, default)
            if (
                state["armed"]
                and not state["fired"]
                and sys._getframe(1).f_code.co_name == "collect"
            ):
                state["fired"] = True
                release_reader.set()
                deadline = time.monotonic() + 30
                while not dict.get(drv._stream_drained, key) and time.monotonic() < deadline:
                    time.sleep(0.01)
            return value

    close_output_handles = driver_module.close_output_handles

    def close_then_arm(process):
        close_output_handles(process)
        state["armed"] = True

    monkeypatch.setattr(driver_module, "close_output_handles", close_then_arm)
    drv._project_line = failing_project  # type: ignore[method-assign]
    drv._stream_drained = RaceAtFirstRead()
    drv._reader_failures = RaceAtFirstRead()
    drv.observe = lambda handle, **kwargs: iter(())  # type: ignore[method-assign]

    result, handle, _elapsed = _timed_invocation(harness, role)

    assert state["fired"], "the forced interleaving did not happen"
    assert "RuntimeError" in dict.get(drv._reader_failures, handle.invocation_id, "")
    assert dict.get(drv._stream_drained, handle.invocation_id) is True
    assert result.stream_order is None, "a partial stream says nothing about what followed"
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    assert any(note.startswith("reader_failed:") for note in result.limitations), result.limitations
    if role == "implementer":
        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert result.error_code == "reader_failed"
    else:
        # The verdict comes from the same one reading of the failure as the outcome.
        assert any(
            note.startswith("review_ambiguous") and "RuntimeError" in note
            for note in result.limitations
        ), result.limitations
    drv.release(handle.invocation_id)


def test_a_surrogate_pair_split_across_two_chunks_is_rejected_fail_closed() -> None:
    """The unpaired-surrogate check is per chunk and does not look ahead: a pair split across two
    ``agent_message_chunk`` updates of one message is refused, even though the joined text would
    be valid. DSH emits one chunk per committed text block, so this split is not expected; if it
    ever happens the reviewer gets no verdict rather than a guessed one."""
    transcript = AnswerTranscript()
    for index, text in enumerate(['{"verdict":"accepted","findings":[]} ok \ud83d', "\ude00"]):
        transcript.observe_update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m-1",
                "content": {"type": "text", "text": text},
            },
            params={},
            sequence=index,
            line_index=index,
        )

    assert transcript.rejected is not None
    assert "line 0" in transcript.rejected and "unpaired surrogate" in transcript.rejected
    assert transcript.final_answer() is None
