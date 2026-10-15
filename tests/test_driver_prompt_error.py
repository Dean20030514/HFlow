"""DSH dsh-v0.2.1-alpha.2's prompt semantics, read by the production driver. Offline.

Upstream (documented from source, not observed; HFlow's recorded DSH is 0.2.0-rc.2):

* an agent failure with no saved ``turn/end`` now makes ``session/prompt`` reject with a JSON-RPC
  error - before, it returned the earlier answer or ``cancelled`` - and headless mode exits
  non-zero (commits 57226b39 / b0237914);
* ``session/prompt`` now returns only after subagents and pending output finish, so a turn's last
  updates can arrive well after its final message (50da6f0b / 7304af57 / 323ec4db).

A rejected prompt arrives after the agent did work - it streamed messages, ran a subagent, and
(the implementer) wrote a file - so the safety property is: never a successful turn, never a
"nothing happened" failure that could pass for unspent work, never re-dispatched. The driver
classifies it ``OUTCOME_UNKNOWN`` in each wire shape a client might print it in (the agent's error
relayed, plus the client's own null-id error line, or that line alone), and the controller blocks
the run with the ledger entry ``unknown``, which keeps its consumption.

Same path as ``test_driver_mid_turn_death.py``: the production driver, the stand-in acpx client
(``STUB_CLIENT_PROMPT_ERROR``) and the stub agent (``STUB_TERMINAL_RESPONSES``,
``STUB_PENDING_OUTPUT``) as real separate processes. A stand-in stream is not a compatibility
proof for any DSH or acpx version.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hflow.contracts import (
    EventKind,
    InvocationOutcome,
    InvocationStartState,
    RefusalCode,
    TaskState,
)
from hflow.controller import Controller
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

from . import test_batch_e_dispatch as dispatch_helpers
from .owner_exit import owner_exits, successor
from .store_helpers import pending_invocations
from .test_driver_mid_turn_death import _chunk_texts, _wire_messages
from .test_review_wire import GOALS, _run, run_invocation, structured_harness

#: How the client prints a rejected prompt: the agent's error relayed alone (the pinned acpx
#: 0.17.1 relay this repo recorded offline, ``test_real_client_review.py``), relayed and followed
#: by the client's own null-id error line, or that line alone with the agent's error withheld.
SHAPES = {
    "relayed": (None, [2]),
    "envelope": ("envelope", [2, None]),
    "envelope-only": ("envelope-only", [None]),
}
#: Two subagent updates after the final message, before the prompt is answered.
PENDING = "2:0"


def _rejecting_harness(tmp_path: Path, shape: str, *, reviewer_only: bool = False):
    responses = {"reviewer_terminal_responses" if reviewer_only else "terminal_responses": "2:!-32603"}
    harness = structured_harness(tmp_path, **responses)
    harness.driver.extra_env["STUB_PENDING_OUTPUT"] = PENDING
    client_mode, _ = SHAPES[shape]
    if client_mode:
        harness.driver.extra_env["STUB_CLIENT_PROMPT_ERROR"] = client_mode
    return harness


def _subagent_updates(messages: list[dict]) -> list[dict]:
    return [
        message["params"]["update"]
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"].get("toolCallId") == "call-subagent-1"
    ]


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_prompt_rejected_after_work_is_unknown_never_completed_or_failed(
    tmp_path: Path, role: str, shape: str
) -> None:
    harness = _rejecting_harness(tmp_path, shape)

    result, handle, _ = run_invocation(harness, role, GOALS[role])

    # Not vacuous: the prompt was sent, the agent streamed its message and ran a subagent to
    # completion, the implementer changed a file, nothing settled the prompt with a stop reason,
    # and the error lines are exactly the shape under test. The client exited 1.
    messages = _wire_messages(harness, handle.invocation_id)
    assert handle.dispatched is True
    assert _chunk_texts(messages), "the agent streamed its message before the rejection"
    assert [update["status"] for update in _subagent_updates(messages)] == [
        "in_progress",
        "in_progress",
        "in_progress",
        "completed",
    ]
    if role == "implementer":
        assert (harness.workspace / "src" / "parser.py").is_file(), "the agent did work"
    assert not any(
        isinstance(message.get("result"), dict) and "stopReason" in message["result"]
        for message in messages
    ), messages
    errors = [m for m in messages if "error" in m and not isinstance(m.get("method"), str)]
    assert [error.get("id") for error in errors] == SHAPES[shape][1]
    assert all(error["error"]["code"] == -32603 for error in errors)
    assert harness.driver._processes[handle.invocation_id].returncode == 1

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    if shape == "envelope-only":
        # The client's own line answers no request, so nothing ties it to the prompt: the turn
        # simply never settled. Its message is kept in the event log only.
        assert result.error_code == "no_stop_reason"
        assert result.error_message == "client exited 1 before the turn settled"
        assert not any(note.startswith("prompt_error") for note in result.limitations)
    else:
        assert result.error_code == "prompt_error_response", result.error_message
        assert "-32603" in (result.error_message or "")
        assert "turn failed: stub provider error" in (result.error_message or "")
        assert any(
            note.startswith("prompt_error_response:") and "not observable" in note
            for note in result.limitations
        )
    assert result.review is None
    assert not any(note.startswith("review_decoded") for note in result.limitations)
    kinds = {event.kind for event in harness.driver.events(handle.invocation_id)}
    assert EventKind.COMPLETED not in kinds, kinds
    # The event projection names each error line FAILED; the outcome is not taken from it.
    assert EventKind.FAILED in kinds
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("shape", ["envelope", "envelope-only"])
@pytest.mark.parametrize("failing_role", ["implementer", "reviewer"])
def test_a_run_whose_prompt_is_rejected_blocks_unknown_keeps_its_spend_and_is_not_redispatched(
    tmp_path: Path,
    store: Store,
    project,
    task_spec,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_role: str,
    shape: str,
) -> None:
    """A root-bound run, so the dispatch ledger and the root's counters are the real ones."""
    harness = _rejecting_harness(tmp_path, shape, reviewer_only=failing_role == "reviewer")
    binding = dispatch_helpers._binding(store, task_spec, project_root)
    limits = dispatch_helpers._limits()
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=harness.data_dir,
        authorization=dispatch_helpers._authorization(
            spec=task_spec, binding=binding, limits=limits, project_root=project_root
        ),
        preflight=lambda: (True, "the zero-model pre-flight is not what this test measures"),
        root_binding=binding,
        root_limits=limits,
        production=False,
    )
    try:
        outcome = _run(controller, project, task_spec, project_root)
        run_id = outcome.run_id
        review_evidence = store.evidence_for(run_id, "review")
        entries = store.invocations_for(run_id)
        attempts = len(store.attempts_for(run_id))
        launched = len(harness.driver._handles)
        used = store.root_budget_view(binding.root_id).used_top_level_submissions

        owner_exits(store, run_id, controller, monkeypatch)
        resumed = successor(controller).resume(run_id)
        after = store.invocations_for(run_id)
        attempts_after = len(store.attempts_for(run_id))
        used_after = store.root_budget_view(binding.root_id).used_top_level_submissions
        pending = [entry.role for entry in pending_invocations(store, binding.root_id)]
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.OUTCOME_UNKNOWN, outcome.block_reason
    assert outcome.receipt is None
    assert review_evidence == []
    assert outcome.implementer_invocations == 1
    assert outcome.reviewer_invocations == (1 if failing_role == "reviewer" else 0)
    assert runner.calls == ([] if failing_role == "implementer" else ["unit", "docs-check"])
    # The rejected turn's ledger entry is unknown: its consumption stands and it blocks the root,
    # never a settled failure that reads as nothing spent.
    (failed,) = [entry for entry in entries if entry.role == failing_role]
    assert failed.state is InvocationStartState.UNKNOWN, failed
    assert failed.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    # One top-level submission per dispatched role, each spent and kept.
    assert attempts == 1 and used == len(entries) == (1 if failing_role == "implementer" else 2)

    # Reconciled once its owner has gone: still unknown, still spent, still blocking, and nothing
    # is launched again.
    assert resumed.block_code is RefusalCode.OUTCOME_UNKNOWN, resumed.block_reason
    assert len(harness.driver._handles) == launched
    assert len(after) == len(entries) and attempts_after == attempts
    (still,) = [entry for entry in after if entry.role == failing_role]
    assert still.state is InvocationStartState.UNKNOWN
    assert used_after == used
    assert pending == [failing_role]


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
def test_a_turn_that_waits_for_subagent_output_settles_normally_within_its_deadline(
    tmp_path: Path, role: str
) -> None:
    """The prompt is answered only after a subagent's slow output, then ``end_turn``."""
    harness = structured_harness(tmp_path)
    harness.driver.extra_env["STUB_PENDING_OUTPUT"] = "3:0.5"

    started = time.monotonic()
    result, handle, request = run_invocation(harness, role, GOALS[role])
    elapsed = time.monotonic() - started

    messages = _wire_messages(harness, handle.invocation_id)
    response_line = next(
        index
        for index, message in enumerate(messages)
        if isinstance(message.get("result"), dict) and "stopReason" in message["result"]
    )
    subagent_lines = [
        index
        for index, message in enumerate(messages)
        if message.get("method") == "session/update"
        and message["params"]["update"].get("toolCallId") == "call-subagent-1"
    ]
    assert len(subagent_lines) == 5 and max(subagent_lines) < response_line
    assert 1.5 <= elapsed < request.deadline_seconds, "a long turn, inside its deadline"

    assert result.outcome is InvocationOutcome.COMPLETED, result.error_message
    assert result.error_code is None
    assert result.stream_order is not None
    assert result.stream_order.updates_after_prompt_response == 0
    if role == "reviewer":
        # Tool updates after the final message are not answer text: the verdict still decodes.
        assert result.review is not None
        assert result.review.verdict == "accepted"
    harness.driver.release(handle.invocation_id)


def test_a_turn_whose_pending_output_outlasts_the_wait_is_stopped_and_unknown(
    tmp_path: Path,
) -> None:
    """Longer alpha.2 turns are not judged early: when the wait runs out, the boundary stops it."""
    harness = structured_harness(tmp_path)
    harness.driver.extra_env["STUB_PENDING_OUTPUT"] = "1:60"
    harness.driver.completion_timeout_seconds = 2

    result, handle, _ = run_invocation(harness, "implementer", GOALS["implementer"])

    assert handle.dispatched is True
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "completion_timeout"
    assert result.review is None
    kinds = {event.kind for event in harness.driver.events(handle.invocation_id)}
    assert EventKind.COMPLETED not in kinds
    harness.driver.release(handle.invocation_id)
