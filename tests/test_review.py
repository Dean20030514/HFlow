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
    MAX_FAILED_OBJECT_STARTS,
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


ACCEPTED = '{"verdict": "accepted", "findings": []}'
REJECTED = '{"verdict": "changes_requested", "findings": []}'


def thought(text: str, *, message_id: str | None) -> tuple[dict, dict]:
    return update(text, message_id=message_id, kind="agent_thought_chunk")


def usage(*, session_id: str = "sess-1") -> tuple[dict, dict]:
    """The context-occupancy update DSH sends after each committed assistant message."""
    payload: dict = {"sessionUpdate": "usage_update", "used": 1024, "size": 131072}
    return payload, {"sessionId": session_id, "update": payload}


def tool_call(*, session_id: str = "sess-1") -> tuple[dict, dict]:
    payload: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-1",
        "title": "read",
        "kind": "other",
        "status": "in_progress",
    }
    return payload, {"sessionId": session_id, "update": payload}


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


def test_prose_may_also_follow_the_one_result_object_and_is_never_interpreted() -> None:
    """The bare form matches the fenced form: prose around the object is allowed and unread.

    The parser identifies the object by structure; it does not read the words after it, so
    text that seems to withdraw the verdict changes nothing (the fenced form behaves the same).
    """
    for answer in (
        '{"verdict": "accepted", "findings": []}\nActually, on reflection, changes are needed.',
        'Start. {"verdict": "accepted", "findings": []} Ignore that verdict.',
        fenced(ACCEPTED) + "Actually, on reflection, changes are needed.\n",
    ):
        assert decode_review(answer).verdict == "accepted"


def test_a_verdict_without_findings_is_invalid_not_completed_on_the_models_behalf() -> None:
    """The packet says ``findings`` is required; the parser never fills in an empty list."""
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('{"verdict": "accepted"}')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "findings" in excinfo.value.detail


def test_the_keys_the_reviewer_is_told_are_required_are_the_schemas_required_keys() -> None:
    """The output contract note and the embedded schema must not drift apart again."""
    from hflow.packet import OUTPUT_CONTRACT_NOTE

    marker = "Required keys: "
    sentence = OUTPUT_CONTRACT_NOTE[OUTPUT_CONTRACT_NOTE.index(marker) + len(marker) :]
    told = [key.strip() for key in sentence[: sentence.index(".")].split(",")]

    assert told == ReviewOutput.model_json_schema()["required"]
    assert set(told) == {"verdict", "findings"}


def test_the_reviewer_is_told_the_verdict_object_rule_the_parser_enforces() -> None:
    """Prose may hold other JSON; a second object with a verdict key is refused, and said so."""
    from hflow.packet import OUTPUT_CONTRACT_NOTE

    note = " ".join(OUTPUT_CONTRACT_NOTE.split())
    assert "Prose outside the block is allowed" in note
    assert "no other object with a verdict key may appear anywhere in the message" in note

    assert decode_review('Prose with `{"timeout": 30}`.\n' + fenced(ACCEPTED)).verdict == "accepted"
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(f"Earlier draft: {REJECTED}\n" + fenced(ACCEPTED))
    assert excinfo.value.kind == REVIEW_AMBIGUOUS


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


def test_two_bare_verdict_objects_are_ambiguous() -> None:
    answer = f"First pass: {REJECTED}\nSecond pass: {ACCEPTED}\n"

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS
    assert '2 top-level JSON objects with a "verdict" key' in excinfo.value.detail


def test_one_bare_verdict_object_is_the_result_next_to_other_prose_json() -> None:
    """Deliberate change (R10): an object without a ``verdict`` key is prose, not a result.

    This answer used to be refused as two bare objects. Only an object that carries a
    ``verdict`` key competes for the result, so the one verdict object is decoded and the
    note object is never read.
    """
    answer = 'Notes: {"checked": "AC-1"}\n' + ACCEPTED + "\n"

    assert decode_review(answer).verdict == "accepted"


def test_several_objects_without_a_verdict_key_are_ambiguous() -> None:
    answer = 'Notes: {"checked": "AC-1"} and {"checked": "AC-2"}\n'

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS
    assert 'none with a "verdict" key' in excinfo.value.detail


def test_one_object_with_a_misspelt_verdict_key_is_invalid_not_missing() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('My answer: {"verdikt": "accepted", "findings": []}\n')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "verdikt" in excinfo.value.detail


@pytest.mark.parametrize("bare_first", [True, False], ids=["bare-before-fence", "bare-after-fence"])
def test_a_bare_object_next_to_a_result_block_is_ambiguous(bare_first: bool) -> None:
    """A conflicting verdict outside the json fence is not silently discarded."""
    bare = '{"verdict": "changes_requested", "findings": [{"statement": "still broken"}]}\n'
    block = f"{FENCE}json\n{ACCEPTED}\n{FENCE}\n"
    answer = bare + block if bare_first else block + bare

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS
    assert "outside it" in excinfo.value.detail


def test_a_verdict_object_outside_the_result_block_makes_it_ambiguous() -> None:
    """Updated for R10: an object outside the block competes only when it has a ``verdict`` key.

    The previous rule counted every JSON object, which refused contract-following answers
    whose prose mentions ``{}``; the reviewer packet allows prose outside the block. Any
    object carrying a ``verdict`` key - even one that would not validate - still competes.
    """
    for outside in ('{"verdict": "accept"}', '{"id": "AC-1", "verdict": "changes_requested"}'):
        answer = f"I checked {outside} first.\n" + fenced(ACCEPTED)

        with pytest.raises(ReviewDecodeError) as excinfo:
            decode_review(answer)

        assert excinfo.value.kind == REVIEW_AMBIGUOUS


@pytest.mark.parametrize(
    "answer",
    [
        "The handler now returns `{}` on a missing key.\n\n" + fenced(ACCEPTED),
        'Setting `{"timeout": 30}` in config is honoured.\n\n' + fenced(ACCEPTED),
        fenced(ACCEPTED) + "Note: the parser still rejects `{}` input.\n",
        'I checked {"id": "AC-1"} first.\n' + fenced(ACCEPTED),
    ],
    ids=["empty-object-before", "config-object-before", "empty-object-after", "note-object"],
)
def test_other_json_in_prose_next_to_a_result_block_is_allowed(answer: str) -> None:
    """R10: JSON that is not a verdict object is prose, as the reviewer packet promises."""
    assert decode_review(answer).verdict == "accepted"


REJECTED_WITH_FINDING = (
    '{"verdict": "changes_requested", "findings": '
    '[{"severity": "high", "summary": "broken", "file": "a.py"}]}'
)


@pytest.mark.parametrize(
    "answer",
    [
        "The format string uses an unclosed { placeholder.\n"
        + REJECTED_WITH_FINDING
        + f"\n{FENCE}json\n{ACCEPTED}\n{FENCE}\n",
        'Checked the 27" layout. ' + REJECTED_WITH_FINDING + f"\n{FENCE}json\n{ACCEPTED}\n{FENCE}\n",
        'The 27" monitor note. '
        + ACCEPTED
        + ' Then I reconsidered " and the real answer: '
        + REJECTED_WITH_FINDING
        + "\n",
    ],
    ids=["unbalanced-brace", "stray-quote-next-to-fence", "stray-quotes-two-bare-objects"],
)
def test_stray_prose_characters_do_not_hide_a_competing_verdict(answer: str) -> None:
    """R13: an inch mark or an unbalanced brace in prose must not hide a bare rejection.

    The old scanner tracked JSON string and depth state across the prose, so the object after
    a stray ``"`` or ``{`` was never counted and a rejection could become an acceptance.
    """
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_AMBIGUOUS


def test_failed_object_starts_are_bounded_and_refused_past_the_bound() -> None:
    """Each failed decode costs time proportional to the answer, so their number is bounded.

    Past the bound the answer is refused - never accepted - because a competing verdict may
    not have been looked for.
    """
    noise = 'and {"a": x} '
    within = noise * MAX_FAILED_OBJECT_STARTS + "\n" + fenced(ACCEPTED)
    beyond = noise * (MAX_FAILED_OBJECT_STARTS + 1) + "\n" + fenced(ACCEPTED)

    assert decode_review(within).verdict == "accepted"
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(beyond)
    assert excinfo.value.kind == REVIEW_AMBIGUOUS
    assert "search for a competing verdict stopped" in excinfo.value.detail


def test_a_long_run_of_prose_braces_is_classified_in_linear_time() -> None:
    """``{`` not followed by a key or ``}`` cannot start an object and is never decoded.

    A decode attempt at each of these used to cost time proportional to its offset (the
    decoder's error computes a line number), so this answer took tens of seconds.
    """
    import time

    answer = "{" * 256 * 1024 + "\n" + fenced(REJECTED)

    started = time.perf_counter()
    review = decode_review(answer)

    assert review.verdict == "changes_requested"
    assert time.perf_counter() - started < 10


def test_a_lone_prose_brace_is_invalid_with_the_decoders_message() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review("The helper {x} is fine and I approve.")

    assert excinfo.value.kind == REVIEW_INVALID
    assert "not valid JSON" in excinfo.value.detail


def test_an_object_nested_in_a_verdict_object_is_part_of_it_not_a_competitor() -> None:
    answer = (
        'Verdict: {"verdict": "changes_requested", "findings": '
        '[{"verdict": "accepted", "note": "quoted from the old review"}]}\n'
    )

    review = decode_review(answer)

    assert review.verdict == "changes_requested"
    assert review.findings == [{"verdict": "accepted", "note": "quoted from the old review"}]


def test_prose_braces_that_are_not_json_do_not_make_an_answer_ambiguous() -> None:
    for answer in (
        "The helper {x} and the set {a, b} are fine.\n" + fenced(ACCEPTED),
        "The helper {x} is fine.\n" + ACCEPTED + "\n",
    ):
        assert decode_review(answer).verdict == "accepted"


def test_one_malformed_bare_object_is_invalid_not_missing() -> None:
    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review('Verdict: {"verdict": "accepted", "findings": [}')

    assert excinfo.value.kind == REVIEW_INVALID
    assert "not valid JSON" in excinfo.value.detail


@pytest.mark.parametrize("label", ["text", "python"])
def test_an_example_object_inside_a_labelled_fence_is_never_the_verdict(label: str) -> None:
    """The fail-open case: a rejection in prose that shows the format as an example."""
    answer = (
        "I found a blocking defect: the function returns None. The required format is:\n"
        f"{FENCE}{label}\n{ACCEPTED}\n{FENCE}\n"
        "I cannot accept this."
    )

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(answer)

    assert excinfo.value.kind == REVIEW_MISSING
    assert "no top-level JSON object" in excinfo.value.detail


def test_a_labelled_example_plus_one_bare_object_decodes_the_bare_object() -> None:
    answer = (
        "The format is:\n"
        f"{FENCE}python\n{ACCEPTED}\n{FENCE}\n"
        "My verdict:\n"
        f"{REJECTED}\n"
    )

    assert decode_review(answer).verdict == "changes_requested"


def test_a_labelled_example_next_to_a_result_block_is_not_ambiguous() -> None:
    answer = f"Example:\n{FENCE}text\n{REJECTED}\n{FENCE}\n" + fenced(ACCEPTED)

    assert decode_review(answer).verdict == "accepted"


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
    first, params = update(REJECTED, message_id=None)
    second, _ = update(ACCEPTED, message_id=None)
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


@pytest.mark.parametrize("message_id", ["m-1", None])
def test_a_dsh_message_answers_with_its_text_not_its_reasoning(message_id: str | None) -> None:
    """The live shape: [thought(id), message(id), usage] - the reasoning is never the answer."""
    instance = transcript(
        thought(REJECTED, message_id=message_id),
        update(ACCEPTED, message_id=message_id),
        usage(),
    )

    answer = instance.final_answer()

    assert answer is not None and answer.text == ACCEPTED
    assert (answer.chunk_count, answer.first_line, answer.last_line) == (1, 1, 1)
    assert instance.observed_message_count == 1
    assert review_of(instance).verdict == "accepted"


def test_text_blocks_split_by_their_own_reasoning_are_one_answer() -> None:
    """DSH emits [text, reasoning, text] under one messageId: the thought does not end it."""
    prose = "I checked AC-1 against the frozen fingerprint.\n"
    instance = transcript(
        update(prose, message_id="m-1"),
        thought(REJECTED, message_id="m-1"),
        update(ACCEPTED, message_id="m-1"),
        usage(),
    )

    answer = instance.final_answer()

    assert answer is not None
    # Plain concatenation in stream order: no separator, no thought text.
    assert answer.text == prose + ACCEPTED
    assert (answer.message_id, answer.chunk_count, answer.first_line, answer.last_line) == (
        "m-1",
        2,
        0,
        2,
    )
    assert instance.observed_message_count == 1
    assert review_of(instance).verdict == "accepted"


def test_a_verdict_before_the_messages_own_reasoning_is_not_dropped() -> None:
    """Reading only the fragment after the thought used to report this as review_missing."""
    instance = transcript(
        update(ACCEPTED + "\n", message_id="m-1"),
        thought("Done reviewing.", message_id="m-1"),
        update("All criteria pass.", message_id="m-1"),
        usage(),
    )

    answer = instance.final_answer()

    assert answer is not None and answer.text == ACCEPTED + "\nAll criteria pass."
    assert review_of(instance).verdict == "accepted"


def test_conflicting_verdicts_inside_one_message_are_ambiguous_not_the_last_one() -> None:
    """The core regression: the last fragment of one message used to decide the verdict."""
    instance = transcript(
        update(REJECTED + "\n", message_id="m-1"),
        thought("On reflection the tests pass.", message_id="m-1"),
        update(ACCEPTED, message_id="m-1"),
        usage(),
    )

    with pytest.raises(ReviewDecodeError) as excinfo:
        decode_review(instance.final_answer().text)  # type: ignore[union-attr]

    assert excinfo.value.kind == REVIEW_AMBIGUOUS


def test_a_message_id_names_one_message_across_other_updates() -> None:
    """Only a change of messageId starts a new message; other updates do not split one."""
    instance = transcript(
        update("Verdict:\n", message_id="m-1"),
        usage(),
        tool_call(),
        update(ACCEPTED, message_id="m-1"),
    )

    answer = instance.final_answer()

    assert answer is not None and answer.text == "Verdict:\n" + ACCEPTED
    assert instance.observed_message_count == 1


def test_thoughts_and_usage_updates_do_not_change_the_answer() -> None:
    turn = [
        thought("Let me read the candidate.", message_id="x"),
        update("Reading the candidate.", message_id="x"),
        usage(),
        tool_call(),
        update('{"verdict": "accepted"}', message_id=None, kind="tool_call_update"),
        thought("The checks pass.", message_id="y"),
        update("Verdict:\n", message_id="y"),
        thought(REJECTED, message_id="y"),
        update(ACCEPTED, message_id="y"),
        usage(),
    ]
    reference = [
        item
        for item in turn
        if item[0]["sessionUpdate"] not in {"agent_thought_chunk", "usage_update"}
    ]

    answer = transcript(*turn).final_answer()
    expected = transcript(*reference).final_answer()

    assert answer is not None and expected is not None
    assert (answer.message_id, answer.text, answer.chunk_count) == (
        expected.message_id,
        expected.text,
        expected.chunk_count,
    )
    assert answer.text == "Verdict:\n" + ACCEPTED


def test_without_message_ids_a_thought_still_ends_the_message() -> None:
    """The messageId-free fallback is unchanged: a thought is the demonstrable boundary."""
    instance = transcript(
        update(REJECTED, message_id=None),
        thought("Let me look again.", message_id=None),
        update(ACCEPTED, message_id=None),
    )

    answer = instance.final_answer()

    assert answer is not None and answer.text == ACCEPTED
    assert answer.chunk_count == 1
    assert instance.observed_message_count == 2


def test_a_message_id_that_returns_after_another_message_is_refused() -> None:
    """Which text is the final message is not identified, so nothing is guessed."""
    instance = transcript(
        update(REJECTED, message_id="m-1"),
        update("Checking the tests.", message_id="m-2"),
        update(ACCEPTED, message_id="m-1"),
        update(" more", message_id="m-1"),
    )

    # The first reason stands: the fourth chunk does not overwrite it with line 3.
    assert "line 2 continues message 'm-1'" in instance.rejected
    assert instance.final_answer() is None
    assert instance.observed_message_count == 0


def test_updates_from_another_session_are_excluded() -> None:
    instance = transcript(
        update('{"verdict": "accepted"}', message_id="m-1", session_id="other-session"),
        update("prose without a verdict", message_id="m-2", session_id="sess-1"),
    )

    answer = instance.final_answer()

    assert instance.skipped_other_session == 1
    assert answer is not None and "accepted" not in answer.text


def _observe(instance: AnswerTranscript, *updates: tuple[dict, dict], start: int = 0) -> None:
    for offset, (payload, params) in enumerate(updates):
        line = start + offset
        instance.observe_update(payload, params=params, sequence=line, line_index=line)


def test_a_bound_transcript_rejects_any_other_session() -> None:
    """What the driver does: bind on the first prompt; any other session's chunk is unusable.

    A chunk with no ``sessionId`` belongs to no session, so it counts too. A later binding does
    not move the turn to another session, and the reviewer's own rejection does not survive an
    ``accepted`` chunk that another session streamed after it: nothing decides the answer.
    """
    instance = AnswerTranscript(role="reviewer", require_session=True)
    instance.bind_session("sess-1")
    instance.bind_session("sess-2")
    no_session = update(ACCEPTED, message_id="m-3")
    del no_session[1]["sessionId"]
    _observe(
        instance,
        update(REJECTED, message_id="m-1"),
        update(ACCEPTED, message_id="m-2", session_id="sess-2"),
        no_session,
    )

    assert instance.session_id == "sess-1"
    assert instance.skipped_other_session == 2
    assert "line 1 names session 'sess-2', not the turn's 'sess-1'" in instance.rejected
    assert instance.final_answer() is None


def test_a_message_chunk_before_the_binding_rejects_a_transcript_that_requires_one() -> None:
    instance = AnswerTranscript(role="reviewer", require_session=True)
    _observe(instance, update(ACCEPTED, message_id="m-0"))
    instance.bind_session("sess-1")
    _observe(instance, update(ACCEPTED, message_id="m-1"), start=1)

    assert "line 0 arrived before the session/prompt request" in instance.rejected
    assert instance.final_answer() is None


def test_a_non_message_update_before_the_binding_changes_nothing() -> None:
    """Only a message chunk needs a session to be attributed; a usage update is not answer text."""
    instance = AnswerTranscript(role="reviewer", require_session=True)
    _observe(instance, usage())
    instance.bind_session("sess-1")
    _observe(instance, update(ACCEPTED, message_id="m-1"), start=1)

    assert instance.rejected == ""
    assert review_of(instance).verdict == "accepted"


def test_a_prompt_that_names_no_session_leaves_nothing_attributable() -> None:
    instance = AnswerTranscript(role="reviewer", require_session=True)
    instance.bind_session("")
    _observe(instance, update(ACCEPTED, message_id="m-1"))

    assert "named no sessionId" in instance.rejected
    assert instance.final_answer() is None


def test_a_transcript_without_a_session_requirement_reads_every_session() -> None:
    """The unbound form saved bytes are replayed with in tests: no filter, nothing rejected."""
    instance = transcript(
        update(REJECTED, message_id="m-1"),
        update(ACCEPTED, message_id="m-2", session_id="sess-2"),
        session_id=None,
    )

    assert instance.skipped_other_session == 0
    assert review_of(instance).verdict == "accepted"


def test_chunks_after_counts_only_retained_message_chunks_past_a_line() -> None:
    """What the driver asks with the line of the turn's own prompt response."""
    instance = transcript(
        update("commentary", message_id="m-1"),
        update("thinking", message_id="t-1", kind="agent_thought_chunk"),
        update('{"verdict": "accepted"}', message_id="m-2", session_id="other-session"),
        update("final", message_id="m-3"),
        update("after", message_id="m-4"),
    )

    # Lines 0, 3 and 4 hold retained message chunks: the thought is not one, and the transcript
    # skipped the other session's chunk.
    assert instance.chunks_after(-1) == 3
    assert instance.chunks_after(3) == 1
    assert instance.chunks_after(4) == 0


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
