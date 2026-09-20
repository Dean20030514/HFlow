"""Non-prompt ACP client check: start a server, initialize, open a session, close.

This is the A-layer probe for the real DSH path. It deliberately sends **no prompt**, so
it makes zero paid inference calls; it establishes only that the server starts, speaks
ACP on stdio (NDJSON), answers ``initialize``, and can create a session.

Two facts learned the hard way and encoded here:

* The server needs a **live stdin pipe**. Given a closed or exhausted stdin it finishes
  the pending requests and exits 0, which looks like "the profile does nothing".
* A single thread cannot write a request and wait for its reply while also draining
  stdout, so the reader runs on its own thread.

Results are printed as one JSON object on stdout; raw wire traffic goes to ``--wire-log``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
CLIENT_NAME = "hflow-m0-probe"
CLIENT_VERSION = "0.1.0"


def normalize_launch_argv(argv: list[str]) -> list[str]:
    """Make a Windows batch shim launchable, whatever form the caller passed.

    ``dsh`` resolves to ``dsh.CMD`` here, and ``CreateProcess`` cannot launch a batch file
    directly - Python reports ``FileNotFoundError`` even though the path exists, whether the
    name is an absolute path or a bare PATH lookup. That is an OS process-creation fact, not
    a DSH defect, and must not be reported as one. Resolving through ``which`` first also
    removes the ambiguity of relying on PATH resolution inside a redirected child.
    """
    if not argv:
        return argv
    executable = argv[0]
    if not any(sep in executable for sep in ("/", "\\")):
        resolved = shutil.which(executable)
        if resolved:
            executable = resolved
            argv = [resolved, *argv[1:]]
    if executable.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", *argv]
    return argv


class AcpClient:
    """Minimal ACP client: NDJSON over a child's stdio, one reader thread."""

    def __init__(self, child: subprocess.Popen, wire_log: Path) -> None:
        self.child = child
        self.wire_log = wire_log
        self._lock = threading.Lock()
        self.responses: dict[int, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.unparsed: list[str] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _log(self, record: dict[str, Any]) -> None:
        with self._lock:
            with self.wire_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

    def _read_loop(self) -> None:
        assert self.child.stdout is not None
        for raw in self.child.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.unparsed.append(line[:200])
                self._log({"dir": "in", "unparsed": line[:200]})
                continue
            self._log({"dir": "in", "payload": message})
            if "id" in message and message["id"] is not None:
                self.responses[int(message["id"])] = message
            else:
                self.notifications.append(message)

    def request(self, request_id: int, method: str, params: dict[str, Any], timeout: int) -> dict[str, Any] | None:
        assert self.child.stdin is not None
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._log({"dir": "out", "method": method, "payload": payload})
        self.child.stdin.write((json.dumps(payload) + "\n").encode())
        self.child.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if request_id in self.responses:
                return self.responses.pop(request_id)
            if self.child.poll() is not None:
                return None
            time.sleep(0.1)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", choices=["dsh", "mock"], default="dsh")
    parser.add_argument("--home", default=None, help="DSH_HOME for the server process")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--wire-log", default="acp_wire.jsonl")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--prompt", default=None, help="optional prompt text (live use only)")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    wire_log = Path(args.wire_log)
    if not wire_log.is_absolute():
        wire_log = workdir / wire_log
    wire_log.write_text("", encoding="utf-8")

    if args.server == "dsh":
        server_argv = ["dsh", "--profile", "acp"]
    else:
        mock = Path(__file__).resolve().parent / "mock_acp_agent.py"
        server_argv = [
            shutil.which("python") or sys.executable,
            "-u",
            str(mock),
            "--scenario",
            "normal",
            "--wire-log",
            str(workdir / "mock_wire.jsonl"),
        ]
    server_argv = normalize_launch_argv(server_argv)

    env = dict(os.environ)
    if args.home:
        env["DSH_HOME"] = str(args.home)

    result: dict[str, Any] = {
        "server": args.server,
        "server_argv": server_argv,
        "dsh_home": args.home,
        "workdir": str(workdir),
        "responses": [],
        "prompt_sent": bool(args.prompt),
    }

    child: subprocess.Popen | None = None
    try:
        child = subprocess.Popen(  # noqa: S603 - argv is constructed here
            server_argv,
            cwd=str(workdir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result["server_pid"] = child.pid
        client = AcpClient(child, wire_log)

        init = client.request(
            0,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            args.timeout,
        )
        result["responses"].append({"method": "initialize", "payload": init})
        if init is None:
            result["outcome"] = "no initialize response"
        else:
            session = client.request(
                1,
                "session/new",
                {"cwd": str(Path.cwd()), "mcpServers": []},
                args.timeout,
            )
            result["responses"].append({"method": "session/new", "payload": session})
            session_id = None
            if session and isinstance(session.get("result"), dict):
                session_id = session["result"].get("sessionId")
            result["session_id"] = session_id

            if session_id and args.prompt:
                prompt = client.request(
                    2,
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": args.prompt}],
                    },
                    args.timeout,
                )
                result["responses"].append({"method": "session/prompt", "payload": prompt})

            if session_id:
                closed = client.request(3, "session/close", {"sessionId": session_id}, 30)
                result["responses"].append({"method": "session/close", "payload": closed})
            result["outcome"] = "completed"
    except OSError as exc:
        result["spawn_error"] = repr(exc)
        result["outcome"] = "spawn failed"
    finally:
        if child is not None:
            _stop(child, result)
        if child is not None and child.stderr is not None:
            try:
                result["stderr_tail"] = "\n".join(
                    child.stderr.read().decode("utf-8", errors="replace").splitlines()[-25:]
                )
            except (OSError, ValueError):
                result["stderr_tail"] = ""

    result["notifications"] = client.notifications[-20:] if child is not None and "client" in dir() else []
    result["unparsed_lines"] = client.unparsed[:10] if "client" in dir() else []
    result["wire_log"] = str(wire_log)
    print(json.dumps(result, default=str))
    return 0


def _stop(child: subprocess.Popen, result: dict[str, Any]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
            result["terminated"] = True
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            result["terminated"] = "forced"
    result["server_returncode"] = child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
