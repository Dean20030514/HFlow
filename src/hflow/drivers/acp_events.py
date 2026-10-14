"""Projection of an ACP/JSON-RPC line stream into HFlow's neutral event vocabulary.

The driver observes whatever the chosen CLI prints (acpx `--format json` emits NDJSON ACP
messages) and maps it here. Three rules:

* an unparseable line is *evidence of a protocol problem*, never silently dropped - it is
  counted, kept in the raw log, and can fail the invocation;
* only semantics the controller can act on are invented. Unknown methods become
  ``progress`` with the method name, not a fabricated specific event;
* an event describes one line, never the invocation: a JSON-RPC error projects as ``failed``
  whatever request it answers, and the driver's ``collect`` - which binds responses to the
  observed ``session/prompt`` id - decides the outcome (there, a prompt answered with an error
  is ``outcome_unknown``).
"""

from __future__ import annotations

import json

from ..contracts import EventKind, NormalizedEvent

#: Client requests with an event of their own: the kind and message each projects to.
#: ``session/prompt`` is the moment the task actually reaches the Harness - the dispatch marker
#: the controller uses to know model work provably began.
_REQUEST_EVENT: dict[str, tuple[EventKind, str]] = {
    "initialize": (EventKind.STARTED, "initialize sent"),
    "session/new": (EventKind.STARTED, "session/new sent"),
    "session/prompt": (EventKind.DISPATCHED, "session/prompt sent to the harness"),
    "session/cancel": (EventKind.PROGRESS, "session/cancel sent"),
}

_STOP_REASON_EVENT = {
    "end_turn": EventKind.COMPLETED,
    "cancelled": EventKind.CANCELLED,
    "max_tokens": EventKind.FAILED,
    "max_turn_requests": EventKind.FAILED,
    "refusal": EventKind.FAILED,
}
#: ACP v1's closed StopReason set (schema-v1.24.1). Shared with the driver so the event a stop reason
#: projects to and the outcome ``collect`` gives it agree on which values are known: anything else
#: is ``outcome_unknown`` in both.
V1_STOP_REASONS = frozenset(_STOP_REASON_EVENT)


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
    except (ValueError, RecursionError):
        # JSONDecodeError is a ValueError; so is an integer literal past Python's digit limit,
        # and nesting past the decoder's depth raises RecursionError. Each is a line this build
        # cannot read - unparseable, never an exception out of the driver's reader thread.
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
        # A malformed error (not an object) is still an error line, and must not raise out of the
        # driver's reader thread.
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
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
    known = _REQUEST_EVENT.get(method)
    if known is not None:
        kind, text = known
        return NormalizedEvent(kind=kind, sequence=sequence, at=at, message=text, method=method)
    if method == "session/update":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
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
        # Every other update kind, named or not, is progress: only semantics the controller can
        # act on get an event of their own.
        return NormalizedEvent(
            kind=EventKind.PROGRESS,
            sequence=sequence,
            at=at,
            message=f"session update: {kind}",
            method=kind,
        )
    return NormalizedEvent(
        kind=EventKind.PROGRESS, sequence=sequence, at=at, message=f"method {method}", method=method
    )
