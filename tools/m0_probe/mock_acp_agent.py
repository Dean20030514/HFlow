"""Minimal ACP agent over NDJSON stdio, for probing an ACP client (acpx).

This is deliberately *not* a general ACP server. It implements only the methods the
probe needs, from the schema shipped inside the pinned acpx dependency:

* ``initialize``         -> protocol version + minimal agent capabilities
* ``session/new``        -> a session id
* ``session/prompt``     -> one thought chunk, one message chunk, then ``stopReason``
* ``session/cancel``     -> notification; marks the in-flight turn cancelled

Everything else returns JSON-RPC ``-32601`` (method not found) instead of guessing, so
a client that depends on unimplemented surface fails visibly.

The mock is scenario-driven via ``--scenario`` so a failure mode is a named input, not a
code edit. Its stdout carries protocol traffic only; all diagnostics go to the wire log
and stderr.

Usage:
    python mock_acp_agent.py --scenario normal --wire-log <path> [--ready-file <path>]
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
AGENT_NAME = "hflow-mock-acp"
AGENT_VERSION = "0.1.0"

SCENARIOS = (
    "normal",  # initialize, session/new, prompt with two chunks, end_turn
    "echo-nonce",  # like normal, but the prompt text is echoed back inside a marker
    "unknown-method",  # replies -32601 to the first session/new
    "garbage-line",  # writes one non-JSON line, then keeps working
    "slow-prompt",  # prompt sleeps past the client timeout before answering
    "cancel-prompt",  # prompt waits for session/cancel, then answers stopReason=cancelled
    "bad-init",  # initialize returns an unrelated shape
    "exit-after-init",  # exits immediately after initialize succeeds
)


class Wire:
    """Serialized NDJSON writer plus an evidence log."""

    def __init__(self, log_path: Path) -> None:
        self._lock = threading.Lock()
        self._log = log_path.open("a", encoding="utf-8")

    def record(self, direction: str, payload: Any) -> None:
        with self._lock:
            self._log.write(json.dumps({"dir": direction, "payload": payload}) + "\n")
            self._log.flush()

    def send(self, message: dict[str, Any]) -> None:
        self.record("out", message)
        with self._lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def result(self, request_id: Any, payload: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": payload})

    def error(self, request_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        self._log.close()


class MockAgent:
    def __init__(self, scenario: str, wire: Wire, session_id: str) -> None:
        self.scenario = scenario
        self.wire = wire
        self.session_id = session_id
        self.cancelled = threading.Event()
        self.prompts_seen = 0
        self._permission_seq = 0
        self._answers: dict[str, Any] = {}

    # -- request dispatch ---------------------------------------------------

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            self.on_initialize(request_id)
            return
        if method == "session/new":
            if self.scenario == "unknown-method":
                self.wire.error(request_id, -32601, "Method not found: session/new (probe scenario)")
                return
            self.wire.result(
                request_id,
                {
                    "sessionId": self.session_id,
                    "modes": None,
                    "configOptions": None,
                },
            )
            return
        if method == "session/prompt":
            self.on_prompt(request_id, params)
            return
        if method == "session/cancel":
            # Notification, not a request.
            self.cancelled.set()
            self.wire.record("note", {"cancelled": params.get("sessionId")})
            return
        if method in {"session/load", "session/resume", "session/list", "session/close"}:
            self.wire.error(request_id, -32601, f"Method not found: {method} (probe mock)")
            return
        if request_id is not None:
            self.wire.error(request_id, -32601, f"Method not found: {method} (probe mock)")
        else:
            self.wire.record("note", {"ignored_notification": method})

    def on_initialize(self, request_id: Any) -> None:
        if self.scenario == "bad-init":
            self.wire.result(request_id, {"unexpected": True})
            return
        self.wire.result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": False,
                    },
                    "mcpCapabilities": {"http": False, "sse": False, "acp": False},
                    "sessionCapabilities": {},
                    "auth": {},
                },
                "agentInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
            },
        )

    def on_prompt(self, request_id: Any, params: dict[str, Any]) -> None:
        self.prompts_seen += 1
        prompt_text = _prompt_text(params)
        session_id = params.get("sessionId") or self.session_id

        if self.scenario == "slow-prompt":
            time.sleep(30)
        if self.scenario == "cancel-prompt":
            # Wait to be cancelled; the client's cancel must arrive as a notification.
            self.cancelled.wait(timeout=25)

        if self.scenario == "cancel-prompt" and self.cancelled.is_set():
            self.wire.result(request_id, {"stopReason": "cancelled"})
            return

        # Ask the client for permission before answering. This is the one deterministic probe of
        # acpx's *client-side* policy mapping: the mock never answers this request itself, so
        # whatever comes back was decided by the client from its configuration. It says nothing
        # about DSH's native tool enforcement, only about the client's permission mediation.
        decision = self.ask_permission(session_id)

        self.wire.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "mock thought"},
                },
            },
        )
        answer = (
            f"MOCK_NONCE={_nonce_from(prompt_text)}"
            if self.scenario == "echo-nonce"
            else f"mock answer to: {prompt_text[:120]}"
        )
        self.wire.notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": answer},
                },
            },
        )
        self.wire.result(request_id, {"stopReason": "end_turn"})

    def ask_permission(self, session_id: str) -> dict[str, Any]:
        """Send ``session/request_permission`` and wait for the client's own answer.

        The answer arrives while this call is blocked, so the stdin reader must not be the only
        thing that can consume it: responses are routed into ``self._answers`` by the reader
        loop (see :meth:`_read_loop`) and collected here.
        """
        request_id = f"perm-{self._permission_seq}"
        self._permission_seq += 1
        self.wire.record("note", {"asking_permission": request_id})
        self.wire.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": "call-perm-probe",
                        "title": "write a file",
                        "kind": "edit",
                        "status": "pending",
                        "rawInput": {"path": "probe.txt"},
                    },
                    "options": [
                        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            }
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            answer = self._answers.pop(request_id, None)
            if answer is not None:
                # Recorded here, after the answer arrives: writing it before the wait would log
                # a placeholder and hide the real decision.
                self.wire.record("note", {"permission_decision": answer})
                return answer
            time.sleep(0.05)
        self.wire.record("note", {"permission_decision": {"outcome": "no_answer"}})
        return {"outcome": "no_answer"}

    def _read_loop(self, stream: Any, wire: Wire, scenario: str) -> None:
        """Consume stdin on its own thread so a blocked request can still be answered."""
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                wire.record("note", {"unparseable_line": stripped[:200]})
                continue
            wire.record("in", message)
            if message.get("method") is None and message.get("id") is not None:
                # A response to something we asked (our permission request): the blocked turn is
                # waiting for it, so it never goes through the request dispatch path.
                self._answers[str(message["id"])] = message.get("result") or {
                    "error": message.get("error")
                }
                wire.record("note", {"routed_answer": str(message["id"])})
                continue
            if message.get("method") == "session/prompt":
                # Turns block on permission answers, so they run off the reader thread.
                self.handle_async(message)
                continue
            self.handle(message)
            if scenario == "exit-after-init" and message.get("method") == "initialize":
                wire.record("note", {"exiting_after_init": True})
                return

    def handle_async(self, message: dict[str, Any]) -> None:
        """Run a turn on its own thread.

        A prompt turn blocks while it waits for the client's answer to a permission request, and
        that answer arrives on stdin - so the reader thread must stay free to route it. Handling
        the turn inline would deadlock the very exchange being measured.
        """
        worker = threading.Thread(target=self.handle, args=(message,), daemon=True)
        worker.start()


def _prompt_text(params: dict[str, Any]) -> str:
    blocks = params.get("prompt") or []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _nonce_from(text: str) -> str:
    marker = "NONCE="
    if marker not in text:
        return "missing"
    tail = text.split(marker, 1)[1]
    return "".join(ch for ch in tail if ch.isalnum() or ch in "-_")[:64]


def _install_signal_flush(wire: Wire) -> None:
    """On an external stop signal, record and flush before dying.

    Without this, a terminated mock loses buffered log lines and the probe cannot tell
    "the client never forwarded a cancel" apart from "the log was lost".
    """

    def handler(signum: int, _frame: object) -> None:
        wire.record("note", {"signal": signum, "exiting": "by_signal"})
        wire.close()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        candidate = getattr(signal, name, None)
        if candidate is not None:
            try:
                signal.signal(candidate, handler)
            except (ValueError, OSError):  # pragma: no cover - platform dependent
                continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--wire-log", required=True)
    parser.add_argument("--ready-file", default=None)
    parser.add_argument("--session-id", default="mock-session-1")
    args = parser.parse_args(argv)

    wire = Wire(Path(args.wire_log))
    _install_signal_flush(wire)
    wire.record(
        "meta",
        {
            "mock": AGENT_NAME,
            "version": AGENT_VERSION,
            "scenario": args.scenario,
            "pid": __import__("os").getpid(),
            "cwd": str(Path.cwd()),
        },
    )
    if args.ready_file:
        Path(args.ready_file).write_text("ready\n", encoding="utf-8")

    agent = MockAgent(args.scenario, wire, args.session_id)
    # stdin is consumed on its own thread: a prompt turn blocks while it waits for the client's
    # answer to a permission request, and that answer arrives on stdin.
    reader = threading.Thread(
        target=agent._read_loop, args=(sys.stdin, wire, args.scenario), daemon=True
    )
    reader.start()
    try:
        while reader.is_alive():
            time.sleep(0.1)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        wire.record("note", {"interrupted": True})
    finally:
        wire.record("note", {"prompts_seen": agent.prompts_seen, "exiting": True})
        wire.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
