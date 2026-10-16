"""Read-only views over stored runs for one operator: ``hflow runs``, the inbox ``hflow status``
prints without a run id, and ``hflow diff`` (batch L; the inbox since batch N).

None calls a model, probes a process or writes anything.

* ``runs`` lists runs from the ledger, newest first, each with the first command ``hints``
  prints for it. Like ``status``, it reads the project contract file a run's admission recorded,
  and only to decide whether a printed ``integrate prepare`` needs ``--project``. Nothing is
  inferred from a run that cannot be read: such a run is named (in ``--json`` as an entry with an
  ``error``), the rest is still listed, and the command exits 6.
* The inbox (:func:`inbox`) keeps every run whose *stored* facts name something for the operator
  to do (:func:`attention`), judged with the clock and nothing else - no process is probed - and
  files it under one group (:func:`inbox_group`): ``running``, ``ready`` or ``attention``. Each
  entry prints one line of facts and exactly one ``next:`` command: the first one ``hints`` gives,
  or ``status <run>`` when it gives none. A run that cannot be read is named as for ``runs``.
* ``diff`` prints the Git change between a run's recorded base commit and its frozen candidate
  commit - two commits in the repository the run recorded, never a working tree. That is the
  candidate's Git identity, not its content fingerprint (AGENTS rule 11), and the header says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    INTEGRATION_ACTIVE_STATES,
    IntegrationState,
    ResultReceipt,
    TaskState,
)
from .hints import (
    RunFacts,
    hflow_command,
    integrable,
    recorded_repository,
    run_facts,
    steps_for,
    word,
)
from .profiles import summarize_validation_error
from .store import Store, StoredRecordUnreadable

#: What reading one run raises when one of its stored records no longer validates (``ValueError``
#: covers JSON that does not decode and a state name this build does not know).
UNREADABLE_ERRORS = (StoredRecordUnreadable, ValidationError, ValueError)


def unreadable_reason(exc: Exception) -> str:
    """One line naming what could not be read; ``status``, ``report``, ``runs`` and ``diff`` share
    it."""
    if isinstance(exc, ValidationError):
        return f"invalid {exc.title}: {summarize_validation_error(exc)}"
    if isinstance(exc, StoredRecordUnreadable):
        return str(exc).splitlines()[0]
    if isinstance(exc, json.JSONDecodeError):
        return f"not valid JSON: {exc}"
    return f"not valid: {exc}"


# --------------------------------------------------------------------------
# hflow runs
# --------------------------------------------------------------------------


def _integrable(facts: RunFacts) -> bool:
    """An accepted Git candidate: the only runs an integration is for (as ``plan_next`` says)."""
    return integrable(facts.task_state, facts.delivery_state, facts.git_candidate)


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


_LIVE_STATES = frozenset({TaskState.DRAFT, TaskState.READY, TaskState.RUNNING, TaskState.CHECKING})


def _past_deadline(facts: RunFacts, now: datetime) -> bool:
    """The clock is past the per-dispatch deadline a live run's admission recorded, counted from
    the last change its run row recorded; ``False`` when either was not recorded."""
    changed = _parse_time(facts.updated_at)
    return (
        facts.deadline_seconds is not None
        and changed is not None
        and now > changed + timedelta(seconds=facts.deadline_seconds)
    )


def _live_reasons(facts: RunFacts, now: datetime) -> list[str]:
    """A live run: its recorded owner, and whether the clock is past its recorded deadline.

    Stored facts and the clock only - no process is probed, so "past its deadline" is a
    likelihood, never a verdict. The deadline is the per-dispatch one the run's admission binding
    recorded, counted from the last change the run row recorded: no dispatch, check or state
    change since then for longer than that is what an interrupted controller (killed hard, say,
    during checks) leaves. ``resume`` decides, and refuses (exit 5) while the owner may be alive.
    """
    owner = (
        f"recorded owner pid {facts.owner_pid} since {facts.owner_since or 'an unrecorded time'}"
        if facts.owner_pid is not None
        else "no owner process recorded"
    )
    reasons = [f"live {facts.task_state.value}, {owner}"]
    if _past_deadline(facts, now):
        reasons.append(
            f"past its deadline: nothing recorded since {facts.updated_at}, longer than the "
            f"{facts.deadline_seconds}s per-dispatch deadline its admission recorded; probably "
            f"interrupted - `hflow resume {facts.run_id}` decides (exit 5 while the owner may be "
            "alive)"
        )
    return reasons


def attention(facts: RunFacts, *, now: datetime | None = None) -> list[str]:
    """Why a run needs its operator, from stored facts and the clock; empty when nothing does.

    A live run (``DRAFT``/``READY``/``RUNNING``/``CHECKING``: its recorded owner, and - when the
    clock is past the deadline its admission recorded - that it was probably interrupted), a
    blocked run (``outcome_unknown`` included: the run's block code says which), ledger entries
    still open or left ``unknown``/``launch_unknown``, an accepted Git candidate that no
    integration has taken onto a branch (none prepared, one ready to apply or to hand-merge, one
    a process may still be working on, or one that ended without integrating), and a cleanup
    that was interrupted. A released or kept worktree and an integrated candidate name nothing.
    """
    reasons: list[str] = []
    if facts.task_state in _LIVE_STATES:
        reasons += _live_reasons(facts, now or datetime.now(UTC))
    if facts.task_state is TaskState.BLOCKED:
        reasons.append(f"blocked: {facts.block_code or 'no block code recorded'}")
    if facts.open_entries:
        reasons.append("open ledger entries: " + ", ".join(facts.open_entries))
    if facts.settleable_entries:
        reasons.append(
            "unresolved ledger entries: "
            + ", ".join(f"{entry} ({state})" for entry, state in facts.settleable_entries)
        )
    if _integrable(facts) and not any(
        record.state is IntegrationState.INTEGRATED for record in facts.integrations
    ):
        active = [r for r in facts.integrations if r.state in INTEGRATION_ACTIVE_STATES]
        if active:
            reasons += [
                f"integration {r.integration_id} {r.state.value}: reconcile once its process "
                "has exited"
                for r in active
            ]
        elif not facts.integrations:
            reasons.append("accepted candidate not integrated")
        else:
            latest = facts.integrations[-1]
            if latest.state is IntegrationState.READY:
                reasons.append(
                    f"integration {latest.integration_id} ready: integrate applies it or hands "
                    "the merge over"
                )
            else:
                reasons.append(
                    f"integration {latest.integration_id} ended {latest.state.value}; the "
                    "candidate is not integrated"
                )
    if facts.worktree_state == "REMOVING":
        reasons.append("cleanup interrupted")
    return reasons


@dataclass(frozen=True)
class RunLine:
    """One listed run: its stored state and what the operator runs next."""

    run_id: str
    task_id: str
    task_state: str
    delivery_state: str
    block_code: str | None
    updated_at: str
    next: list[str] = field(default_factory=list)
    needs_you: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """``ACCEPTED/LOCAL_CANDIDATE``, ``BLOCKED/review_rejected``, ``RUNNING``."""
        if self.task_state == TaskState.ACCEPTED.value:
            return f"{self.task_state}/{self.delivery_state}"
        if self.block_code:
            return f"{self.task_state}/{self.block_code}"
        return self.task_state

    def as_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "task_state": self.task_state,
            "delivery_state": self.delivery_state,
            "block_code": self.block_code,
            "updated_at": self.updated_at,
            "needs_you": list(self.needs_you),
            "next": list(self.next),
        }


@dataclass(frozen=True)
class Unreadable:
    """A run whose stored records could not be read: named, never guessed about."""

    run_id: str
    reason: str

    def as_json(self) -> dict[str, object]:
        """``runs --json``'s entry for it: the id and the error, and nothing guessed."""
        return {"run_id": self.run_id, "error": f"stored record unreadable: {self.reason}"}


def _line(
    store: Store,
    row,  # noqa: ANN001 - a runs row
    facts: RunFacts,
    reasons: list[str],
) -> RunLine:
    """One run's :class:`RunLine`; raises one of :data:`UNREADABLE_ERRORS` for a record that no
    longer validates."""
    return RunLine(
        run_id=facts.run_id,
        task_id=str(row["task_id"]),
        task_state=facts.task_state.value,
        delivery_state=facts.delivery_state,
        block_code=facts.block_code,
        updated_at=str(row["updated_at"]),
        next=steps_for(store, facts),
        needs_you=reasons,
    )


def list_runs(store: Store, *, limit: int) -> tuple[list[RunLine], list[Unreadable]]:
    """The newest ``limit`` runs; every one looked at is either listed or named as unreadable."""
    lines: list[RunLine] = []
    unreadable: list[Unreadable] = []
    for row in store.list_runs(limit=limit):
        try:
            facts = run_facts(store, str(row["run_id"]))
            lines.append(_line(store, row, facts, attention(facts)))
        except UNREADABLE_ERRORS as exc:
            unreadable.append(Unreadable(str(row["run_id"]), unreadable_reason(exc)))
    return lines, unreadable


def render_runs(lines: list[RunLine], *, unreadable: bool = False) -> str:
    """A text table: run, task, state, updated, and the first next command."""
    if not lines:
        return "no readable run" if unreadable else "no runs recorded"
    header = ("RUN", "TASK", "STATE", "UPDATED", "NEXT")
    rows = [
        (
            line.run_id, line.task_id, line.outcome, line.updated_at,
            line.next[0] if line.next else "-",
        )
        for line in lines
    ]
    widths = [max(len(row[index]) for row in (header, *rows)) for index in range(4)]
    return "\n".join(
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[4]]).rstrip()
        for row in (header, *rows)
    )


# --------------------------------------------------------------------------
# hflow status (no run id): the inbox
# --------------------------------------------------------------------------

#: The inbox's groups, in the order it prints them.
INBOX_GROUPS: tuple[str, ...] = ("running", "ready", "attention")
_GROUP_TITLES = {"running": "Running", "ready": "Ready", "attention": "Attention"}
#: An accepted candidate's newest integration in one of these: its next command moves it on as
#: usual - apply a ready one (or merge it by hand), prepare again once the target moved on
#: (``stale``) or another integration took its place (``superseded``).
_ROUTINE_INTEGRATIONS = frozenset(
    {IntegrationState.READY, IntegrationState.STALE, IntegrationState.SUPERSEDED}
)
#: What the inbox prints for a run the hints name no command for: its detailed view.
_NOTHING_TO_RUN = "nothing left for HFlow to run; read what it recorded"


def inbox_group(facts: RunFacts, *, now: datetime | None = None) -> str:
    """The inbox group of a run :func:`attention` names, from the same stored facts and clock.

    ``running``: a live run whose recorded deadline the clock has not passed (or that recorded
    none), and an accepted candidate an integration process may still be working on - looking
    again later is the usual move. ``ready``: an accepted Git candidate with nothing else in the
    way, whose next command takes it towards a branch - no integration yet, one ready to apply or
    to merge by hand, or one that went stale or was superseded and is prepared again.
    ``attention``: the rest - a live run past its deadline (probably interrupted), a blocked run
    (``outcome_unknown`` included), ledger entries left open or unresolved, an integration that
    ended ``conflict``/``checks_failed``/``interrupted``/``failed``, an interrupted cleanup.
    """
    if facts.task_state in _LIVE_STATES:
        return "attention" if _past_deadline(facts, now or datetime.now(UTC)) else "running"
    if (
        facts.task_state is TaskState.BLOCKED
        or facts.open_entries
        or facts.settleable_entries
        or facts.worktree_state == "REMOVING"
    ):
        return "attention"
    if any(record.state in INTEGRATION_ACTIVE_STATES for record in facts.integrations):
        return "running"
    if not facts.integrations or facts.integrations[-1].state in _ROUTINE_INTEGRATIONS:
        return "ready"
    return "attention"


@dataclass(frozen=True)
class InboxEntry:
    """One run in the inbox: its group, its listing line, and the one command printed for it."""

    group: str
    line: RunLine
    #: The one ``next:`` command the text prints: the first hint, else the run's ``status``.
    next: str

    def as_json(self) -> dict[str, object]:
        """``runs --json``'s entry for the run, plus its group."""
        return {**self.line.as_json(), "group": self.group}


def inbox(
    store: Store, *, now: datetime | None = None
) -> tuple[list[InboxEntry], list[Unreadable]]:
    """Every run whose stored facts name something for the operator, in :data:`INBOX_GROUPS`
    order and newest first within a group.

    Every run in the ledger is looked at, so a run that cannot be read is named, never silently
    left out. Pure ledger reads and the clock (plus what :func:`steps_for` reads); nothing probed.
    """
    moment = now or datetime.now(UTC)
    entries: list[InboxEntry] = []
    unreadable: list[Unreadable] = []
    # SQLite reads a negative LIMIT as "no limit".
    for row in store.list_runs(limit=-1):
        run_id = str(row["run_id"])
        try:
            facts = run_facts(store, run_id)
            reasons = attention(facts, now=moment)
            if not reasons:
                continue
            line = _line(store, row, facts, reasons)
        except UNREADABLE_ERRORS as exc:
            unreadable.append(Unreadable(run_id, unreadable_reason(exc)))
            continue
        first = (
            line.next[0]
            if line.next
            else f"{hflow_command(store, 'status', word(run_id))}  # {_NOTHING_TO_RUN}"
        )
        entries.append(InboxEntry(inbox_group(facts, now=moment), line, first))
    entries.sort(key=lambda entry: INBOX_GROUPS.index(entry.group))  # stable: newest first stays
    return entries, unreadable


def inbox_json(entries: list[InboxEntry], unreadable: list[Unreadable]) -> list[dict[str, object]]:
    """``status --json`` without a run id: each entry, then each unreadable run (``attention``)."""
    return [entry.as_json() for entry in entries] + [
        {**item.as_json(), "group": "attention"} for item in unreadable
    ]


def render_inbox(entries: list[InboxEntry], *, unreadable: bool = False) -> str:
    """Each group with entries: its title and count, then per run one line of facts (run, task,
    state, why it needs you) and one ``next:`` line. One line when nothing needs the operator."""
    if not entries:
        return "nothing readable needs you" if unreadable else "nothing needs you"
    cells = [(entry.line.run_id, entry.line.task_id, entry.line.outcome) for entry in entries]
    widths = [max(len(row[index]) for row in cells) for index in range(3)]
    out: list[str] = []
    for group in INBOX_GROUPS:
        members = [(entry, row) for entry, row in zip(entries, cells) if entry.group == group]
        if not members:
            continue
        out.append(f"{_GROUP_TITLES[group]} ({len(members)})")
        for entry, row in members:
            columns = "  ".join(cell.ljust(width) for cell, width in zip(row, widths))
            out.append(f"  {columns}  {'; '.join(entry.line.needs_you)}")
            out.append(f"    next: {entry.next}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# hflow diff
# --------------------------------------------------------------------------


class NoCandidate(Exception):
    """The run recorded no frozen Git candidate to diff."""


@dataclass(frozen=True)
class CandidateRange:
    """The two commits a run recorded for its candidate, and where they live."""

    run_id: str
    base_commit: str
    candidate_commit: str
    #: Where the commits came from: the accepted receipt, or the newest frozen candidate.
    source: str
    #: The candidate's content fingerprint when the receipt names one ("" otherwise).
    fingerprint: str
    #: ``git rev-parse --git-common-dir`` of the run's repository, when recorded.
    git_dir: str
    #: The project root the run recorded (used only when no Git directory was recorded).
    project_root: str


def candidate_range(store: Store, run_id: str) -> CandidateRange:
    """The base and frozen candidate commits ``run_id`` recorded.

    The accepted receipt's ``candidate`` when it names a Git commit; otherwise the newest frozen
    candidate the run recorded (its DSH context record, written right after the candidate ref),
    which is how a candidate the run did not accept - a rejected review, a failed check - is
    found. Raises :class:`RunNotFound`, :class:`NoCandidate`, or one of
    :data:`UNREADABLE_ERRORS` for a stored record that no longer validates.
    """
    from .gitworkspace import is_full_oid

    row = store.get_run(run_id)
    run_id = str(row["run_id"])
    state = str(row["task_state"])
    base = candidate = fingerprint = ""
    source = ""
    if row["receipt_json"]:
        receipt = ResultReceipt.model_validate_json(row["receipt_json"])
        if receipt.candidate.git_commit:
            base, candidate = receipt.candidate.base_commit, receipt.candidate.git_commit
            fingerprint = receipt.candidate.fingerprint
            source = f"the {receipt.task_state.value} receipt of attempt {receipt.attempt_id}"
        else:
            raise NoCandidate(
                f"run {run_id} was accepted without a Git candidate (an in-place run); its "
                f"identity is its content fingerprint {receipt.candidate.fingerprint or '(none)'}"
            )
    else:
        frozen = store.dsh_context_for(run_id)
        if frozen:
            newest = frozen[-1]
            base, candidate = newest.base_commit, newest.candidate_commit
            source = (
                f"the newest frozen candidate (attempt {newest.attempt_id}, round {newest.round}); "
                f"the run is {state} and did not accept it"
            )
    if not candidate:
        raise NoCandidate(
            f"run {run_id} ({state}) recorded no frozen Git candidate: nothing was frozen, or it "
            "ran in place"
        )
    for label, value in (("base", base), ("candidate", candidate)):
        if not is_full_oid(value):
            raise NoCandidate(
                f"run {run_id} recorded {label} {value!r}, which is not a Git commit id"
            )
    git_dir, project_root = recorded_repository(store, run_id)
    return CandidateRange(
        run_id=run_id,
        base_commit=base,
        candidate_commit=candidate,
        source=source,
        fingerprint=fingerprint,
        git_dir=git_dir,
        project_root=project_root,
    )


def diff_header(rng: CandidateRange) -> list[str]:
    fingerprint = f" ({rng.fingerprint})" if rng.fingerprint else ""
    return [
        f"run {rng.run_id}: git diff {rng.base_commit}..{rng.candidate_commit} ({rng.source})",
        "note: these commits are the candidate's Git identity, not its content fingerprint"
        f"{fingerprint}; verification and review bind to the fingerprint over the task's write "
        "scope, never to a commit id",
    ]


def diff_argv(rng: CandidateRange, *, stat: bool) -> tuple[list[str], Path]:
    """The ``git`` argv and working directory for the diff; never a working-tree diff.

    Two commits, with ``--`` after the range so nothing after it can be read as a revision or
    an option (a long Windows working directory made Git read an argument as both). No rename
    detection, external diff program, textconv filter or colour, whatever the user's
    configuration says.
    """
    options = ["--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"]
    if stat:
        options.append("--stat")
    words = ["diff", *options, f"{rng.base_commit}..{rng.candidate_commit}", "--"]
    if rng.git_dir and Path(rng.git_dir).is_dir():
        return ["git", "--git-dir", rng.git_dir, *words], Path(rng.git_dir)
    if rng.project_root and Path(rng.project_root).is_dir():
        return ["git", *words], Path(rng.project_root)
    raise NoCandidate(
        f"run {rng.run_id}'s repository is gone: recorded Git directory "
        f"{rng.git_dir or '(none)'}, project root {rng.project_root or '(none)'}"
    )


def run_diff(rng: CandidateRange, *, stat: bool, timeout: float = 300.0) -> tuple[int, bytes, str]:
    """Run the diff through HFlow's one git runner: ``(returncode, stdout bytes, stderr)``.

    Git's output is passed through as bytes, never decoded. Git that cannot start or times out
    raises :class:`~hflow.gitworkspace.GitError`.
    """
    from .gitworkspace import git_process

    argv, cwd = diff_argv(rng, stat=stat)
    completed = git_process(argv[1:], cwd=cwd, timeout=timeout)
    return completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace")

