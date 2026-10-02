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

Model selection mirrors the pinned client's ``--model``: ``STUB_MODEL_CATALOG=grouped`` makes
``session/new`` advertise a DSH-shaped grouped catalog (placeholder ids). A requested model that
is advertised and not already current is applied with an outbound ``session/set_config_option``
before the prompt; one that is not advertised, or a request with no catalog at all, ends the run
with the client's JSON-RPC error line and exit 1, without sending the prompt.
``STUB_CLIENT_FAIL_BEFORE_PROMPT=1`` ends any run that way after the session started, for the
case that is *not* a model refusal.

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
#: The grouped catalog's values: an initial one and one other, both advertised.
CATALOG_VALUES = ('["stub-provider","stub-flash"]', '["stub-provider","stub-pro"]')
#: Request id of the model change; the prompt keeps id 2, which the stub agents answer.
SET_CONFIG_ID = 10


def catalog(model: str) -> list[dict]:
    """``configOptions`` in the shape a recorded DSH session/new returned."""
    return [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model,
            "options": [
                {
                    "group": "stub-provider",
                    "name": "Stub provider",
                    "options": [{"value": value, "name": value} for value in CATALOG_VALUES],
                }
            ],
        },
        {
            "id": "reasoning_effort",
            "name": "Reasoning effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": "high",
            "options": [{"value": value, "name": value} for value in ("off", "low", "high", "max")],
        },
    ]


def client_error(message: str) -> None:
    """The client's own error line, as the pinned acpx prints it before exiting 1."""
    out(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32603, "message": message, "data": {"acpxCode": "RUNTIME"}},
        }
    )


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


def spawn_log_path(workdir: Path) -> Path:
    """Where the client records which agent it launched.

    The default is the session cwd, which for a real run is the tree under test. A test that
    drives the *controller* points ``STUB_SPAWN_LOG`` at its own scratch directory instead,
    so harness bookkeeping cannot be mistaken for a worker changing files outside its scope.
    """
    override = os.environ.get("STUB_SPAWN_LOG")
    path = Path(override) if override else workdir / SPAWN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--format", default="json")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--delay-before-prompt", type=float, default=0.0)
    parser.add_argument("--model", default=None)
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
    with spawn_log_path(workdir).open("a", encoding="utf-8") as handle:
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
    advertised = os.environ.get("STUB_MODEL_CATALOG") == "grouped"
    out(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "sessionId": f"sess-{os.getpid()}",
                "configOptions": catalog(CATALOG_VALUES[0]) if advertised else [],
            },
        }
    )

    refusal = ""
    session_id = f"sess-{os.getpid()}"
    if args.model and not advertised:
        refusal = f"Cannot apply --model {args.model!r}: no model support was advertised"
    elif args.model and args.model not in CATALOG_VALUES:
        refusal = f"Cannot apply --model {args.model!r}: that model was not advertised"
    if args.model and not refusal and args.model != CATALOG_VALUES[0]:
        out(
            {
                "jsonrpc": "2.0",
                "id": SET_CONFIG_ID,
                "method": "session/set_config_option",
                "params": {"sessionId": session_id, "configId": "model", "value": args.model},
            }
        )
        out(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": catalog(args.model),
                    },
                },
            }
        )
        out(
            {"jsonrpc": "2.0", "id": SET_CONFIG_ID, "result": {"configOptions": catalog(args.model)}}
        )
    if not refusal and os.environ.get("STUB_CLIENT_FAIL_BEFORE_PROMPT") == "1":
        refusal = "stub client failed before the prompt"
    if refusal:
        process.kill()
        process.wait()
        client_error(refusal)
        return 1

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
