"""Test-only stub agent for the offline self-check of the live forced-stop probe.

It plays the part DSH plays in the real trial: it starts the fixed helper **itself**, in the
foreground, and then reports ACP progress while the helper runs. The probe under test must
still prove ownership through the OS (job membership + parent chain), and must stop the
helper through the driver's boundary.

This file exists so the probe's evidence chain can be exercised without a model call. It is
never part of the production path, and a trial that uses it can only ever be INCONCLUSIVE.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_prompt() -> str:
    payload = sys.stdin.read()
    try:
        return str(json.loads(payload).get("params", {}).get("prompt", [{}])[0].get("text", ""))
    except (json.JSONDecodeError, IndexError, AttributeError):
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="helper")
    parser.add_argument("--helper", required=True)
    parser.add_argument("--lifetime", type=float, default=30.0)
    # Accepted and ignored: the stand-in client appends `--task-file -` like the real CLI,
    # and the prompt arrives on stdin.
    parser.add_argument("--task-file", dest="task_file", default=None)
    args = parser.parse_args(argv)
    read_prompt()

    session_id = f"stub-helper-{os.getpid()}"
    scratch = Path(os.environ.get("STUB_SCRATCH_DIR", "."))
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / f"stub-helper-{os.getpid()}.spawn").write_text("spawned", encoding="utf-8")

    # The "tool call": run the fixed helper in the foreground.
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-u",
            args.helper,
            "--nonce-file",
            "helper_ready.json",
            "--heartbeat-file",
            "helper_heartbeat.txt",
            "--lifetime",
            str(args.lifetime),
        ],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-helper-1",
                    "title": "run stoppable helper",
                    "kind": "other",
                    "status": "in_progress",
                    "rawInput": {"helper_pid": child.pid},
                },
            },
        }
    )
    # Stay in the foreground until the helper exits (or we are torn down with it).
    assert child.stdout is not None
    for line in child.stdout:
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "call-helper-1",
                        "status": "in_progress",
                        "content": [{"type": "content", "content": {"type": "text", "text": line.strip()}}],
                    },
                },
            }
        )
    code = child.wait()
    emit({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
    return code


if __name__ == "__main__":
    raise SystemExit(main())
