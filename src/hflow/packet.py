"""Deterministic input packets: what an implementer or a reviewer is actually told.

Why this module exists (plan 5.1): the production driver used to send ``request.goal`` and
nothing else, so the acceptance criteria, the write scope, the frozen candidate identity and
the check results never reached the Harness. The worker had to guess its own scope; the
reviewer was asked to review a candidate it was never told about. Both are *wiring* defects,
not model defects, and the fix belongs in one deterministic renderer rather than in a string
built at each call site.

Three rules this module keeps:

* the controller supplies every fact; a driver only transports the rendered text. Nothing
  here inspects a repository, reads a file or calls a model;
* rendering never truncates a *required* field. When the result exceeds
  :data:`MAX_PACKET_BYTES` a :class:`PacketTooLargeError` is raised, and the caller refuses
  the dispatch - silently dropping an acceptance criterion is worse than not starting;
* the digest is a hash of the exact text that is sent, so a driver can prove *which* prompt
  its invocation received instead of the controller assuming the packet arrived.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from .contracts import AcceptanceCriterion, RepairContext, ReviewOutput, Scope, canonical_json, digest_of

PacketRole = Literal["implementer", "reviewer"]

#: Bound on one rendered packet. The plan proposes 32 KiB for a small task description plus
#: summaries: a full diff and full logs travel by *reference*, never inline. This is an
#: adjustable operating value, not a measured optimum.
MAX_PACKET_BYTES = 32 * 1024

#: Longest rendered command line in a check summary. A check argv is short by construction;
#: this only stops a pathological one from dominating the packet.
MAX_COMMAND_CHARS = 400

#: Longest stored check detail copied into a packet. The complete text stays in the evidence
#: row and in the run's own logs, so truncating the *summary* hides nothing.
MAX_DETAIL_CHARS = 600

#: How many evidence rows are rendered before the list is closed with an explicit marker.
MAX_EVIDENCE_ROWS = 200

#: The exact contract a reviewer's final message must satisfy. Written out rather than
#: described in prose: the reviewer is told the same thing the parser enforces.
OUTPUT_CONTRACT_NOTE = (
    "Return exactly one JSON object, in your final message, matching the schema below.\n"
    "Put it in a single fenced block labelled json (```json ... ```), and put nothing else\n"
    "inside that block. Prose outside the block is allowed, but no other object with a\n"
    "verdict key may appear anywhere in the message. Required keys: verdict, findings.\n"
    "verdict must be exactly \"accepted\" or \"changes_requested\"; findings is a list of\n"
    "objects (they may be empty). No trailing commas, no comments, no second result block.\n"
    "If the work is not acceptable, use verdict \"changes_requested\" and put the defects in\n"
    "findings. A missing, malformed or duplicated object is a protocol error, not a rejection."
)


class PacketTooLargeError(ValueError):
    """A required field did not fit the packet bound; the dispatch must be refused."""


@dataclass(frozen=True)
class RenderedPacket:
    """One rendered packet plus the digest of the exact bytes that travel."""

    role: PacketRole
    text: str
    digest: str
    byte_length: int

    def __str__(self) -> str:  # pragma: no cover - convenience for operators
        return self.text


def packet_digest(text: str) -> str:
    """Digest of the prompt text as handed to the transport. Same helper the offline tools use.

    What this identifies: the *local* input HFlow sent. It is not a receipt from the ACP server or
    the model - nothing in this repository acknowledges receipt remotely, and the value is computed
    before the bytes leave the process. "The prompt arrived" is evidenced in tests by the
    receiving agent's own record of what it read (`tests/fixtures/checking_acp_agent.py`), not by
    this digest.
    """
    return digest_of({"prompt": text})


def _finish(role: PacketRole, text: str) -> RenderedPacket:
    # No surrounding whitespace: acpx trims the prompt it reads from stdin, so a trailing newline
    # here would make the digest cover bytes the agent never receives.
    payload = text.strip()
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise PacketTooLargeError(
            f"the {role} input packet is {len(encoded)} bytes, above the {MAX_PACKET_BYTES} byte "
            "bound. Nothing was truncated: shorten the task description or move detail into "
            "referenced files, then submit again."
        )
    return RenderedPacket(role=role, text=payload, digest=packet_digest(payload), byte_length=len(encoded))


# --------------------------------------------------------------------------
# small deterministic renderers
# --------------------------------------------------------------------------


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def _acceptance_lines(criteria: list[AcceptanceCriterion]) -> str:
    lines = [
        f"- {criterion.id}: {criterion.statement} "
        f"(proved by check(s): {', '.join(criterion.check_ids) or 'none'})"
        for criterion in criteria
    ]
    return "\n".join(lines) if lines else "- (no acceptance criteria were supplied)"


def _check_lines(check_ids: list[str], summaries: list[dict[str, object]]) -> str:
    """Required checks, with the observed result when this run already executed them.

    Two kinds of content are kept apart on purpose:

    * **references** - the artifact path, the stream sizes/digests/truncation flags and the exit
      reason. These identify where the evidence is and are never shortened: a reader that cannot
      obtain the path cannot read the log, so a clipped reference is worse than a long line;
    * **prose** - the command line and the check's own detail text. A long one of these is cut,
      with the cut stated, because it is a summary of something already available in full at the
      reference.

    The whole packet is still bounded; if the references themselves do not fit, the packet is
    refused by :func:`_finish` rather than having a path cut in half.
    """
    by_id = {str(row.get("check_id", "")): row for row in summaries}
    wanted = list(dict.fromkeys([*check_ids, *by_id.keys()]))
    lines: list[str] = []
    for check_id in wanted:
        row = by_id.get(check_id)
        if row is None:
            lines.append(f"- {check_id}: not executed in this run")
            continue
        status = str(row.get("status", "unknown"))
        exit_code = row.get("exit_code")
        reason = str(row.get("reason", "") or "")
        artifact = str(row.get("artifact", "") or "")
        parts = [f"- {check_id}: {status}"]
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        if reason:
            parts.append(f"reason={reason}")
        if artifact:
            parts.append(f"artifact={artifact}")
        for name in ("stdout", "stderr"):
            capture = row.get(name)
            if not isinstance(capture, dict):
                continue
            parts.append(
                f"{name}={capture.get('retained_bytes')}/{capture.get('total_bytes')}B"
                f" truncated={capture.get('truncated')} digest={capture.get('digest')}"
            )
        reference_line = " ".join(parts)

        summary_parts: list[str] = []
        command = str(row.get("command", "") or "")
        if len(command) > MAX_COMMAND_CHARS:
            command = command[:MAX_COMMAND_CHARS] + "… (command truncated for display)"
        if command:
            summary_parts.append(f"argv={command}")
        detail = str(row.get("detail", "") or "")
        if len(detail) > MAX_DETAIL_CHARS:
            detail = detail[:MAX_DETAIL_CHARS] + "… (detail truncated; see the artifact reference)"
        if detail:
            summary_parts.append(f"detail={detail}")
        lines.append(reference_line)
        if summary_parts:
            # Indented under the reference, so a reader can see which line must not be cut.
            lines.append("  " + " ".join(summary_parts))
    return "\n".join(lines) if lines else "- (no approved check is referenced by the acceptance criteria)"


def _evidence_lines(rows: list[dict[str, object]]) -> str:
    """The evidence rows, each on one line with its references intact.

    The artifact path is rendered in full here too: a row whose reference is unreadable is a row a
    reviewer cannot verify, whatever else the line says.
    """
    if not rows:
        return "- (no program evidence row was recorded for this run)"
    lines: list[str] = []
    for row in rows[:MAX_EVIDENCE_ROWS]:
        parts = [
            f"- {row.get('evidence_id', '?')}",
            f"check={row.get('check_id', '')}",
            f"status={row.get('status', '')}",
        ]
        if row.get("exit_code") is not None:
            parts.append(f"exit={row.get('exit_code')}")
        if row.get("artifact"):
            parts.append(f"artifact={row['artifact']}")
        if row.get("stdout_digest"):
            parts.append(f"stdout_digest={row.get('stdout_digest')}")
        if row.get("candidate_fingerprint"):
            parts.append(f"candidate_fingerprint={row.get('candidate_fingerprint')}")
        lines.append(" ".join(parts))
    omitted = len(rows) - len(lines)
    if omitted > 0:
        lines.append(f"- (+{omitted} more evidence row(s) stored in the run database)")
    return "\n".join(lines)


def _candidate_lines(candidate: dict[str, object] | None) -> str:
    candidate = candidate or {}
    fingerprint = str(candidate.get("fingerprint", "") or "unavailable")
    lines = [f"- candidate content fingerprint: {fingerprint}"]
    commit = str(candidate.get("git_commit", "") or "")
    tree = str(candidate.get("git_tree", "") or "")
    if commit or tree:
        lines.append(f"- candidate commit: {commit or 'not recorded'}")
        lines.append(f"- candidate tree: {tree or 'not recorded'}")
    else:
        lines.append(
            "- candidate commit: none - this run did not use a Git worktree, so the content "
            "fingerprint above is the candidate identity"
        )
    lines.append(f"- base commit the candidate was produced from: {candidate.get('base_commit') or 'not recorded'}")
    if candidate.get("worktree"):
        lines.append(f"- frozen worktree: {candidate['worktree']}")
    if candidate.get("paths"):
        paths = candidate["paths"]
        assert isinstance(paths, list)
        lines.append(f"- paths changed from the base commit: {_path_list(paths)}")
    return "\n".join(lines)


def _path_list(paths: list[object]) -> str:
    """At most twenty paths and a count of the rest, so a wide change cannot grow the packet."""
    shown = ", ".join(str(path) for path in paths[:20])
    more = f" (+{len(paths) - 20} more)" if len(paths) > 20 else ""
    return f"{shown}{more}"


def _diff_reference(candidate: dict[str, object] | None) -> str:
    """Where a reviewer can obtain the diff, without inlining it."""
    candidate = candidate or {}
    commit = str(candidate.get("git_commit", "") or "")
    base = str(candidate.get("base_commit", "") or "")
    if commit and base:
        return (
            f"git diff {base} {commit} (the candidate is a real commit in the frozen worktree "
            "named above; the HFlow candidate ref points at it)"
        )
    return (
        "no Git commit exists for this run; diff the frozen workspace against the base commit "
        "recorded above (the content fingerprint is the authoritative candidate identity)"
    )


def _round_change_lines(candidate: dict[str, object] | None) -> str:
    """A repair round's own patch, labelled, in addition to the cumulative diff - never instead.

    The candidate a repair round produced is the whole change from the task's base: that is what
    is delivered, and it is what the base, the paths and the diff above describe. What this round
    added on top of the previous round's candidate is shown here, separately, so a reviewer can
    see where the repair started without mistaking the repair patch for the delivery. Empty for
    a first round.
    """
    candidate = candidate or {}
    parent = str(candidate.get("round_parent_commit", "") or "")
    commit = str(candidate.get("git_commit", "") or "")
    if not parent or not commit:
        return ""
    round_paths = candidate.get("round_paths") or []
    assert isinstance(round_paths, list)
    return (
        f"\n- this round's change (repair round {candidate.get('round', '?')}, on top of the "
        "previous round's candidate; part of the diff above, not a replacement for it): "
        f"git diff {parent} {commit}"
        f"\n- paths this round changed: {_path_list(round_paths) or 'none'}"
    )


# --------------------------------------------------------------------------
# the two packets
# --------------------------------------------------------------------------


#: Longest single value of a reviewer finding copied into a repair packet, in UTF-8 bytes. Every
#: key is rendered; a value above this is cut with an explicit ``…[truncated N bytes]`` marker,
#: and the packet bound still refuses a section that does not fit as a whole.
MAX_FINDING_VALUE_BYTES = 2048


def _capped_finding_value(value: object) -> object:
    """One finding value, unchanged if it fits :data:`MAX_FINDING_VALUE_BYTES`, else cut and marked.

    A string is cut on a character boundary. Any other value is measured as its canonical JSON
    text, and only an oversize one is replaced by that text cut and marked, so the cap bounds a
    long list or object as well as a long string.
    """
    text = value if isinstance(value, str) else canonical_json(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_FINDING_VALUE_BYTES:
        return value
    kept = encoded[:MAX_FINDING_VALUE_BYTES].decode("utf-8", errors="ignore")
    dropped = len(encoded) - len(kept.encode("utf-8"))
    return f"{kept}…[truncated {dropped} bytes]"


def _repair_section(context: RepairContext) -> str:
    """The one repair attempt's input: which candidate it starts from, and what failed.

    Everything here is a recorded fact, and the section says what it is *not*: a permission
    grant, a research brief or an instruction. The write scope, the checks and the configuration
    are unchanged, so the section cannot be read as widening what this attempt may do.

    The previous candidate's paths are capped like the reviewer's lists (twenty plus a count)
    and the full list is given as a ``git diff --no-renames --name-only`` reference (without
    ``--no-renames`` a moved file's deleted source would drop out, and the command would not
    reproduce the recorded paths): one directory entry in
    ``write_allow`` admits any number of changed files, and a wide first round must not make
    every repair packet exceed :data:`MAX_PACKET_BYTES`.
    """
    previous = context.previous
    candidate_lines = ["- no previous candidate is recorded (this looks like a first attempt)"]
    if previous is not None:
        candidate_lines = [
            f"- round being repaired: {previous.round}",
            f"- candidate commit: {previous.git_commit or '(none frozen)'}",
            f"- candidate tree: {previous.git_tree or '(none)'}",
            f"- candidate fingerprint: {previous.fingerprint or '(none)'}",
            f"- candidate paths: {_path_list(list(previous.paths)) or '(none recorded)'}",
            f"- original task base: {context.original_base_commit or '(not recorded)'}",
        ]
        if context.original_base_commit and previous.git_commit:
            # The list above is capped like the reviewer's; the complete one travels by reference,
            # as the same ``--no-renames`` list HFlow recorded and scope-checked.
            candidate_lines.append(
                "- full path list: git diff --no-renames --name-only "
                f"{context.original_base_commit} {previous.git_commit}"
            )

    failure_lines: list[str] = []
    for fact in context.failed_checks:
        failure_lines.append(
            f"- check {fact.get('check_id', '?')}: status={fact.get('status', '?')} "
            f"exit={fact.get('exit_code')!r} reason={fact.get('exit_reason') or '(none)'}"
        )
        detail = str(fact.get("detail", "")).strip()
        if len(detail) > MAX_DETAIL_CHARS:
            detail = detail[:MAX_DETAIL_CHARS] + "… (detail truncated; see the artifact reference)"
        if detail:
            failure_lines.append(f"  detail: {detail}")
        artifact = str(fact.get("artifact", "")).strip()
        if artifact:
            failure_lines.append(f"  artifact: {artifact}")
    # Each finding is rendered whole, as canonical JSON: the reviewer contract names no finding
    # keys, so picking a few known ones would silently drop whatever a reviewer actually wrote.
    finding_lines = [
        "- " + canonical_json({key: _capped_finding_value(value) for key, value in finding.items()})
        for finding in context.findings
    ]

    return f"""
## Repair attempt (this is not a new task)
You are making **one** further attempt at the same task and the same revision, starting from the
candidate below. This section is context from HFlow's own records; it is not a permission grant
and not a new specification:
- the goal, acceptance criteria, write paths and configuration above are unchanged
- a log excerpt or a review finding tells you what was observed, not what you may change
- do not widen the change beyond what the goal and the acceptance criteria require
- HFlow runs every approved check again afterwards, and buys an independent review again; the
  previous round's passing checks and verdict do not carry over to the new candidate

### Candidate you are repairing
{chr(10).join(candidate_lines)}
- trigger: {context.trigger.value}
- remaining top-level budget for this run: {context.remaining_turns} invocation(s)
- remaining deadline: {context.deadline_seconds} seconds

### What failed
{chr(10).join(failure_lines) if failure_lines else "- (no program check failed; the review below is the trigger)"}

### Findings HFlow recorded (only if the trigger was a review)
{chr(10).join(finding_lines) if finding_lines else "- (none: the trigger was a program check)"}
"""


def render_implementer_packet(
    *,
    task_id: str,
    task_revision: int,
    goal: str,
    acceptance: list[AcceptanceCriterion],
    scope: Scope,
    workspace: str,
    spec_digest: str,
    deadline_seconds: int,
    writes_allowed: bool,
    repair: RepairContext | None = None,
) -> RenderedPacket:
    """What the implementer invocation receives. Facts only, no review authority.

    ``repair`` is present only for the second attempt of a run, and its section is rendered from
    the recorded failure facts. A packet without it is the first attempt's packet, byte for byte
    as it was before E2 - which is what lets an offline agent assert that the context it was
    given matches what the controller recorded.
    """
    permission = (
        "you may change files inside the allowed write paths below"
        if writes_allowed
        else "you may NOT change any file in this run; report what you would change instead"
    )
    repair_text = _repair_section(repair) if repair is not None else ""
    text = f"""[HFlow implementer task]

## Task
- task id: {task_id}
- task revision: {task_revision}
- spec digest: {spec_digest}
- goal (verbatim from the TaskSpec):
{goal}

## Acceptance criteria (all of them must hold)
{_acceptance_lines(acceptance)}

## Allowed write paths (nothing outside these may be modified)
{_bullets(list(scope.write_allow), "none - this task authorizes no write path")}

## Forbidden write paths (in addition to the allowed list)
{_bullets(list(scope.write_deny), "none beyond the allowed list")}

## Workspace
- work in this directory: {workspace}
- do not create HFlow scaffolding, caches or logs in the workspace
- do not commit, push, merge or rewrite Git history

## Formal checks
- HFlow runs the approved checks after you finish; they are looked up in the project
  contract and you cannot choose or edit them.
- required check ids: {", ".join(dict.fromkeys(cid for c in acceptance for cid in c.check_ids)) or "none"}
- your own targeted tests are useful while developing, but they are not the formal evidence.

## External side effects (must all hold)
- no network calls, installs, dependency upgrades or downloads
- no deploy, publish, release, migration or scheduled job
- no writes outside the workspace above, and no writes to any forbidden path
- no Git remote operations, no credential or secret access

## Runtime
- deadline: {deadline_seconds} seconds
- file permission: {permission}

## When you finish
- make the requested change, and leave the workspace in a state where the formal checks can
  run without extra setup
- report what you changed and anything you could not do; a clear partial report is better
  than an unverified claim of completion
{repair_text}"""
    return _finish("implementer", text)


def render_reviewer_packet(
    *,
    task_id: str,
    task_revision: int,
    goal: str,
    acceptance: list[AcceptanceCriterion],
    scope: Scope,
    workspace: str,
    spec_digest: str,
    candidate_fingerprint: str,
    deadline_seconds: int,
    candidate: dict[str, object] | None = None,
    verification_status: str = "not_run",
    verification_detail: str = "",
    check_summaries: list[dict[str, object]] | None = None,
    evidence_rows: list[dict[str, object]] | None = None,
) -> RenderedPacket:
    """What the reviewer invocation receives: task, candidate identity, program evidence.

    Deliberately absent: the implementer's conversation, its reasoning, and its own claim
    about the result. The reviewer gets the task, the frozen candidate and the recorded
    program checks - not a summary written by the party being reviewed.
    """
    acceptance_ids = ", ".join(criterion.id for criterion in acceptance) or "none"
    required_checks = [cid for criterion in acceptance for cid in criterion.check_ids]
    detail = verification_detail
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[:MAX_DETAIL_CHARS] + "… (truncated for display)"
    schema = json.dumps(ReviewOutput.model_json_schema(), indent=2, sort_keys=True)
    text = f"""[HFlow reviewer task]

## Role and authority
- You are the independent reviewer of one frozen candidate. Review the frozen candidate
  against the acceptance criteria; your only output is the verdict object below.
- Read-only: do not edit, create or delete any file, do not fix the implementation, and do
  not run commands that change the workspace.
- You may read the candidate, its diff and the recorded check results.

## Task under review
- task id: {task_id}
- task revision: {task_revision}
- spec digest: {spec_digest}
- goal (verbatim from the TaskSpec):
{goal}

## Acceptance criteria (each one must be judged)
{_acceptance_lines(acceptance)}
- acceptance ids: {acceptance_ids}

## Declared scope
- allowed write paths:
{_bullets(list(scope.write_allow), "none - this task authorizes no write path")}
- forbidden write paths: {", ".join(scope.write_deny) or "none beyond the allowed list"}
- anything changed outside the allowed paths is a scope violation, not a style issue
- not requested by this task (do not treat as defects on their own): refactors, extra
  features, documentation rewrites, dependency changes, wider test suites

## Candidate identity (HFlow froze this; do not rely on any hash quoted in prose)
{_candidate_lines(candidate or {"fingerprint": candidate_fingerprint})}
- diff to read: {_diff_reference(candidate)}{_round_change_lines(candidate)}
- review workspace: {workspace}

## Program evidence (produced by HFlow, not by the implementer)
- verification status: {verification_status}
- verification detail: {detail or "none recorded"}
- required check ids: {", ".join(dict.fromkeys(required_checks)) or "none"}
{_check_lines(list(dict.fromkeys(required_checks)), list(check_summaries or []))}
- evidence rows:
{_evidence_lines(list(evidence_rows or []))}
- read the recorded results yourself where a finding depends on them. Do not re-run the full
  suite and do not install or build anything; HFlow will re-run approved checks if a second
  run is genuinely needed.

## Review rules
- Judge the acceptance criteria first, then product risk. Report a defect that makes an
  acceptance criterion untrue, a scope violation, a regression risk in the changed code, or
  a missing piece of the task.
- Style, naming and formatting preferences are not blocking findings.
- Base every finding on the frozen candidate or the recorded evidence. If you cannot verify
  something, say so in a finding instead of assuming it is fine.
- If the checks pass but an acceptance criterion is still not met by the code, that is
  changes_requested.

## Output contract (the only accepted form)
{OUTPUT_CONTRACT_NOTE}

```json
{schema}
```

## Runtime
- deadline: {deadline_seconds} seconds
- your invocation is read-only; the write permission of the implementer does not extend to you
"""
    return _finish("reviewer", text)
