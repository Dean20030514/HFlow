"""An agent that dies mid-turn: a partial message, client exit 1, no response of any kind.

Prior art: acpx issue #770. Under ``exec --format json`` the pinned acpx 0.17.1 streams the
agent's partial ``agent_message_chunk`` updates when the agent process dies mid-turn, then exits
1 - and writes **no** JSON-RPC error envelope and **no** ``stopReason``. (Fixed upstream in
v0.19.3; HFlow keeps the 0.17.1 pin, so this is the shape its production driver must read.)

Nothing in that stream settles the turn, so for both roles the invocation is
``OUTCOME_UNKNOWN`` - never ``FAILED`` (a failure is a known ending, and none was reported) and
never ``COMPLETED`` - and no verdict is read from the text the reviewer streamed before it died,
even when that text is a complete, valid ``accepted`` verdict.

Offline, through the production driver, the stand-in acpx client and the stub agent
(``STUB_DIE_MID_TURN`` / ``STUB_REVIEWER_DIE_MID_TURN``), the same path ``test_review_wire.py``
and ``test_driver_turn_settlement.py`` use. The agent and the client are real separate processes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import (
    EventKind,
    InvocationOutcome,
    RefusalCode,
    TaskState,
)

from .test_driver_turn_settlement import _run_task
from .test_review_wire import GOALS, run_invocation, structured_harness


def _dying_harness(tmp_path: Path, *, stage: str, reviewer_only: bool = False):
    harness = structured_harness(tmp_path)
    variable = "STUB_REVIEWER_DIE_MID_TURN" if reviewer_only else "STUB_DIE_MID_TURN"
    harness.driver.extra_env[variable] = f"{stage}:1"
    return harness


def _wire_messages(harness, invocation_id: str) -> list[dict]:
    """Every retained protocol line, decoded: the stream the driver judged."""
    messages = []
    for line in harness.driver.raw_lines(invocation_id):
        decoded = json.loads(line)
        if isinstance(decoded, dict):
            messages.append(decoded)
    return messages


def _chunk_texts(messages: list[dict]) -> list[str]:
    return [
        str(message["params"]["update"]["content"]["text"])
        for message in messages
        if message.get("method") == "session/update"
        and message.get("params", {}).get("update", {}).get("sessionUpdate")
        == "agent_message_chunk"
    ]


@pytest.mark.parametrize("stage", ["partial", "answer"])
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_an_agent_that_dies_mid_turn_is_unknown_never_failed_or_completed(
    tmp_path: Path, role: str, stage: str
) -> None:
    harness = _dying_harness(tmp_path, stage=stage)

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    # Not vacuous: the stream is exactly the #770 shape. The prompt was sent, message chunks
    # arrived, the client exited 1, and nothing answered the prompt - no stop reason and no
    # JSON-RPC error envelope from either the agent or the client.
    messages = _wire_messages(harness, handle.invocation_id)
    assert handle.dispatched is True
    assert _chunk_texts(messages), "the agent streamed part of its message before it died"
    assert not any("error" in message for message in messages), messages
    assert not any(
        isinstance(message.get("result"), dict) and "stopReason" in message["result"]
        for message in messages
    ), messages
    assert harness.driver._processes[handle.invocation_id].returncode == 1

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.outcome is not InvocationOutcome.FAILED
    assert result.error_code == "no_stop_reason"
    assert result.error_message == "client exited 1 before the turn settled"
    assert result.stream_order is None, "no bound response, so nothing to count from"
    assert not any(note.startswith("prompt_error") for note in result.limitations)
    kinds = {event.kind for event in harness.driver.events(handle.invocation_id)}
    assert not kinds & {EventKind.COMPLETED, EventKind.FAILED, EventKind.CANCELLED}, kinds

    # No verdict from the text the reviewer streamed before it died - for ``answer`` that text
    # is a complete, valid ``accepted`` verdict object.
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    if role == "reviewer":
        streamed = "".join(_chunk_texts(messages))
        if stage == "answer":
            assert '"verdict": "accepted"' in streamed, "the whole verdict was streamed"
        assert any(
            note.startswith("review_missing: the reviewer turn did not complete (outcome_unknown)")
            for note in result.limitations
        ), result.limitations
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("dying_role", ["implementer", "reviewer"])
def test_a_run_whose_agent_dies_mid_turn_blocks_unknown_and_is_never_redispatched(
    tmp_path: Path, project, task_spec, project_root: Path, dying_role: str
) -> None:
    """Through the controller: unknown means stop. No receipt, no review evidence, no retry."""
    harness = _dying_harness(
        tmp_path, stage="answer", reviewer_only=dying_role == "reviewer"
    )

    outcome, evidence, inspection, runner = _run_task(
        harness, tmp_path, project, task_spec, project_root
    )

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert evidence == [], "no verdict, so no review evidence"
    assert outcome.implementer_invocations == 1
    assert outcome.reviewer_invocations == (1 if dying_role == "reviewer" else 0)
    assert runner.calls == ([] if dying_role == "implementer" else ["unit", "docs-check"])
    assert len(inspection.attempts) == 1
