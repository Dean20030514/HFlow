"""Test-only stand-in for the acpx CLI, so the production driver is autonomous in tests.

It mimics exactly the contract the driver relies on:

* reads the agent launch argv from the acpx config in the OS home (the structured-argv
  boundary the real client requires on Windows);
* accepts the driver's real arguments (``--cwd``, ``--format json``, ``--timeout``,
  ``exec``, ``-f -``) and reads the task body from **stdin until EOF**;
* projects the agent's ACP messages to NDJSON on stdout, exactly like ``--format json``.

Being autonomous matters: the test then exercises the same start/observe/cancel code path a
live run would use, instead of a fake that shares the driver's in-process state.

Scenario behaviour is selected by the first element of the configured agent argv, so the
"stubborn agent" case is a real separate process tree that ignores cancellation.

Usage (normally spawned by the driver):
    python fake_acpx_client.py --cwd <dir> --format json --timeout 60 exec -f -
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SPAWN_LOG = "agent-spawns.jsonl"


def load_agent_argv() -> list[str]:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    config_path = Path(home) / ".acpx" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    default = config.get("defaultAgent")
    agents = config.get("agents") or {}
    entry = agents.get(default) or {}
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv:
        raise SystemExit(f"fake client: no usable argv in {config_path}")
    return [str(item) for item in argv]


def out(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--format", default="json")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--delay-before-prompt", type=float, default=0.0)
    # Accepted and ignored: the driver passes `-f -`, and the body is read from stdin.
    parser.add_argument("-f", "--file", dest="task_file", default=None)
    parser.add_argument("command", nargs="?")
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args(argv)

    task = sys.stdin.read()
    agent_argv = load_agent_argv()
    scenario = agent_argv[0]

    workdir = Path(args.cwd)
    workdir.mkdir(parents=True, exist_ok=True)
    with (workdir / SPAWN_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"scenario": scenario, "task": task[:200]}) + "\n")

    out(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": "fake-acpx", "version": "0.1.0"},
            },
        }
    )
    out(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "fake-agent", "version": "0.1.0"},
                "agentCapabilities": {"loadSession": False, "promptCapabilities": {}},
            },
        }
    )

    if args.delay_before_prompt:
        time.sleep(args.delay_before_prompt)

    # The configured argv is already a complete command line (executable first), exactly as
    # the real client treats `agents.<name>.argv`.
    process = subprocess.Popen(  # noqa: S603 - argv comes from the configured agent entry
        [*agent_argv, "--task-file", "-"],
        cwd=str(workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None and process.stdout is not None

    out(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": str(workdir), "mcpServers": []},
        }
    )
    out(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"sessionId": f"sess-{os.getpid()}", "configOptions": []},
        }
    )

    pump_done = threading.Event()

    def pump_out() -> None:
        for line in process.stdout:  # type: ignore[union-attr]
            sys.stdout.write(line)
            sys.stdout.flush()
        pump_done.set()

    reader = threading.Thread(target=pump_out, daemon=True)
    reader.start()

    prompt = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/prompt",
        "params": {
            "sessionId": f"sess-{os.getpid()}",
            "prompt": [{"type": "text", "text": task}],
        },
    }
    out(prompt)
    process.stdin.write(json.dumps(prompt) + "\n")
    process.stdin.flush()
    process.stdin.close()

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if pump_done.wait(0.2):
            break
    else:
        # Mirrors the real client: the turn timed out, and the agent is torn down.
        process.kill()
        out(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32070,
                    "message": f"Timed out after {args.timeout * 1000}ms",
                    "data": {"acpxCode": "TIMEOUT"},
                },
            }
        )
        return 3
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
