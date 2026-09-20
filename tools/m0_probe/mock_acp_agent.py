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
    try:
        for line in sys.stdin:
            stripped = line.strip()
            if not stripped:
                continue
            if args.scenario == "garbage-line" and agent.prompts_seen == 0 and "not-json" not in stripped:
                wire.record("note", {"injecting_garbage": True})
                sys.stdout.write("this-is-not-json\n")
                sys.stdout.flush()
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError as exc:
                wire.record("note", {"unparseable_line": stripped[:200], "error": str(exc)})
                wire.send(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                )
                continue
            wire.record("in", message)
            agent.handle(message)
            if args.scenario == "exit-after-init" and message.get("method") == "initialize":
                wire.record("note", {"exiting_after_init": True})
                break
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        wire.record("note", {"interrupted": True})
    finally:
        wire.record("note", {"prompts_seen": agent.prompts_seen, "exiting": True})
        wire.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
