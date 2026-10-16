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

It also holds the stream readers the driver keeps per invocation that are about messages, not
processes: JSON-RPC id keys, a message's session, and ``_ModelWatch`` - what the stream showed of
the session's model option.
"""

from __future__ import annotations

import json
from typing import Any

from ..contracts import EventKind, ModelApplied, ModelChange, ModelObservation, NormalizedEvent

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


def _session_of(params: dict[str, Any]) -> str:
    """The ``sessionId`` a message's params name, or ``""`` when they name none."""
    session = params.get("sessionId")
    return session if isinstance(session, str) else ""


def _rpc_id(value: Any) -> tuple[str, Any] | None:
    """A JSON-RPC id as a key that keeps its type: ``2``, ``2.0`` and ``True`` stay distinct."""
    if value is None or isinstance(value, (dict, list)):
        return None
    return type(value).__name__, value


def _option_of(config_options: Any, category: str, preferred_id: str) -> dict[str, Any] | None:
    """The ``select`` option of one category, the way acpx picks it.

    Category match first, and among those the one whose id is ``preferred_id``; an option that
    only has that id (no category) is the last resort.
    """
    if not isinstance(config_options, list):
        return None
    chosen: dict[str, Any] | None = None
    best = -1
    for option in config_options:
        if not isinstance(option, dict) or option.get("type") != "select":
            continue
        if option.get("category") == category:
            rank = 2 if option.get("id") == preferred_id else 1
        elif option.get("id") == preferred_id:
            rank = 0
        else:
            continue
        if rank > best:
            chosen, best = option, rank
    return chosen


def _option_values(options: Any) -> set[str]:
    """The values a ``select`` option offers, from a flat list or from provider groups."""
    values: set[str] = set()
    for entry in options if isinstance(options, list) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("value"), str):
            values.add(entry["value"])
        values |= _option_values(entry.get("options"))
    return values


class _ModelWatch:
    """What one invocation's stream shows about its model option. Observation only.

    Reads the ``session/new`` response's ``configOptions``, every ``config_option_update``, and
    each outbound ``session/set_config_option`` for the model with its response. It never
    decides an outcome; ``collect`` asks it narrow questions.
    """

    def __init__(self, requested: str | None) -> None:
        self.requested = requested
        self.advertised = False
        self.config_id = ""
        #: Every value the advertised model option offers (flat or grouped), verbatim.
        self.values: set[str] = set()
        self.initial_value: str | None = None
        self.effective_value: str | None = None
        self.source = ""
        self.thought_level: str | None = None
        self.changes: list[ModelChange] = []
        self.session_created = False
        self.session_id = ""
        self._prompt_seen = False
        self._ambiguous = False
        #: Request ids are only unique within one direction; the raw stream has both.
        self._seen_request_ids: set[tuple[str, Any]] = set()
        self._tracked_request_ids: set[tuple[str, Any]] = set()
        #: Outbound requests awaiting their response: id key -> (method, session).
        self._pending: dict[tuple[str, Any], tuple[str, str]] = {}
        self.skipped_updates = 0
        self.skipped_sets = 0
        #: Outcomes of the model's set_config_option requests, in order: True = succeeded.
        self.set_results: list[bool] = []
        self.set_requests = 0
        #: The client's own error line: a JSON-RPC error with a null id, as acpx prints it.
        self.client_error = ""

    def observe(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        key = _rpc_id(message.get("id"))
        if isinstance(method, str):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            session = _session_of(params)
            model_set = (
                method == "session/set_config_option"
                and params.get("configId") == (self.config_id or "model")
            )
            tracked = method == "session/new" or (
                model_set and bool(self.session_id) and session == self.session_id
            )
            if key is not None:
                if key in self._seen_request_ids and (
                    tracked or key in self._tracked_request_ids
                ):
                    self._ambiguous = True
                self._seen_request_ids.add(key)
                if tracked:
                    self._tracked_request_ids.add(key)
            if key is not None and method == "session/new" and not self.session_created:
                self._pending[key] = (method, "")
            elif model_set:
                if key is not None and self.session_id and session == self.session_id:
                    self._pending[key] = (method, session)
                    self.set_requests += 1
                else:
                    self.skipped_sets += 1
            elif method == "session/update":
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                if update.get("sessionUpdate") == "config_option_update":
                    if self.session_id and session == self.session_id:
                        self._read_options(update.get("configOptions"), "config_option_update")
                    else:
                        self.skipped_updates += 1
            return
        answered = self._pending.pop(key, None) if key is not None else None
        result = message.get("result")
        if answered is not None and self._ambiguous:
            return
        method, session = answered if answered is not None else (None, "")
        if (
            method == "session/new"
            and not self.session_created
            and isinstance(result, dict)
            and "error" not in message
            and isinstance(result.get("sessionId"), str)
            and result["sessionId"]
        ):
            self.session_created = True
            self.session_id = result["sessionId"]
            option = _option_of(result.get("configOptions"), "model", "model")
            if option is not None:
                self.advertised = True
                self.config_id = str(option.get("id") or "")
                self.values = _option_values(option.get("options"))
                current = option.get("currentValue")
                self.initial_value = current if isinstance(current, str) else None
                self.effective_value = self.initial_value
                self.source = "session/new"
            self._read_thought_level(result.get("configOptions"))
        elif method == "session/set_config_option" and session == self.session_id:
            # Any JSON-RPC success counts as the agent accepting the request, whatever shape its
            # result has; the effective value is only taken from a result that reports one.
            ok = "result" in message and "error" not in message
            self.set_results.append(ok)
            if ok and isinstance(result, dict):
                self._read_options(result.get("configOptions"), "session/set_config_option")
        elif (
            answered is None
            and "id" in message
            and message.get("id") is None
            and "error" in message
            and not self.client_error
        ):
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            self.client_error = str(error.get("message") or error)[:500]

    def bind_prompt(self, session: str) -> None:
        """Check the first prompt against the session whose configuration was observed."""
        if self._prompt_seen:
            return
        self._prompt_seen = True
        if not session or not self.session_id or session != self.session_id:
            self._ambiguous = True

    def _read_options(self, config_options: Any, source: str) -> None:
        option = _option_of(config_options, "model", self.config_id or "model")
        if option is not None and isinstance(option.get("currentValue"), str):
            value = option["currentValue"]
            if value != self.effective_value:
                self.changes.append(ModelChange(source=source, value=value))
            self.effective_value = value
            self.source = source
        self._read_thought_level(config_options)

    def _read_thought_level(self, config_options: Any) -> None:
        option = _option_of(config_options, "thought_level", "reasoning_effort")
        if option is not None and isinstance(option.get("currentValue"), str):
            self.thought_level = option["currentValue"]

    def refused(self) -> bool:
        """The requested model was refused, as the stream shows it.

        Either the client could not offer it - no model option was advertised, or the value is
        not among the advertised ones - so no change request went out; or the agent answered the
        change request with an error. A value that was already current needs no request, so its
        absence is not a refusal, and a request still awaiting its answer is unknown, not refused.
        """
        if self._ambiguous or not self.session_created or self._pending:
            return False
        if self.set_requests == 0:
            return self.requested is not None and (
                not self.advertised or self.requested not in self.values
            )
        return bool(self.set_results) and not any(self.set_results)

    def observation(self) -> ModelObservation:
        return ModelObservation(
            advertised=self.advertised,
            config_id=self.config_id,
            initial_value=self.initial_value,
            requested=self.requested,
            effective_value=self.effective_value,
            changes=list(self.changes),
            source=self.source,
            thought_level=self.thought_level,
        )

    def applied(self, *, rejected: bool) -> ModelApplied:
        """Classify the requested model from what the stream showed. Never a guess upward.

        ``rejected`` is ``collect``'s confirmed pre-prompt refusal. Otherwise: an answered change
        request is ``accepted`` only while the effective value is the request; an unanswered one
        is ``unknown``. With no change request, ``passed`` is said only while the stream's last
        reported value is still the request - a refusal the stream shows (not advertised, not in
        the catalog) that an earlier outcome branch shadowed, or a later
        ``config_option_update`` that moved the model away, is ``unknown``, not ``passed``.
        """
        if not self.requested:
            return ModelApplied.NOT_PASSED
        if self._ambiguous or not self.session_created:
            return ModelApplied.UNKNOWN
        if rejected or (self.set_results and not any(self.set_results)):
            return ModelApplied.REJECTED
        if any(self.set_results):
            return (
                ModelApplied.ACCEPTED
                if self.effective_value == self.requested
                else ModelApplied.UNKNOWN
            )
        if self.set_requests:
            return ModelApplied.UNKNOWN
        # No change request went out. The rejection branch did not confirm a refusal (rule 7: no
        # upgrade without an observation), so a refusal or a different value is unknown.
        if (
            self.refused()
            or self.initial_value != self.requested
            or self.effective_value != self.requested
        ):
            return ModelApplied.UNKNOWN
        return ModelApplied.PASSED
