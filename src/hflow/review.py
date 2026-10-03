"""Turn a reviewer invocation's own messages into the canonical :class:`ReviewOutput`.

Why this module exists: the transport (`acpx --format json`) emits raw ACP JSON-RPC
NDJSON. Its *terminal prompt response* carries a stop reason, never HFlow's Review
object - the verdict is ordinary assistant text delivered as ``agent_message_chunk``
updates. Before this module the production driver reported ``review=None`` for every
invocation, so a real reviewer could never reach acceptance.

Two stages, both deterministic and both reusable for offline replay of saved bytes:

1. :class:`AnswerTranscript` reassembles the invocation's **final answer** from the
   chunks the driver actually observed. It reads only ``agent_message_chunk`` updates, so
   user prompts, thoughts, tool results and the implementer transcript cannot supply a
   verdict. The production driver and the offline replay tool build it with
   ``require_session`` and bind it (``bind_session``) to the session the turn's first
   ``session/prompt`` request named: a chunk for any other session, or with no usable
   ``sessionId``, is counted (``skipped_other_session``) and leaves the answer unusable, as
   does a chunk observed before that request or a request that named no session. A transcript
   given a ``session_id`` without ``require_session`` excludes other sessions' chunks, and one
   built with neither reads every session's chunks.
2. :func:`decode_review` parses exactly one Review object out of that answer and
   validates it against the canonical model in ``contracts.py``, typed findings included
   (``Finding``: a required non-blank ``body``; optional ``title``, ``location``,
   ``severity``, ``id``; no other key).

Nothing here decides acceptance, and nothing here is allowed to fill in a missing
verdict: a missing, malformed or ambiguous answer is an error the controller reports as
a protocol problem, never as the reviewer's substantive rejection.

Supported answer grammar (documented and tested; no other form is accepted). Every fenced
block is masked first; the remaining prose is searched for top-level JSON objects by trying a
decode at every ``{`` (leftmost first, resuming after each object found), never by tracking
string or brace state across prose, so an inch mark (``27"``) or a stray ``{`` cannot hide an
object. A *verdict object* is one of those objects that has a ``verdict`` key.

* the answer holds exactly one result block - a fenced code block (``` or ```` ```json ````)
  whose content is one JSON object, the form the reviewer output contract asks for - and no
  verdict object outside its fences. Other JSON in the prose (``{}``, a config snippet) is
  allowed and never read;
* the answer holds no result block and exactly one verdict object (the answer may *be* that
  object). Other objects in the prose are not read;
* the answer holds no result block, no verdict object and exactly one JSON object: it is the
  result, so a misspelt key is reported as invalid rather than missing.

Prose may come before or after the result, in every form; the parser never interprets prose,
so the result is accepted verbatim whatever the surrounding text says. A fence with any other
label (``python``, ``text``, ...) is decoration: its body is never read, so an example object
inside it is neither the verdict nor a competitor. Refused as ambiguous: two result blocks, a
result block next to a verdict object outside it, two verdict objects, two objects of which
none has a verdict key, and an answer with more than :data:`MAX_FAILED_OBJECT_STARTS` places
that begin like an object but do not decode. Prose braces that do not decode as a JSON object,
such as ``{x}``, are never an object.

Wrapper removal never rewrites the object's bytes: the decoded text's digest is
recorded next to the parsed verdict by the offline replay tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .contracts import RecordedUntypedReview, ReviewOutput

#: Bound on retained assistant text for one invocation. A looping agent must not be able
#: to grow driver memory without limit, and a verdict read from a truncated answer is not
#: trustworthy - so exceeding this marks the transcript unusable instead of truncating it.
MAX_ANSWER_BYTES = 4 * 1024 * 1024

#: Meaning of ``ReviewDecodeError.kind``.
REVIEW_MISSING = "missing"
REVIEW_INVALID = "invalid"
REVIEW_AMBIGUOUS = "ambiguous"

#: Bound on places in the prose that begin like a JSON object (``{`` then a key or ``}``) but
#: do not decode. Each failed decode costs time proportional to the answer, so without a bound
#: an answer full of them makes classification quadratic. Past it the answer is refused as
#: ambiguous - never accepted - because a competing verdict may not have been looked for.
MAX_FAILED_OBJECT_STARTS = 64

#: JSON whitespace (RFC 8259): the only characters allowed between ``{`` and its first token.
_JSON_WHITESPACE = " \t\n\r"

#: Fence languages accepted around the Review object. An empty info string is the plain
#: triple-backtick form; ``json`` is the labelled form.
_JSON_FENCE_LANGUAGES = frozenset({"", "json"})

#: Prefix the driver puts on the limitation that explains why no verdict was decoded.
#: ``review_input_error`` reads it back without having to parse prose.
REVIEW_INPUT_PREFIX = "review_"


def review_input_error(limitations: list[str]) -> tuple[str, str] | None:
    """``(kind, detail)`` of a review-wire failure recorded by a driver, if there is one.

    Returns ``None`` when the driver reported no wire problem - the caller must then decide
    whether the invocation had review authority at all.
    """
    for note in limitations:
        if not note.startswith(REVIEW_INPUT_PREFIX):
            continue
        head, _, rest = note.partition(": ")
        kind = head[len(REVIEW_INPUT_PREFIX) :].strip()
        return (kind or "unknown", rest.strip() or note)
    return None


class ReviewDecodeError(ValueError):
    """The final answer did not yield exactly one valid Review object.

    ``kind`` separates *absent* evidence from *unusable* evidence; the controller maps it
    to a block reason instead of pretending the reviewer rejected the candidate.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"review_{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class AnswerChunk:
    """One observed ``agent_message_chunk``, with the evidence needed to trace it."""

    message_id: str
    line_index: int
    sequence: int
    text: str


@dataclass(frozen=True)
class FinalAnswer:
    """The invocation's final assistant message, verbatim.

    ``first_line``/``last_line`` span its message chunks; a thought between them is not part of
    ``text``, and ``chunk_count`` counts message chunks only.
    """

    message_id: str
    first_line: int
    last_line: int
    chunk_count: int
    text: str


@dataclass
class AnswerTranscript:
    """Accumulates the assistant messages of one invocation, in stream order.

    Grouping rule (pinned to the observed runtime): chunks belong to the same message when
    they carry the same ``messageId``, and only a different id starts a new one (ACP: a
    change in ``messageId`` indicates a new message has started). Thoughts, usage updates
    and tool calls are never answer text, and when ids are present they are not boundaries
    either. DSH gives a committed message's reasoning the message's own id (seen in every
    recorded live stream) and emits the message's blocks in content order, then a
    ``usage_update`` (``packages/acp/acp/src/updates.ts``), so a reasoning block between two
    text blocks arrives as a same-id thought inside the message. A ``messageId`` that
    returns after another message started leaves the final message unidentified (ACP's v2
    draft lets chunks append to an earlier message), so the transcript is rejected rather
    than guessing which text is the answer.

    ``messageId`` is *optional* in ACP, so when the runtime does not send one, a new
    ``sessionUpdate`` that is not a message chunk ends the current message - that is the
    demonstrable turn boundary, not a guess about the last line of the stream. A gap in
    sequence numbers also ends it.

    The answer is the **last** message: the text of its ``agent_message_chunk`` updates,
    concatenated in stream order with nothing inserted. Thought text is never part of it,
    and intermediate commentary (an earlier message) is never concatenated into it.

    Session attribution: with a ``session_id`` (given, or bound by ``bind_session``) only that
    session's message chunks are read. With ``require_session``, a message chunk that arrives
    before the binding, or names another session, rejects the transcript.
    """

    session_id: str | None = None
    role: str = ""
    #: The session must come from ``bind_session`` before any message chunk is read. Until it
    #: is bound, no chunk can be attributed to the turn, so one that arrives first rejects the
    #: transcript instead of being kept or dropped.
    require_session: bool = False
    _groups: list[list[AnswerChunk]] = field(default_factory=list, repr=False)
    _retained_bytes: int = 0
    _truncated: bool = False
    #: Set when a message chunk could not be read at all; the answer is then unusable.
    _rejected: str = ""
    #: Every ``messageId`` that has started a message. One that returns after a different
    #: message started leaves the final message unidentified and rejects the transcript.
    _message_ids: set[str] = field(default_factory=set, repr=False)
    #: Incremented for message chunks excluded because they name another session, or no usable
    #: ``sessionId``, so the driver and replay can report what was excluded instead of silently
    #: dropping it.
    skipped_other_session: int = 0
    #: Whether the session is settled: given at construction, or by the first ``bind_session``.
    _bound: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._bound = self.session_id is not None

    # -- ingest --------------------------------------------------------------

    def bind_session(self, session_id: str) -> None:
        """Attribute message chunks to ``session_id``: the one the turn's first prompt named.

        Only the first binding counts; a later prompt does not move the turn to another session.
        An empty id binds nothing attributable, so the transcript is rejected: no chunk could be
        told apart from another session's.
        """
        if self._bound:
            return
        self._bound = True
        if not session_id:
            if not self._rejected:
                self.reject(
                    "the session/prompt request named no sessionId, so no agent_message_chunk "
                    "can be attributed to the turn"
                )
            return
        self.session_id = session_id

    def observe_update(
        self,
        update: dict[str, Any],
        *,
        params: dict[str, Any],
        sequence: int,
        line_index: int,
    ) -> None:
        """Record one ``session/update`` payload observed on the wire."""
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str):
            return
        if kind != "agent_message_chunk":
            # Thoughts, tool calls, plans, usage and user/agent echoes are separate updates and
            # never answer text. Seeing one ends the current message for the messageId-free
            # fallback; a message that carries a messageId is ended only by a different id
            # (see ``_group_for``).
            self._end_group()
            return

        if self.require_session and not self._bound:
            # Before the prompt named its session nothing says whose message this is, and
            # dropping it would silently choose for the turn.
            if not self._rejected:
                self.reject(
                    f"agent_message_chunk on line {line_index} arrived before the session/prompt "
                    "request named the turn's session, so it cannot be attributed to the turn"
                )
            return

        if self.session_id is not None:
            session = params.get("sessionId")
            if session != self.session_id:
                self.skipped_other_session += 1
                if self.require_session and not self._rejected:
                    # A one-session turn that streams another session's message is not a stream
                    # whose final answer can be identified: excluding the chunk would let the
                    # rest of the stream decide, so the answer is unusable instead.
                    self.reject(
                        f"agent_message_chunk on line {line_index} names session "
                        f"{session!r}, not the turn's {self.session_id!r}, so the turn's final "
                        "answer is not identified"
                    )
                return

        if self._rejected:
            # The first reason stands: nothing after an unusable chunk is retained.
            return

        text = _chunk_text(update)
        if text is None:
            # A message chunk this build cannot read makes the whole answer unreadable:
            # dropping it would silently rewrite the reviewer's verdict.
            self.reject(
                f"agent_message_chunk on line {line_index} carries no text content "
                f"({update.get('content')!r})"
            )
            return
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            # The check is per chunk: a chunk whose text alone does not encode as UTF-8 (it holds
            # an unpaired surrogate, such as a \uD83D escape) is rejected, even when the next chunk
            # would complete the surrogate pair. That is fail-closed on purpose: DSH emits one
            # agent_message_chunk per committed text block, so a pair split across chunks is not
            # expected. (A driver that streamed delta-level chunks would need the pair check on
            # the joined text instead.) Measuring the text must not raise out of the driver's
            # reader thread, and keeping it would carry an unencodable string into stored
            # evidence, so the answer is unusable.
            self.reject(
                f"agent_message_chunk on line {line_index} carries text with an unpaired "
                "surrogate, which is not valid Unicode, so the answer is not readable text"
            )
            return

        message_id = update.get("messageId")
        identity = message_id if isinstance(message_id, str) and message_id else ""

        group = self._group_for(identity, sequence=sequence, line_index=line_index)
        if group is None:
            return

        chunk = AnswerChunk(
            message_id=identity,
            line_index=line_index,
            sequence=sequence,
            text=text,
        )
        group.append(chunk)
        if not self._truncated:
            # Counted once per chunk: beyond the cap the content is unusable (never
            # truncated-and-kept), and the counter itself must stay bounded.
            self._retained_bytes += size
            if self._retained_bytes > MAX_ANSWER_BYTES:
                self._truncated = True

    def _group_for(
        self, identity: str, *, sequence: int, line_index: int
    ) -> list[AnswerChunk] | None:
        """The message a chunk belongs to, or ``None`` once the final message is unidentifiable."""
        previous = next((group for group in reversed(self._groups) if group), None)
        if identity:
            if previous is not None and previous[0].message_id == identity:
                # The same message, resumed after a thought, usage update or tool call that
                # ``_end_group`` treated as a boundary for the messageId-free fallback.
                while not self._groups[-1]:
                    self._groups.pop()
                return previous
            if identity in self._message_ids:
                self.reject(
                    f"agent_message_chunk on line {line_index} continues message {identity!r} "
                    "after a later message started, so the final message is not identified"
                )
                return None
            self._message_ids.add(identity)
        elif self._groups and self._groups[-1] and sequence == self._groups[-1][-1].sequence + 1:
            # No messageId: a contiguous chunk continues the open message.
            return self._groups[-1]
        if not self._groups or self._groups[-1]:
            self._groups.append([])
        return self._groups[-1]

    def _end_group(self) -> None:
        if self._groups and self._groups[-1]:
            self._groups.append([])

    def reject(self, detail: str) -> None:
        """Mark the whole transcript unusable (a chunk could not be read).

        Dropping just that chunk would silently rewrite the answer, and guessing at it is
        worse; an unreadable answer yields no verdict at all.
        """
        self._rejected = detail
        self._groups = []

    # -- extract -------------------------------------------------------------

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def rejected(self) -> str:
        return self._rejected

    @property
    def observed_message_count(self) -> int:
        return len([group for group in self._groups if group])

    def chunks_after(self, line_index: int) -> int:
        """Retained message chunks observed on a stream line after ``line_index``.

        The caller asks with the line of the turn's own prompt response: a chunk after it was
        sent outside the settled turn and can change which message reads as the final one.
        """
        return sum(1 for group in self._groups for chunk in group if chunk.line_index > line_index)

    def final_answer(self) -> FinalAnswer | None:
        """The last assistant message, or ``None`` when the invocation never spoke."""
        if self._truncated or self._rejected:
            return None
        groups = [group for group in self._groups if group]
        if not groups:
            return None
        group = groups[-1]
        return FinalAnswer(
            message_id=group[0].message_id,
            first_line=group[0].line_index,
            last_line=group[-1].line_index,
            chunk_count=len(group),
            text="".join(chunk.text for chunk in group),
        )


def _chunk_text(update: dict[str, Any]) -> str | None:
    """Text of one message chunk. ``None`` when the update carries no text block."""
    content = update.get("content")
    if not isinstance(content, dict):
        return None
    if content.get("type") != "text":
        return None
    text = content.get("text")
    return text if isinstance(text, str) else None


# --------------------------------------------------------------------------
# Review decoding
# --------------------------------------------------------------------------


def decode_review(answer: str) -> ReviewOutput:
    """Decode exactly one canonical Review object from a reviewer's final answer.

    Raises :class:`ReviewDecodeError` for anything else. The raw answer is never modified
    and no field is ever filled in on the model's behalf.
    """
    return validate_review(_answer_payload(answer))


def decode_untyped_recorded_review(answer: str) -> RecordedUntypedReview:
    """Decode a *historical* answer against the contract its reviewer was shown: untyped findings.

    Same grammar as :func:`decode_review`; only the finding objects are not typed. This exists so
    an offline replay of bytes recorded before typed findings can still read their verdict. It is
    never a fallback for a new answer: the drivers call :func:`decode_review` only, and a caller
    that uses this must say which contract the verdict was read under.
    """
    payload = _answer_payload(answer)
    try:
        return RecordedUntypedReview.model_validate(payload)
    except ValidationError as exc:
        raise ReviewDecodeError(REVIEW_INVALID, _describe(payload, exc)) from exc


def _answer_payload(answer: str) -> dict[str, Any]:
    """The one JSON object the answer grammar admits, parsed but not yet validated."""
    if not answer.strip():
        raise ReviewDecodeError(REVIEW_MISSING, "the reviewer's final answer is empty")

    region, fenced = _result_region(answer)
    if fenced:
        payload = _loads_object(region, where="the fenced result block")
    else:
        payload = _loads_object(region, where="the answer")
    if not isinstance(payload, dict):
        raise ReviewDecodeError(
            REVIEW_INVALID, f"the review result must be a JSON object, not {type(payload).__name__}"
        )
    return payload


def validate_review(payload: Any) -> ReviewOutput:
    """Validate an already-parsed object against the canonical review contract.

    Every finding must satisfy the typed :class:`~hflow.contracts.Finding`: an extra key, a
    blank ``body``, an unknown severity or a bad line range makes the whole answer
    ``REVIEW_INVALID``. Nothing is coerced, dropped or turned into text on the model's behalf.
    """
    if not isinstance(payload, dict):
        raise ReviewDecodeError(
            REVIEW_INVALID, f"the review result must be a JSON object, not {type(payload).__name__}"
        )
    try:
        return ReviewOutput.model_validate(payload)
    except ValidationError as exc:
        raise ReviewDecodeError(REVIEW_INVALID, _describe(payload, exc)) from exc


def _result_region(answer: str) -> tuple[str, bool]:
    """Return the text that must contain the Review object, and whether it was fenced.

    A fenced block is a *result block* when its info string is empty or ``json``. Any other
    label (``python``, ``text``, ...) is prose decoration, not the result. Every fenced block
    is masked out before the prose is searched for JSON objects (:func:`_top_level_objects`),
    so an example object inside a ```` ```text ```` block is never the verdict and never a
    competitor. A *verdict object* is a JSON object in the prose that has a ``verdict`` key.

    * Two or more candidate result blocks: ambiguous.
    * One candidate result block: its body is the result. A verdict object outside it is a
      competing result and makes the answer ambiguous; any other JSON in the prose (``{}``, a
      config snippet) is allowed and never read.
    * No candidate result block: one verdict object is the result and other objects in the
      prose are not read; two or more verdict objects are ambiguous. Without any verdict
      object, a single JSON object is the result (so a misspelt key is reported as invalid,
      not missing) and several are ambiguous.

    Conflicts are refused instead of guessed: choosing the object that validates is exactly
    the behaviour this parser must not have.
    """
    spans = _fenced_spans(answer)
    candidates = [span for span in spans if span.info.lower() in _JSON_FENCE_LANGUAGES]
    if len(candidates) > 1:
        raise ReviewDecodeError(
            REVIEW_AMBIGUOUS,
            f"the answer contains {len(candidates)} candidate result blocks "
            "(unlabelled or json-fenced); the Review object is not identified",
        )
    outside = _mask_fences(answer, spans)
    objects, first_error = _top_level_objects(outside)
    verdicts = [found for found in objects if found.has_verdict]
    if candidates:
        if verdicts:
            raise ReviewDecodeError(
                REVIEW_AMBIGUOUS,
                f"the answer contains a fenced result block and {len(verdicts)} JSON object(s) "
                'with a "verdict" key outside it; the Review object is not identified',
            )
        return candidates[0].body, True

    if len(verdicts) > 1:
        raise ReviewDecodeError(
            REVIEW_AMBIGUOUS,
            f'the answer contains {len(verdicts)} top-level JSON objects with a "verdict" key '
            "and no result block; the Review object is not identified",
        )
    if verdicts:
        return verdicts[0].text, False
    if len(objects) > 1:
        raise ReviewDecodeError(
            REVIEW_AMBIGUOUS,
            f"the answer contains {len(objects)} top-level JSON objects, none with a "
            '"verdict" key, and no result block; the Review object is not identified',
        )
    if objects:
        # One object without a verdict key: decoding it reports what the contract misses.
        return objects[0].text, False
    if first_error is not None:
        raise ReviewDecodeError(
            REVIEW_INVALID,
            "the answer has a '{' outside its fences but no JSON object starts at any of them; "
            f"the first one is not valid JSON: {first_error}",
        )
    if spans:
        labels = ", ".join(sorted({span.info or "<unlabelled>" for span in spans}))
        raise ReviewDecodeError(
            REVIEW_MISSING,
            f"the answer carries no structured verdict: its fenced block(s) are labelled {labels} "
            "and it has no top-level JSON object",
        )
    if not _has_json_value(outside):
        raise ReviewDecodeError(
            REVIEW_MISSING, "the answer contains no JSON object, so it carries no structured verdict"
        )
    raise ReviewDecodeError(
        REVIEW_INVALID, "the answer's JSON is not an object, so it is not a review result"
    )


def _mask_fences(text: str, spans: list[_FenceSpan]) -> str:
    """The text with every fenced block, fence lines included, blanked out.

    Line breaks are kept and every other character becomes a space, so what remains is
    exactly the answer's prose, at unchanged offsets.
    """
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(text[cursor : span.start])
        pieces.append(
            "".join(ch if ch in "\r\n" else " " for ch in text[span.start : span.end])
        )
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


@dataclass(frozen=True)
class _FenceSpan:
    info: str
    body: str
    #: Offset of the opening fence line, and offset just past the closing fence line.
    start: int
    end: int


def _fenced_spans(text: str) -> list[_FenceSpan]:
    """Top-level fenced code blocks, in order.

    A fence is a run of three or more backticks or tildes at the start of a line. Only a
    block labelled ``json`` or left unlabelled is ever read; :func:`_mask_fences` blanks every
    block before the answer is scanned for bare objects, so a brace or a marker in a code
    sample cannot be mistaken for the result object.
    """
    spans: list[_FenceSpan] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    open_fence: tuple[str, str, int, int] | None = None  # marker, info, body start, open offset
    for line in lines:
        stripped = line.lstrip()
        marker = _fence_marker(stripped)
        if open_fence is None:
            if marker is not None:
                info = stripped[len(marker) :].strip()
                open_fence = (marker[0], info, offset + len(line), offset)
        else:
            closing, info, body_start, opened_at = open_fence
            if marker is not None and marker[0] == closing and stripped.strip() == marker:
                spans.append(
                    _FenceSpan(
                        info=info.strip(),
                        body=text[body_start:offset],
                        start=opened_at,
                        end=offset + len(line),
                    )
                )
                open_fence = None
        offset += len(line)
    if open_fence is not None:
        raise ReviewDecodeError(REVIEW_INVALID, "the answer has an unterminated code fence")
    return spans


def _fence_marker(stripped_line: str) -> str | None:
    """The fence run at the start of a line, or ``None``. Never a regex over the answer."""
    for character in ("`", "~"):
        run = 0
        while run < len(stripped_line) and stripped_line[run] == character:
            run += 1
        if run >= 3:
            return character * run
    return None


@dataclass(frozen=True)
class _ObjectSpan:
    #: The object's text exactly as it appears in the scanned (fence-masked) text.
    text: str
    has_verdict: bool


def _top_level_objects(text: str) -> tuple[list[_ObjectSpan], str | None]:
    """Every top-level JSON object in the prose, leftmost first, and the first decode error.

    The prose is never scanned with JSON string or depth state: an inch mark (``27"``) or an
    unbalanced ``{`` in a sentence would otherwise hide every object after it. Instead a
    decode is attempted at every ``{`` offset; a position where an object decodes records
    that object and the search resumes after its end (so objects nested inside it are part
    of it), and any other position is skipped. The caller passes the answer with its fences
    masked, so a fenced body is never found here.

    Lenient on purpose: this only *finds and classifies* objects. The object that is finally
    decoded goes through the strict loader, which refuses repeated keys and non-finite
    constants. The second value is the decode error at the first ``{`` that did not start an
    object, or ``None`` when every ``{`` did (or there was none).
    """
    decoder = json.JSONDecoder()
    found: list[_ObjectSpan] = []
    first_error: str | None = None
    failed_starts = 0
    index = text.find("{")
    while index >= 0:
        cursor = index + 1
        while cursor < len(text) and text[cursor] in _JSON_WHITESPACE:
            cursor += 1
        if cursor >= len(text) or text[cursor] not in '"}':
            # The JSON grammar allows only a key or "}" after "{": no object starts here, and
            # a decode attempt (whose error costs time proportional to the offset) is skipped.
            if first_error is None:
                first_error = _decode_error(decoder, text, index)
            index = text.find("{", index + 1)
            continue
        value: Any = None
        end = index
        try:
            value, end = decoder.raw_decode(text, index)
        except RecursionError:
            if first_error is None:
                first_error = "the object nests too deeply to decode"
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            if first_error is None:
                first_error = str(exc)
        if not isinstance(value, dict):
            failed_starts += 1
            if failed_starts > MAX_FAILED_OBJECT_STARTS:
                raise ReviewDecodeError(
                    REVIEW_AMBIGUOUS,
                    f"more than {MAX_FAILED_OBJECT_STARTS} places outside the answer's fences "
                    "begin like a JSON object but do not decode; the search for a competing "
                    "verdict stopped, so the Review object is not identified",
                )
        if isinstance(value, dict):
            found.append(_ObjectSpan(text=text[index:end], has_verdict="verdict" in value))
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)
    return found, first_error


def _decode_error(decoder: json.JSONDecoder, text: str, index: int) -> str:
    """The decoder's own message for the failed decode at ``index``."""
    try:
        decoder.raw_decode(text, index)
    except RecursionError:
        return "the object nests too deeply to decode"
    except ValueError as exc:
        return str(exc)
    return "the value there is not a JSON object"


def _has_json_value(text: str) -> bool:
    """Is there any JSON value at all in the text? Used only to tell "nothing" from "wrong"."""
    stripped = text.strip()
    if not stripped:
        return False
    try:
        json.loads(stripped, parse_constant=_reject_constant)
    except ReviewDecodeError:
        return True  # a non-finite constant *is* JSON-ish input, just not acceptable
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _reject_constant(name: str) -> Any:
    """``json`` accepts ``NaN``/``Infinity`` by default; the review contract does not."""
    raise ReviewDecodeError(REVIEW_INVALID, f"the answer contains the non-finite JSON constant {name}")


class _UniqueKeys(dict):
    """Object hook that refuses a repeated key instead of silently keeping the last value."""

    def __init__(self, pairs: Any) -> None:
        super().__init__()
        for key, value in pairs:
            if key in self:
                raise ReviewDecodeError(
                    REVIEW_INVALID, f"the review object repeats the key {key!r}"
                )
            self[key] = value


def _loads_object(text: str, *, where: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ReviewDecodeError(REVIEW_MISSING, f"{where} is empty")
    try:
        return json.loads(
            stripped, object_pairs_hook=_UniqueKeys, parse_constant=_reject_constant
        )
    except ReviewDecodeError:
        raise
    except json.JSONDecodeError as exc:
        raise ReviewDecodeError(REVIEW_INVALID, f"{where} is not valid JSON: {exc}") from exc
    except ValueError as exc:
        # A plain ValueError (for example an integer longer than the interpreter's digit
        # limit) is still an unreadable review, not a driver failure.
        raise ReviewDecodeError(REVIEW_INVALID, f"{where} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # Nesting past the decoder's stack is the same: unreadable, never a crash.
        raise ReviewDecodeError(REVIEW_INVALID, f"{where} nests too deeply to decode") from exc


def _describe(payload: dict[str, Any], exc: ValidationError) -> str:
    problems = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        problems.append(f"{location}: {error.get('msg')}")
    keys = ", ".join(sorted(str(key) for key in payload)) or "<none>"
    return "the review object does not satisfy the contract (" + "; ".join(problems) + f"); keys: {keys}"
