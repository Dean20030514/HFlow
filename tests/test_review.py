"""Review decoding: the grammar, and every shape that must not be accepted.

These are the deterministic rules the production driver and the offline replay share. They
are pure functions over bytes, so a failure here is a parser defect, never model variance -
no test in this file launches a process or reaches a model.
"""

from __future__ import annotations

import json

import pytest

from hflow.contracts import ReviewOutput
from hflow.review import (
    REVIEW_AMBIGUOUS,
    REVIEW_INVALID,
    REVIEW_MISSING,
    MAX_ANSWER_BYTES,
    AnswerTranscript,
    ReviewDecodeError,
    decode_review,
)

FENCE = "```"


def fenced(payload: str, label: str = "json") -> str:
    return f"Here is my verdict.\n\n{FENCE}{label}\n{payload}\n{FENCE}\n"


def update(
    text: str,
    *,
    message_id: str | None = "m-1",
    session_id: str = "sess-1",
    kind: str = "agent_message_chunk",
) -> tuple[dict, dict]:
    payload: dict = {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
    if message_id is not None:
        payload["messageId"] = message_id
    return payload, {"sessionId": session_id, "update": payload}


def transcript(*updates: tuple[dict, dict], session_id: str | None = "sess-1") -> AnswerTranscript:
    instance = AnswerTranscript(session_id=session_id, role="reviewer")
    for sequence, (payload, params) in enumerate(updates):
        instance.observe_update(payload, params=params, sequence=sequence, line_index=sequence)
    return instance


# --------------------------------------------------------------------------
# the supported grammar
# --------------------------------------------------------------------------


def test_a_plain_json_object_is_one_supported_form() -> None:
    review = decode_review('{"verdict": "accepted", "findings": []}')

    assert review == ReviewOutput(verdict="accepted", findings=[])


def test_a_single_fenced_json_block_is_one_supported_form() -> None:
    review = decode_review(fenced('{"verdict": "changes_requested", "findings": [{"id": "F-1"}]}'))

    assert review.verdict == "changes_requested"
    assert review.findings == [{"id": "F-1"}]


def test_prose_may_end_in_one_result_object() -> None:
    answer = (
        "I checked AC-1 and AC-2 against the frozen fingerprint.\n"
        "Nothing outside the declared scope changed.\n\n"
        '{"verdict": "accepted", "findings": [{"id": "AC-1", "status": "pass"}]}\n'
    )

    review = decode_review(answer)

    assert review.verdict == "accepted"
    assert review.findings == [{"id": "AC-1", "status": "pass"}]


def test_the_labelled_fence_wins_over_a_code_sample_in_the_same_answer() -> None:
    """The recorded reviewer showed a python sample *and* a json verdict block."""
    answer = (
        "The fix is:\n\n"
        f"{FENCE}python\nassert parse('') is None\n{FENCE}\n\n"
        "Verdict:\n\n" + fenced('{"verdict": "accepted", "findings": []}').split("\n\n", 1)[1]
    )

    review = decode_review(answer)

    assert review.verdict == "accepted"


@pytest.mark.parametrize("label", ["", "json", "JSON", " json "])
def test_bare_and_labelled_fences_are_both_supported(label: str) -> None:
    review = decode_review(fenced('{"verdict": "accepted", "findings": []}', label=label))

    assert review.verdict == "accepted"


def test_a_verdict_may_carry_no_findings_and_stays_a_verdict() -> None:
    """Acceptance is not reduced to a boolean: the object survives as the contract type."""
    review = decode_review('{"verdict": "accepted"}')

    assert isinstance(review, ReviewOutput)
    assert review.findings == []


def test_unknown_fields_are_refused_rather_than_ignored() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('{"verdict": "accepted", "confidence": 0.9}')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "Extra inputs" in excinfo.value.detail


# --------------------------------------------------------------------------
# shapes that must never be accepted
# --------------------------------------------------------------------------


def test_malformed_json_is_invalid_not_a_verdict() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('```json\n{"verdict": "accepted", "findings": [}\n```')

    assert excinfo.value.kind == REVIEW_INVALID


def test_duplicate_keys_are_refused_even_though_json_would_keep_the_last_one() -> None:
    """``json.loads`` silently keeps the last value; a conflicting object is not a verdict."""
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('```json\n{"verdict": "changes_requested", "verdict": "accepted"}\n```')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "repeats the key" in excinfo.value.detail


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_refused(constant: str) -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(f'{{"verdict": "accepted", "findings": [{{"score": {constant}}}]}}')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "non-finite" in excinfo.value.detail


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "accept"}',
        '{"verdict": true}',
        '{"verdict": "accepted", "findings": {}}',
        "{}",
        '["accepted"]',
    ],
)
def test_wrong_types_enums_and_missing_fields_are_invalid(payload: str) -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(payload)

    assert excinfo.value.kind == REVIEW_INVALID


def test_two_result_objects_are_ambiguous_not_whichever_one_validates() -> None:
    answer = (
        '```json\n{"verdict": "changes_requested", "findings": []}\n```\n\n'
        '```json\n{"verdict": "accepted", "findings": []}\n```\n'
    )

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS


def test_two_bare_objects_are_ambiguous() -> None:
    answer = (
        'Notes: {"checked": "AC-1"}\n'
        '{"verdict": "accepted", "findings": []}\n'
    )

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS


def test_prose_without_any_object_is_missing_not_an_approval() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review("I reviewed everything and I am happy. Approved.")

    assert excinfo.value.kind == REVIEW_MISSING


def test_a_non_json_fence_is_not_read_as_a_result_block() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(f"{FENCE}python\nverdict = 'accepted'\n{FENCE}\n")

    assert excinfo.value.kind == REVIEW_MISSING


def test_an_unterminated_fence_is_invalid() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('```json\n{"verdict": "accepted"}')

    assert excinfo.value.kind == REVIEW_INVALID


def test_a_brace_inside_a_string_is_not_an_object_boundary() -> None:
    """A greedy first-brace/last-brace scan would cut this object in half."""
    review = decode_review('{"verdict": "accepted", "findings": [{"detail": "uses { and } in prose"}]}')

    assert review.findings == [{"detail": "uses { and } in prose"}]


def test_the_verbatim_answer_is_preserved_for_evidence() -> None:
    answer = fenced('{"verdict": "accepted", "findings": []}')
    review = decode_review(answer)

    # The wrapper is removed for decoding only; the caller keeps the original text and can
    # hash it (see the offline replay tool).
    assert review.model_dump() == {"verdict": "accepted", "findings": []}
    assert "```json" in answer


# --------------------------------------------------------------------------
# final-answer reassembly (the framing half)
# --------------------------------------------------------------------------


def test_chunks_of_one_message_are_concatenated_in_order() -> None:
    instance = transcript(
        update("Verdict follows.\n", message_id="m-1"),
        update('{"verdict": "acce', message_id="m-1"),
        update('pted", "findings": []}', message_id="m-1"),
    )

    answer = instance.final_answer()

    assert answer is not None
    assert answer.chunk_count == 3
    assert review_of(instance).verdict == "accepted"


def test_the_last_message_is_the_answer_not_the_commentary() -> None:
    instance = transcript(
        update('{"verdict": "changes_requested", "findings": []}', message_id="early"),
        update("Let me look at the tests.", message_id="middle"),
        update('{"verdict": "accepted", "findings": []}', message_id="final"),
    )

    answer = instance.final_answer()

    assert answer is not None and answer.message_id == "final"
    assert review_of(instance).verdict == "accepted"


def test_without_message_ids_a_turn_boundary_splits_messages() -> None:
    """The fallback rule when the optional ``messageId`` is absent from the runtime."""
    instance = transcript(
        update("Thinking out loud.", message_id=None),
        update('{"verdict": "changes_requested", "findings": []}', message_id=None),
        update("tool_call", message_id=None, kind="tool_call"),
        update("Final answer: ", message_id=None),
        update('{"verdict": "accepted", "findings": []}', message_id=None),
    )

    answer = instance.final_answer()

    assert answer is not None
    # The two contiguous chunks after the tool call form the answer; the earlier object is
    # commentary and is not concatenated into it.
    assert answer.chunk_count == 2
    assert review_of(instance).verdict == "accepted"


def test_a_sequence_gap_starts_a_new_message() -> None:
    instance = AnswerTranscript(session_id="sess-1", role="reviewer")
    first, params = update('{"verdict": "changes_requested"}', message_id=None)
    second, _ = update('{"verdict": "accepted"}', message_id=None)
    instance.observe_update(first, params=params, sequence=0, line_index=0)
    instance.observe_update(second, params=params, sequence=5, line_index=5)

    answer = instance.final_answer()

    assert answer is not None and answer.chunk_count == 1
    assert review_of(instance).verdict == "accepted"


def test_thoughts_tool_output_and_user_text_never_supply_the_answer() -> None:
    instance = transcript(
        update('{"verdict": "accepted"}', message_id="u", kind="user_message_chunk"),
        update('{"verdict": "accepted"}', message_id="t", kind="agent_thought_chunk"),
        update('{"verdict": "accepted"}', message_id="x", kind="tool_call_update"),
        update("no verdict here", message_id="m-1"),
    )

    assert instance.observed_message_count == 1
    with pytest.raises(ReviewDecodeError):
        decode_review(instance.final_answer().text)  # type: ignore[union-attr]


def test_updates_from_another_session_are_excluded() -> None:
    instance = transcript(
        update('{"verdict": "accepted"}', message_id="m-1", session_id="other-session"),
        update("prose without a verdict", message_id="m-2", session_id="sess-1"),
    )

    answer = instance.final_answer()

    assert instance.skipped_other_session == 1
    assert answer is not None and "accepted" not in answer.text


def test_a_message_without_text_content_is_rejected_not_guessed() -> None:
    instance = AnswerTranscript(session_id="sess-1", role="reviewer")
    payload = {"sessionUpdate": "agent_message_chunk", "content": {"type": "image"}}

    instance.observe_update(payload, params={"sessionId": "sess-1"}, sequence=0, line_index=0)

    # A chunk this build cannot read makes the whole answer unusable: the driver records
    # that reason and the review stays absent, rather than decoding a rewritten answer.
    assert instance.rejected != ""
    assert instance.final_answer() is None


def test_an_over_budget_answer_is_unusable_rather_than_truncated() -> None:
    instance = AnswerTranscript(session_id="sess-1", role="reviewer")
    payload, params = update("x" * (MAX_ANSWER_BYTES // 2 + 1), message_id="m-1")
    instance.observe_update(payload, params=params, sequence=0, line_index=0)
    instance.observe_update(payload, params=params, sequence=1, line_index=1)

    assert instance.truncated is True
    assert instance.final_answer() is None
    assert instance._retained_bytes <= MAX_ANSWER_BYTES + MAX_ANSWER_BYTES // 2


def test_a_rejected_transcript_yields_no_answer() -> None:
    instance = transcript(update('{"verdict": "accepted"}', message_id="m-1"))

    instance.reject("a chunk carried no readable text")

    assert instance.final_answer() is None


def test_multibyte_answer_round_trips_exactly() -> None:
    text = "— reviewed ✓\n" + json.dumps({"verdict": "accepted", "findings": [{"d": "café"}]})
    instance = transcript(update(text[:7], message_id="m-1"), update(text[7:], message_id="m-1"))

    answer = instance.final_answer()

    assert answer is not None and answer.text == text
    assert review_of(instance).findings == [{"d": "café"}]


def review_of(instance: AnswerTranscript) -> ReviewOutput:
    answer = instance.final_answer()
    assert answer is not None, "the transcript produced no final answer"
    return decode_review(answer.text)
