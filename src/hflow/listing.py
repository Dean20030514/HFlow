"""Read-only views over stored runs for one operator: ``hflow runs`` and ``hflow diff`` (batch L).

Neither calls a model, probes a process or writes anything.

* ``runs`` lists runs from the ledger, newest first, each with the first command ``hints``
  prints for it. Like ``status``, it reads the project contract file a run's admission recorded,
  and only to decide whether a printed ``integrate prepare`` needs ``--project``. ``--needs-you``
  keeps the runs whose *stored* facts name something for the operator to do (:func:`attention`);
  there is no default attention, and nothing is inferred from a run that cannot be read: such a
  run is named, the rest is still listed, and the command exits 6.
* ``diff`` prints the Git change between a run's recorded base commit and its frozen candidate
  commit - two commits in the repository the run recorded, never a working tree. That is the
  candidate's Git identity, not its content fingerprint (AGENTS rule 11), and the header says so.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    INTEGRATION_ACTIVE_STATES,
    DeliveryState,
    IntegrationState,
    ResultReceipt,
    TaskState,
)
from .hints import RunFacts, named_data_dir, plan_next, run_facts
from .profiles import summarize_validation_error
from .store import Store, StoredRecordUnreadable

#: What reading one run raises when one of its stored records no longer validates (``ValueError``
#: covers JSON that does not decode and a state name this build does not know).
UNREADABLE_ERRORS = (StoredRecordUnreadable, ValidationError, ValueError)


def unreadable_reason(exc: Exception) -> str:
    """One line naming what could not be read, as ``status`` words it."""
    if isinstance(exc, ValidationError):
        return f"invalid {exc.title}: {summarize_validation_error(exc)}"
    if isinstance(exc, StoredRecordUnreadable):
        return str(exc).splitlines()[0]
    return f"not valid: {exc}"


# --------------------------------------------------------------------------
# hflow runs
# --------------------------------------------------------------------------


def _integrable(facts: RunFacts) -> bool:
    """An accepted Git candidate: the only runs an integration is for (as ``plan_next`` says)."""
    return (
        facts.task_state is TaskState.ACCEPTED
        and facts.delivery_state == DeliveryState.LOCAL_CANDIDATE.value
        and facts.git_candidate
    )


def attention(facts: RunFacts) -> list[str]:
    """Why a run needs its operator, from explicit stored facts only; empty when nothing does.

    A blocked run (``outcome_unknown`` included: the run's block code says which), ledger entries
    still open or left ``unknown``/``launch_unknown``, an accepted Git candidate that no
    integration has taken onto a branch (none prepared, one ready to apply or to hand-merge, one
    a process may still be working on, or one that ended without integrating), and a cleanup
    that was interrupted. A live run, a released or kept worktree and an integrated candidate
    name nothing.
    """
    reasons: list[str] = []
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
                    f"integration {latest.integration_id} ready: apply it, or merge it yourself "
                    "and reconcile"
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


def _next_for(store: Store, facts: RunFacts) -> list[str]:
    """``hints.plan_next`` exactly as ``status`` prints it for this run."""
    project_default = False
    if _integrable(facts):
        from .integrate import recorded_project_file  # integrate imports hints

        project_default = recorded_project_file(store, facts.run_id) is not None
    return plan_next(facts, data_dir=named_data_dir(store), project_default=project_default)


def list_runs(
    store: Store, *, limit: int, needs_you: bool = False
) -> tuple[list[RunLine], list[Unreadable]]:
    """The newest ``limit`` runs (with ``needs_you``: the newest ``limit`` that need the operator).

    Every run looked at is either listed or named as unreadable; with ``needs_you`` every run in
    the ledger is looked at, so a run that cannot be read is never silently left out.
    """
    # SQLite reads a negative LIMIT as "no limit".
    rows = store.list_runs(limit=-1 if needs_you else limit)
    lines: list[RunLine] = []
    unreadable: list[Unreadable] = []
    for row in rows:
        run_id = str(row["run_id"])
        try:
            facts = run_facts(store, run_id)
            reasons = attention(facts)
            if needs_you and (not reasons or len(lines) >= limit):
                continue
            steps = _next_for(store, facts)
        except UNREADABLE_ERRORS as exc:
            unreadable.append(Unreadable(run_id, unreadable_reason(exc)))
            continue
        lines.append(
            RunLine(
                run_id=run_id,
                task_id=str(row["task_id"]),
                task_state=facts.task_state.value,
                delivery_state=facts.delivery_state,
                block_code=facts.block_code,
                updated_at=str(row["updated_at"]),
                next=steps,
                needs_you=reasons,
            )
        )
    return lines, unreadable


def render_runs(lines: list[RunLine], *, needs_you: bool, unreadable: bool = False) -> str:
    """A text table: run, task, state, updated, and the first next command."""
    if not lines:
        if needs_you:
            return "no readable run needs you" if unreadable else "no run needs you"
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
    out: list[str] = []
    for row, line in zip((header, *rows), (None, *lines)):
        out.append("  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[4]]))
        if needs_you and line is not None:
            out.append("  needs you: " + "; ".join(line.needs_you))
    return "\n".join(text.rstrip() for text in out)


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


def _repository(store: Store, run_id: str) -> tuple[str, str]:
    """``(git_common_dir, project_root)`` from the run's admission record or worktree record."""
    binding = store.admission_binding_for(run_id)
    if binding is not None and binding.git_common_dir:
        return binding.git_common_dir, binding.project_root
    provenance = store.workspace_provenance_for(run_id)
    if provenance is not None:
        return provenance.git_common_dir, provenance.project_root
    return "", binding.project_root if binding is not None else ""


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
    git_dir, project_root = _repository(store, run_id)
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
    """Run the diff in HFlow's inert Git environment: ``(returncode, stdout bytes, stderr)``."""
    from .gitworkspace import _base_env  # the one hardened Git environment HFlow uses

    argv, cwd = diff_argv(rng, stat=stat)
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=str(cwd),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        env=_base_env(),
    )
    return completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace")

