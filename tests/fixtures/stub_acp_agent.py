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
                  treated as an untrustworthy result rather than a success.

Invoked as: ``python stub_acp_agent.py <mode> --task-file -`` with the prompt on stdin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODES = ("cooperative", "stubborn", "slow-ready", "no-answer", "chatty")
HELPER_SLEEP = 300


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


def read_prompt() -> str:
    payload = sys.stdin.read()
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return payload
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args and args[0] in MODES else "cooperative"
    # Diagnostics go to stderr only; stdout must stay pure ACP traffic.
    print(f"stub: mode={mode} argv={args} cwd={os.getcwd()}", file=sys.stderr, flush=True)
    task = read_prompt()
    session_id = f"stub-{mode}-{os.getpid()}"
    scratch = scratch_dir()
    marker = scratch / f"stub-{mode}-{os.getpid()}.ready"

    # Announce readiness before any prompt work: tests wait on this file, never on a sleep.
    spawn_record = scratch / f"stub-{mode}-{os.getpid()}.spawn"
    _write_then_marker(spawn_record, json.dumps({"mode": mode, "pid": os.getpid()}))
    marker.write_text("ready", encoding="utf-8")

    if mode == "slow-ready":
        time.sleep(float(os.environ.get("STUB_SLOW_SECONDS", "30")))

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
