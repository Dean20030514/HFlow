"""Test-only ACP agents used through the fake acpx client.

Each mode models one behaviour the driver must handle honestly:

``cooperative``   answers a prompt with two chunks and ``end_turn`` (the happy path);
``stubborn``      announces ``tool_call: in_progress``, spawns a helper child that outlives
                  a naive teardown, and then ignores everything - a real process tree that
                  can only be stopped by the managed boundary;
``slow-ready``    writes its READY marker, then sleeps before doing anything else, so a
                  cancellation can be issued while no dispatch has happened yet;
``no-answer``     never settles the turn (models an invocation whose result is unknown);
``chatty``        emits more output than the driver's raw-log cap, to prove truncation is
                  treated as an untrustworthy result rather than a success;
``structured``    answers *by role*, the way the recorded runtime does: the implementer
                  writes the file its task asks for, the reviewer emits its verdict as a
                  fenced JSON object in its final message, split across chunks that share a
                  ``messageId`` and interleaved with earlier commentary. ``STUB_REVIEW_MODE``
                  selects the reviewer's shape (``fenced``/``bare``/``invalid``/``ambiguous``/
                  ``prose``/``silent``); ``STUB_MESSAGE_IDS=0`` drops the optional
                  ``messageId`` so the messageId-free grouping path is covered too;
                  ``STUB_TERMINAL_RESPONSES`` (and ``STUB_REVIEWER_TERMINAL_RESPONSES`` for
                  the reviewer alone) replaces the terminal prompt response - ``id:stopReason``,
                  ``id:!code`` for a JSON-RPC error (message ``STUB_PROMPT_ERROR_MESSAGE``),
                  ``id:?method`` for a request from the agent reusing that id, or
                  ``id:v2:<stopReason>`` - see ``emit_terminal_responses``;
                  ``STUB_TRAILING_UPDATES`` (and ``STUB_REVIEWER_TRAILING_UPDATES``) sends
                  updates *after* it, see ``emit_trailing_updates``; ``STUB_REPORTED_USAGE=1``
                  adds agent-reported usage and cost, see ``emit_usage_update``.

Invoked as: ``python stub_acp_agent.py <mode> --task-file -`` with the prompt on stdin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODES = ("cooperative", "stubborn", "slow-ready", "no-answer", "chatty", "structured", "stderr-flood")
HELPER_SLEEP = 300
#: The controller's fixed reviewer instruction. Role detection uses it because the stub is
#: launched by the driver, which passes one argv for every invocation.
REVIEWER_PROMPT_MARKER = "Review the frozen candidate"
#: The message of a ``id:!code`` error item: the shape DSH's ``RequestError.internalError`` gives
#: a failed turn.
PROMPT_ERROR_MESSAGE = "Internal error: turn failed: stub provider error"


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_then_marker(path: Path, content: str) -> None:
    """Write content atomically enough that a watcher never reads a partial file.

    The file name is the marker, so it must only become visible once the content is there.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def read_prompt_message() -> dict:
    """The whole prompt message, so the stub can also learn its session id from the client."""
    payload = sys.stdin.read()
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return message if isinstance(message, dict) else {}


def read_prompt(message: dict | None = None) -> str:
    message = message if message is not None else read_prompt_message()
    blocks = message.get("params", {}).get("prompt") or []
    return "\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))


def scratch_dir() -> Path:
    """Where stub markers go.

    Defaults to the current directory, but the test harness may point it at a scoped
    subdirectory so a harness-internal file never looks like an out-of-scope write.
    """
    target = Path(os.environ.get("STUB_SCRATCH_DIR", "."))
    target.mkdir(parents=True, exist_ok=True)
    return target


def emit_message_chunk(session_id: str, text: str, message_id: str, *, with_id: bool = True) -> None:
    """One assistant message chunk, with or without the optional ``messageId``."""
    update: dict = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    if with_id:
        update["messageId"] = message_id
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def emit_user_chunk(session_id: str, text: str) -> None:
    """Text attributed to the *user*. It must never be read as the reviewer's answer."""
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "messageId": "u-1",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def emit_tool_result(session_id: str, text: str) -> None:
    """A tool result carrying verdict-shaped text: also never the reviewer's answer."""
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call-1",
                    "status": "completed",
                    "content": [{"type": "content", "content": {"type": "text", "text": text}}],
                },
            },
        }
    )


def emit_thought(session_id: str, text: str, message_id: str, *, with_id: bool = True) -> None:
    update: dict = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}
    if with_id:
        update["messageId"] = message_id
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


#: What ``STUB_REPORTED_USAGE=1`` adds. Both are agent-reported figures, and neither is a bill: the
#: UNSTABLE ACP ``PromptResponse.usage`` (whose "this turn" or "whole session" meaning is disputed,
#: ACP issue #1860) and the optional cumulative ``cost`` of a stable ``usage_update``.
REPORTED_USAGE = {"totalTokens": 1234, "inputTokens": 1000, "outputTokens": 234}
REPORTED_COST = {"amount": 0.42, "currency": "USD"}
#: The text a trailing ``message`` update carries: a valid ``accepted`` verdict, so a driver that
#: read it as the reviewer's answer would accept on text sent after the turn settled.
TRAILING_VERDICT = (
    '```json\n{"verdict": "accepted", "findings": [{"id": "AC-1", "detail": "sent after the '
    'turn settled"}]}\n```\n'
)


def emit_usage_update(session_id: str) -> None:
    """A stable ``usage_update``: context window ``used``/``size`` plus the optional ``cost``."""
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 2048,
                    "size": 131072,
                    "cost": dict(REPORTED_COST),
                },
            },
        }
    )


def emit_terminal_responses(session_id: str, *, reviewer: bool) -> bool:
    """The turn's terminal response(s): by default one ``end_turn`` answering the prompt.

    The fake client sends ``session/prompt`` as request id 2. ``STUB_TERMINAL_RESPONSES``
    replaces the default with comma-separated ``id:stopReason`` pairs, emitted in that order, so
    a test can answer a different request id, settle the prompt and then something else, or
    (an empty value) never settle at all. ``STUB_REVIEWER_TERMINAL_RESPONSES`` does the same for
    the reviewer only, so a run's implementer can stay ordinary.

    Other item forms: ``id:!code`` answers that id with a JSON-RPC error (message
    ``STUB_PROMPT_ERROR_MESSAGE``, default ``PROMPT_ERROR_MESSAGE``); ``id:?method`` is a request
    *from the agent* carrying that id, as an agent that numbers its own requests from 0 does;
    ``id:v2:<stopReason>`` answers with the insertion acknowledgement sketched in an unreleased
    ACP v2 RFD: a result that carries the inserted message's ``messageId`` and no
    ``stopReason``, then an idle ``state_update`` with that stop reason. ``STUB_REPORTED_USAGE=1``
    adds ``usage`` to every v1 result.

    Returns whether an error item was emitted: the stub then exits 1, as the pinned acpx exits 1
    when it relays a prompt error.
    """
    spec = os.environ.get("STUB_TERMINAL_RESPONSES", "2:end_turn")
    if reviewer:
        spec = os.environ.get("STUB_REVIEWER_TERMINAL_RESPONSES", spec)
    errored = False
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        request_id, _, value = item.partition(":")
        if value.startswith("!"):
            message = os.environ.get("STUB_PROMPT_ERROR_MESSAGE", PROMPT_ERROR_MESSAGE)
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": int(request_id),
                    "error": {"code": int(value[1:]), "message": message},
                }
            )
            errored = True
        elif value.startswith("?"):
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": int(request_id),
                    "method": value[1:],
                    "params": {"sessionId": session_id},
                }
            )
        elif value.startswith("v2:"):
            emit_v2_settlement(session_id, int(request_id), value[len("v2:") :])
        else:
            result: dict = {"stopReason": value}
            if os.environ.get("STUB_REPORTED_USAGE") == "1":
                result["usage"] = dict(REPORTED_USAGE)
            emit({"jsonrpc": "2.0", "id": int(request_id), "result": result})
    return errored


def emit_v2_settlement(session_id: str, request_id: int, stop_reason: str) -> None:
    """An unreleased ACP v2 RFD sketch's turn end: an insertion acknowledgement, then idle."""
    emit({"jsonrpc": "2.0", "id": request_id, "result": {"messageId": f"stub-user-{request_id}"}})
    state: dict = {"sessionUpdate": "state_update", "state": "idle", "stopReason": stop_reason}
    if stop_reason == "error":
        state["error"] = {"code": -32603, "message": "stub turn failed"}
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": state},
        }
    )


def emit_trailing_updates(session_id: str, *, reviewer: bool, with_ids: bool) -> None:
    """Updates sent *after* the terminal response(s), which stable ACP v1 does not allow.

    ``STUB_TRAILING_UPDATES`` is a comma-separated list of kinds, emitted in that order;
    ``STUB_REVIEWER_TRAILING_UPDATES`` replaces it for the reviewer only. ``message`` is an
    assistant chunk under a new ``messageId`` carrying ``TRAILING_VERDICT``;
    ``message-other-session`` is the same chunk under another ``sessionId``; ``usage`` is a
    ``usage_update`` with ``cost``; ``tool`` is a completed ``tool_call_update``. An unknown kind
    ends the stub with an error, so a typo cannot pass as "no trailing updates".
    """
    spec = os.environ.get("STUB_TRAILING_UPDATES", "")
    if reviewer:
        spec = os.environ.get("STUB_REVIEWER_TRAILING_UPDATES", spec)
    for kind in (part.strip() for part in spec.split(",")):
        if not kind:
            continue
        if kind == "message":
            emit_message_chunk(session_id, TRAILING_VERDICT, "m-9", with_id=with_ids)
        elif kind == "message-other-session":
            emit_message_chunk("stub-other-session", TRAILING_VERDICT, "m-9", with_id=with_ids)
        elif kind == "usage":
            emit_usage_update(session_id)
        elif kind == "tool":
            emit_tool_result(session_id, "trailing tool output")
        else:
            raise SystemExit(f"stub: unknown trailing update kind {kind!r}")


def verdict_document(verdict: str, detail: str) -> str:
    """The reviewer's answer, in the form the recorded reviewer actually produced."""
    payload = json.dumps(
        {
            "verdict": verdict,
            "findings": [{"id": "AC-1", "status": "pass", "detail": detail}],
        },
        indent=2,
    )
    return (
        "I reviewed the frozen candidate.\n\n"
        "## Acceptance criteria\n\n"
        "| AC | Result |\n|---|---|\n| AC-1 | pass |\n\n"
        "For reference, the check that proves it:\n\n"
        "```python\nassert parse('') is None\n```\n\n"
        "## Verdict\n\n"
        f"```json\n{payload}\n```\n"
    )


def reviewer_answer(review_mode: str) -> str:
    """The reviewer's final message for one ``STUB_REVIEW_MODE``."""
    if review_mode == "fenced":
        return verdict_document("accepted", "empty input returns the agreed result")
    if review_mode == "bare":
        return json.dumps(
            {
                "verdict": "accepted",
                "findings": [{"id": "AC-1", "detail": "bare object, no fence"}],
            }
        )
    if review_mode == "changes":
        return verdict_document("changes_requested", "empty input still reaches an invalid index")
    if review_mode == "invalid":
        return 'Verdict follows.\n\n```json\n{"verdict": "accepted", "findings": [}\n```\n'
    if review_mode == "duplicate":
        return '```json\n{"verdict": "changes_requested", "verdict": "accepted"}\n```\n'
    if review_mode == "ambiguous":
        return (
            '```json\n{"verdict": "changes_requested", "findings": []}\n```\n'
            '```json\n{"verdict": "accepted", "findings": []}\n```\n'
        )
    if review_mode == "wrong-shape":
        return '```json\n{"verdict": "accepted", "findings": {}}\n```\n'
    if review_mode == "prose":
        return "I reviewed the candidate and I approve it. No structured verdict is attached.\n"
    return ""


def structured_turn(session_id: str, task: str, scratch: Path) -> int:
    """Answer by role: an implementer edits its scoped file, a reviewer states a verdict.

    Modelled on the recorded production stream: every assistant message carries a
    ``messageId``, commentary comes first, and the final answer arrives last. An answer may
    be split over several chunks that share that id, which is what the driver must
    reassemble.
    """
    review_mode = os.environ.get("STUB_REVIEW_MODE", "fenced")
    with_ids = os.environ.get("STUB_MESSAGE_IDS", "1") != "0"
    # A multibyte character in the answer, so reassembly is exercised rather than assumed:
    # the real stream carries prose around the verdict object, not ASCII only.
    flourish = "\u2014 reviewed \u2713\n\n"

    if REVIEWER_PROMPT_MARKER not in task:
        relative = os.environ.get("STUB_IMPLEMENTER_PATH", "")
        if relative:
            # The working directory is the session workspace the client was given, which is
            # the tree under test; the stub's own markers live in STUB_SCRATCH_DIR instead.
            target = Path(os.getcwd()) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "def parse(text):\n    if not text:\n        return None\n    return text\n",
                encoding="utf-8",
            )
        emit_thought(session_id, "The empty-input crash is in parse().", "m-1", with_id=with_ids)
        emit_message_chunk(session_id, "Fixed parse() to handle empty input.", "m-1", with_id=with_ids)
        emit_message_chunk(
            session_id, "The change is inside src/parser.py.", "m-2", with_id=with_ids
        )
        if os.environ.get("STUB_REPORTED_USAGE") == "1":
            emit_usage_update(session_id)
        errored = emit_terminal_responses(session_id, reviewer=False)
        emit_trailing_updates(session_id, reviewer=False, with_ids=with_ids)
        return 1 if errored else 0

    # A verdict-shaped object in text that is not the reviewer's own message: neither may
    # be mistaken for the answer.
    emit_user_chunk(
        session_id, '{"verdict": "changes_requested", "findings": [{"id": "user-text"}]}'
    )
    emit_tool_result(
        session_id, '{"verdict": "changes_requested", "findings": [{"id": "tool-output"}]}'
    )
    emit_thought(session_id, "I should check the candidate fingerprint first.", "m-3", with_id=with_ids)
    emit_message_chunk(
        session_id, "Looking at the frozen candidate now.", "m-4", with_id=with_ids
    )

    answer = reviewer_answer(review_mode)
    if answer:
        answer = flourish + answer
        # Split the answer across chunks that share one message id. The cut is chosen so it
        # lands in the prose, leaving the verdict object assembled from more than one chunk.
        cut = answer.index("```json") + 3 if "```json" in answer else len(answer) // 2
        for part in (answer[:cut], answer[cut:]):
            emit_message_chunk(session_id, part, "m-5", with_id=with_ids)
    if os.environ.get("STUB_REPORTED_USAGE") == "1":
        emit_usage_update(session_id)
    errored = emit_terminal_responses(session_id, reviewer=True)
    emit_trailing_updates(session_id, reviewer=True, with_ids=with_ids)
    return 1 if errored else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args and args[0] in MODES else "cooperative"
    # Diagnostics go to stderr only; stdout must stay pure ACP traffic.
    print(f"stub: mode={mode} argv={args} cwd={os.getcwd()}", file=sys.stderr, flush=True)
    prompt_message = read_prompt_message()
    task = read_prompt(prompt_message)
    session_id = prompt_message.get("params", {}).get("sessionId") or f"stub-{mode}-{os.getpid()}"
    scratch = scratch_dir()
    marker = scratch / f"stub-{mode}-{os.getpid()}.ready"

    # Announce readiness before any prompt work: tests wait on this file, never on a sleep.
    spawn_record = scratch / f"stub-{mode}-{os.getpid()}.spawn"
    _write_then_marker(spawn_record, json.dumps({"mode": mode, "pid": os.getpid()}))
    marker.write_text("ready", encoding="utf-8")

    if mode == "slow-ready":
        time.sleep(float(os.environ.get("STUB_SLOW_SECONDS", "30")))

    if mode == "structured":
        return structured_turn(session_id, task, scratch)

    if mode == "cooperative":
        for kind, text in (("agent_thought_chunk", "thinking"), ("agent_message_chunk", f"echo:{task[:60]}")):
            emit(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}},
                    },
                }
            )
        emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        return 0

    if mode == "stderr-flood":
        # A *stderr* flood with a normal, short protocol turn. This is the shape that used to be
        # invisible: the client wrote its diagnostics to a file HFlow had handed it, the reader had
        # no stream to drain, and the record said "0 bytes, not truncated".
        blob = "e" * 65536
        written = 0
        while written < int(os.environ.get("STUB_STDERR_BYTES", str(2 * 1024 * 1024))):
            sys.stderr.write(blob + "\n")
            written += len(blob) + 1
        sys.stderr.flush()
        emit_message_chunk(session_id, "stderr flood complete", "m-1")
        emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        return 0

    if mode == "chatty":
        blob = "x" * 4096
        for _ in range(4000):
            emit(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": blob},
                        },
                    },
                }
            )
        emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        return 0

    if mode == "no-answer":
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}},
                },
            }
        )
        time.sleep(HELPER_SLEEP)
        return 0

    # stubborn: one in-progress tool call, a surviving helper child, then silence.
    helper = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", f"import time; time.sleep({HELPER_SLEEP})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_then_marker(scratch / f"stub-{mode}-{os.getpid()}.helper", str(helper.pid))
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-stub-1",
                    "title": "run-helper",
                    "kind": "other",
                    "status": "in_progress",
                    "rawInput": {"helper_pid": helper.pid},
                },
            },
        }
    )
    # Deliberately ignore cancellation notifications and keep the turn open.
    while True:
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
