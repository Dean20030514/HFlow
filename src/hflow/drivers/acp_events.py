"""Projection of an ACP/JSON-RPC line stream into HFlow's neutral event vocabulary.

The driver observes whatever the chosen CLI prints (acpx `--format json` emits NDJSON ACP
messages) and maps it here. Two rules:

* an unparseable line is *evidence of a protocol problem*, never silently dropped - it is
  counted, kept in the raw log, and can fail the invocation;
* only semantics the controller can act on are invented. Unknown methods become
  ``progress`` with the method name, not a fabricated specific event.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from ..contracts import EventKind, NormalizedEvent

#: Update kinds this build understands well enough to name.
_UPDATE_EVENT = {
    "agent_message_chunk": EventKind.PROGRESS,
    "agent_thought_chunk": EventKind.PROGRESS,
    "tool_call": EventKind.PROGRESS,
    "tool_call_update": EventKind.PROGRESS,
    "plan": EventKind.PROGRESS,
    "available_commands_update": EventKind.PROGRESS,
    "current_mode_update": EventKind.PROGRESS,
    "config_option_update": EventKind.PROGRESS,
    "usage_update": EventKind.USAGE,
}

_STOP_REASON_EVENT = {
    "end_turn": EventKind.COMPLETED,
    "cancelled": EventKind.CANCELLED,
    "max_tokens": EventKind.FAILED,
    "max_turn_requests": EventKind.FAILED,
    "refusal": EventKind.FAILED,
}


class ObservedLine:
    """One parsed line plus the projection it produced."""

    __slots__ = ("event", "message", "parsed")

    def __init__(self, parsed: bool, message: dict | None, event: NormalizedEvent | None) -> None:
        self.parsed = parsed
        self.message = message
        self.event = event


def project_line(line: str, sequence: int, at: str) -> ObservedLine:
    """Map one NDJSON line to a neutral event (or to "unparseable")."""
    stripped = line.strip()
    if not stripped:
        return ObservedLine(True, None, None)
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError:
        return ObservedLine(False, None, None)
    if not isinstance(message, dict):
        return ObservedLine(False, None, None)

    method = message.get("method")
    if isinstance(method, str):
        return ObservedLine(True, message, _event_for_method(method, message, sequence, at))

    result = message.get("result")
    if isinstance(result, dict) and "stopReason" in result:
        reason = str(result["stopReason"])
        return ObservedLine(
            True,
            message,
            NormalizedEvent(
                kind=_STOP_REASON_EVENT.get(reason, EventKind.OUTCOME_UNKNOWN),
                sequence=sequence,
                at=at,
                message=f"prompt settled with stopReason={reason}",
                method="session/prompt",
            ),
        )

    if "error" in message:
        error = message.get("error") or {}
        return ObservedLine(
            True,
            message,
            NormalizedEvent(
                kind=EventKind.FAILED,
                sequence=sequence,
                at=at,
                message=f"error {error.get('code')}: {error.get('message')}",
                method=str(error.get("data", {}).get("acpxCode", "error"))
                if isinstance(error.get("data"), dict)
                else "error",
            ),
        )
    return ObservedLine(True, message, None)


def _event_for_method(method: str, message: dict, sequence: int, at: str) -> NormalizedEvent:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return NormalizedEvent(
            kind=EventKind.STARTED, sequence=sequence, at=at, message="initialize sent", method=method
        )
    if method == "session/new":
        return NormalizedEvent(
            kind=EventKind.STARTED, sequence=sequence, at=at, message="session/new sent", method=method
        )
    if method == "session/prompt":
        # The moment the task actually reaches the Harness. This is the dispatch marker the
        # controller uses to know model work provably began.
        return NormalizedEvent(
            kind=EventKind.DISPATCHED,
            sequence=sequence,
            at=at,
            message="session/prompt sent to the harness",
            method=method,
        )
    if method == "session/cancel":
        return NormalizedEvent(
            kind=EventKind.PROGRESS,
            sequence=sequence,
            at=at,
            message="session/cancel sent",
            method=method,
        )
    if method == "session/update":
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = str(update.get("sessionUpdate", "unknown"))
        if kind == "usage_update":
            return NormalizedEvent(
                kind=EventKind.USAGE,
                sequence=sequence,
                at=at,
                message=(
                    f"context usage {update.get('used')}/{update.get('size')} "
                    "(context window, not billed tokens)"
                ),
                method=kind,
            )
        return NormalizedEvent(
            kind=_UPDATE_EVENT.get(kind, EventKind.PROGRESS),
            sequence=sequence,
            at=at,
            message=f"session update: {kind}",
            method=kind,
        )
    return NormalizedEvent(
        kind=EventKind.PROGRESS, sequence=sequence, at=at, message=f"method {method}", method=method
    )


def iter_lines(text: str) -> Iterator[str]:
    yield from text.splitlines()


def summarize(events: Iterable[NormalizedEvent]) -> dict[str, object]:
    """Small deterministic summary used by receipts and tests."""
    kinds: dict[str, int] = {}
    stop_reason: str | None = None
    dispatched = False
    for event in events:
        kinds[event.kind.value] = kinds.get(event.kind.value, 0) + 1
        if event.kind is EventKind.DISPATCHED:
            dispatched = True
        if event.method == "session/prompt" and "stopReason=" in event.message:
            stop_reason = event.message.split("stopReason=", 1)[1]
    return {"kinds": kinds, "stop_reason": stop_reason, "dispatched": dispatched}
