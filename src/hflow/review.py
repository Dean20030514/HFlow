"""Turn a reviewer invocation's own messages into the canonical :class:`ReviewOutput`.

Why this module exists: the transport (`acpx --format json`) emits raw ACP JSON-RPC
NDJSON. Its *terminal prompt response* carries a stop reason, never HFlow's Review
object - the verdict is ordinary assistant text delivered as ``agent_message_chunk``
updates. Before this module the production driver reported ``review=None`` for every
invocation, so a real reviewer could never reach acceptance.

Two stages, both deterministic and both reusable for offline replay of saved bytes:

1. :class:`AnswerTranscript` reassembles the invocation's **final answer** from the
   chunks the driver actually observed. It reads only ``agent_message_chunk`` updates
   that carry the invocation's own session id, so user prompts, thoughts, tool results,
   other sessions and the implementer transcript cannot supply a verdict.
2. :func:`decode_review` parses exactly one Review object out of that answer and
   validates it against the canonical model in ``contracts.py``.

Nothing here decides acceptance, and nothing here is allowed to fill in a missing
verdict: a missing, malformed or ambiguous answer is an error the controller reports as
a protocol problem, never as the reviewer's substantive rejection.

Supported answer grammar (documented and tested; no other form is accepted):

* the answer *is* one JSON object;
* the answer is exactly one fenced code block (``` or ```` ```json ````) whose content is
  one JSON object - the form the reviewer output contract asks for;
* a prose answer ending in one JSON object - the parser identifies objects by
  structure, so a single trailing object is unambiguous and is accepted verbatim.

Wrapper removal never rewrites the object's bytes: the decoded text's digest is
recorded next to the parsed verdict by the offline replay tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .contracts import ReviewOutput

#: Bound on retained assistant text for one invocation. A looping agent must not be able
#: to grow driver memory without limit, and a verdict read from a truncated answer is not
#: trustworthy - so exceeding this marks the transcript unusable instead of truncating it.
MAX_ANSWER_BYTES = 4 * 1024 * 1024

#: Meaning of ``ReviewDecodeError.kind``.
REVIEW_MISSING = "missing"
REVIEW_INVALID = "invalid"
REVIEW_AMBIGUOUS = "ambiguous"

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
    """The invocation's final assistant message, verbatim."""

    message_id: str
    first_line: int
    last_line: int
    chunk_count: int
    text: str


@dataclass
class AnswerTranscript:
    """Accumulates the assistant messages of one invocation, in stream order.

    Grouping rule (pinned to the observed runtime): chunks belong to the same message when
    they carry the same ``messageId``. ``messageId`` is *optional* in ACP, so when the
    runtime does not send one, a new ``sessionUpdate`` that is not a message chunk ends the
    current message - that is the demonstrable turn boundary, not a guess about the last
    line of the stream. A gap in sequence numbers also ends it.

    The answer is the **last** group, i.e. the message the turn ended with. Intermediate
    commentary is never concatenated into it.
    """

    session_id: str | None = None
    role: str = ""
    _groups: list[list[AnswerChunk]] = field(default_factory=list, repr=False)
    _retained_bytes: int = 0
    _truncated: bool = False
    #: Set when a message chunk could not be read at all; the answer is then unusable.
    _rejected: str = ""
    #: Incremented for message chunks rejected because they belong to another session, so
    #: replay can report what it excluded instead of silently dropping it.
    skipped_other_session: int = 0

    # -- ingest --------------------------------------------------------------

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
            # Thoughts, tool calls, plans and user/agent echoes are separate updates: seeing
            # one ends the current message group, which is what makes the fallback grouping
            # work when the runtime sends no messageId.
            self._end_group()
            return

        if self.session_id is not None:
            session = params.get("sessionId")
            if session != self.session_id:
                self.skipped_other_session += 1
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

        message_id = update.get("messageId")
        identity = message_id if isinstance(message_id, str) and message_id else ""

        if not self._groups:
            self._groups.append([])
        elif identity:
            if self._groups[-1] and self._groups[-1][0].message_id != identity:
                self._groups.append([])
        elif self._groups[-1] and sequence != self._groups[-1][-1].sequence + 1:
            # No messageId, but the chunk is not contiguous with the previous one: a new
            # message started in between.
            self._groups.append([])

        chunk = AnswerChunk(
            message_id=identity,
            line_index=line_index,
            sequence=sequence,
            text=text,
        )
        self._groups[-1].append(chunk)
        size = len(text.encode("utf-8"))
        if not self._truncated:
            # Counted once per chunk: beyond the cap the content is unusable (never
            # truncated-and-kept), and the counter itself must stay bounded.
            self._retained_bytes += size
            if self._retained_bytes > MAX_ANSWER_BYTES:
                self._truncated = True

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
    if not answer.strip():
        raise ReviewDecodeError(REVIEW_MISSING, "the reviewer's final answer is empty")

    region, fenced = _result_region(answer)
    if fenced:
        payload = _loads_object(region, where="the fenced result block")
    else:
        payload = _loads_object(region, where="the answer")
    return validate_review(payload)


def validate_review(payload: Any) -> ReviewOutput:
    """Validate an already-parsed object against the canonical review contract."""
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
    label (``python``, ``text``, ...) is prose decoration, not the result - which is why one
    ```` ```json ```` verdict block inside an answer that also shows code samples is not
    ambiguous. Two candidate result blocks, or two bare objects, is refused instead of
    guessed: choosing the object that validates is exactly the behaviour this parser must
    not have.
    """
    spans = _fenced_spans(answer)
    candidates = [span for span in spans if span.info.lower() in _JSON_FENCE_LANGUAGES]
    if len(candidates) > 1:
        raise ReviewDecodeError(
            REVIEW_AMBIGUOUS,
            f"the answer contains {len(candidates)} candidate result blocks "
            "(unlabelled or json-fenced); the Review object is not identified",
        )
    if candidates:
        return candidates[0].body, True

    objects = _object_spans(answer)
    if not objects:
        if spans:
            labels = ", ".join(sorted({span.info or "<unlabelled>" for span in spans}))
            raise ReviewDecodeError(
                REVIEW_MISSING,
                f"the answer carries no structured verdict: its fenced block(s) are labelled {labels} "
                "and it has no top-level JSON object",
            )
        if not _has_json_value(answer):
            raise ReviewDecodeError(
                REVIEW_MISSING, "the answer contains no JSON object, so it carries no structured verdict"
            )
        raise ReviewDecodeError(
            REVIEW_INVALID, "the answer's JSON is not an object, so it is not a review result"
        )
    if len(objects) > 1:
        raise ReviewDecodeError(
            REVIEW_AMBIGUOUS,
            f"the answer contains {len(objects)} top-level JSON objects; the Review object is not "
            "identified",
        )
    return objects[0], False


@dataclass(frozen=True)
class _FenceSpan:
    info: str
    body: str
    start: int


def _fenced_spans(text: str) -> list[_FenceSpan]:
    """Top-level fenced code blocks, in order.

    A fence is a run of three or more backticks or tildes at the start of a line. Anything
    inside a fence is skipped, so a brace or a marker in a code sample cannot be mistaken
    for the result object.
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
                    _FenceSpan(info=info.strip(), body=text[body_start:offset], start=opened_at)
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


def _object_spans(text: str) -> list[str]:
    """Every top-level ``{...}`` in the text, found by structural scanning.

    String literals and escapes are respected, so a brace inside a quoted value is not a
    boundary. A bare (unfenced) answer that contains two objects is ambiguous by design.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start : index + 1])
                    start = -1
    return spans


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


def _describe(payload: dict[str, Any], exc: ValidationError) -> str:
    problems = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        problems.append(f"{location}: {error.get('msg')}")
    keys = ", ".join(sorted(str(key) for key in payload)) or "<none>"
    return "the review object does not satisfy the contract (" + "; ".join(problems) + f"); keys: {keys}"
