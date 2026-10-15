"""Guarded workspace cleanup: preview by default, remove only when it is provably safe.

`clean` releases the *working directory* of a run. It never deletes the delivery: the
candidate commit stays reachable through an HFlow-owned ref, and the receipt and evidence
live in the controller's own data directory, outside the worktree.

The guards exist because deleting a directory is the one operation here that cannot be
undone by re-running something:

1. the target must be the linked worktree this run created, registered with the same Git
   common directory - not the source repository, not another run, not a path that merely
   looks right;
2. no managed execution may be active or unconfirmed (`BLOCKED` is a state name, not proof
   that a process stopped);
3. HEAD must still be the frozen candidate, with no unfrozen changes (for a run without a
   receipt: the latest attempt's recorded frozen candidate, or the run's base commit);
4. the candidate must be reachable through a ref that outlives the worktree. A run without a
   receipt whose HEAD is not its base needs a branch, tag or HFlow ref that contains HEAD;
   otherwise the commit would be dropped, and cleanup refuses ``unretained_commit``;
5. untracked and ignored files are refused unless they match the explicit artifact policy -
   an ignored `.env` is a user's file, not garbage.

Anything unclear refuses and keeps the scene, which is the correct outcome.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    AttemptState, RefusedError, ResultReceipt, TaskSpec, TaskState,
    WorkspaceProvenance,
)
from .gitworkspace import (
    IGNORED_ARTIFACT_ALLOWLIST,
    GitError,
    GitRepo,
    StatusReport,
    brief_list,
    is_full_oid,
    registered_worktrees,
)
from .hints import hflow_command, shell_word
from .store import Store, StoreError
from .workspace import matches_pattern

#: Workspace lifecycle, kept separate from the business task state.
WORKSPACE_PRESENT = "PRESENT"
WORKSPACE_REMOVED = "REMOVED"
WORKSPACE_MISSING = "MISSING"


@dataclass
class CleanPlan:
    """What `clean` would do, and why it is or is not allowed. Read-only by construction."""

    run_id: str
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    refusals: list[dict[str, str]] = field(default_factory=list)
    path: str = ""
    common_dir: str = ""
    head: str = ""
    expected_candidate: str = ""
    candidate_ref: str = ""
    candidate_ref_target: str = ""
    registered: bool = False
    workspace_state: str = ""
    task_state: str = ""
    tracked_changes: list[str] = field(default_factory=list)
    ignored_paths: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    keeps: list[str] = field(default_factory=list)
    already_removed: bool = False
    receipt_present: bool = False
    #: The refs (``refs/heads``, ``refs/tags``, ``refs/hflow``) that contain HEAD, when that was
    #: what allowed a run without a receipt to be cleaned. Empty when it was not checked.
    retained_by: list[str] = field(default_factory=list)
    provenance: WorkspaceProvenance | None = None
    root_repository: str | None = None
    #: The recorded source repository, opened from the user's checkout (or a legacy run's root
    #: repository): ``git worktree remove`` runs there, never inside the worktree being removed and
    #: never from the main worktree, which is the git directory itself for a bare repository.
    source: GitRepo | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "allowed": self.allowed,
            "reasons": self.reasons,
            "refusals": self.refusals,
            "path": self.path,
            "git_common_dir": self.common_dir,
            "head": self.head,
            "expected_candidate": self.expected_candidate,
            "candidate_ref": self.candidate_ref,
            "candidate_ref_target": self.candidate_ref_target,
            "registered_worktree": self.registered,
            "workspace_state": self.workspace_state,
            "task_state": self.task_state,
            "tracked_changes": self.tracked_changes,
            "ignored_paths": self.ignored_paths,
            "unsupported_status": self.unsupported,
            "keeps": self.keeps,
            "already_removed": self.already_removed,
            "receipt_present": self.receipt_present,
            "retained_by": self.retained_by,
        }


def _refuse(plan: CleanPlan, code: str, detail: str) -> None:
    plan.allowed = False
    plan.refusals.append({"reason": code, "detail": detail})


def _inspect(worktree: str, *commands: str) -> str:
    """Read-only git commands, pasteable, that show what a refusal is about. Never a removal."""
    where = shell_word(worktree)
    return "; inspect (read-only): " + "; ".join(f"git -C {where} {command}" for command in commands)


#: The note the controller writes when it resolves the task's base name to a commit.
_BASE_NOTE = re.compile(r"base .+ -> (?P<commit>[0-9a-f]{40}(?:[0-9a-f]{24})?)")
#: The namespaces whose refs count as keeping a commit: a branch, a tag, or an HFlow ref. A
#: remote-tracking ref is not one - a fetch may prune it - and neither is a worktree's own HEAD.
_RETAINING_NAMESPACES = ("refs/heads", "refs/tags", "refs/hflow")
#: What shows a moved HEAD: the commits it now names, and anything uncommitted on top.
_HEAD_DRIFT_INSPECT = ("log --oneline -5", "status --short --ignored")


def _recorded_base_commit(store: Store, run_id: str, spec: TaskSpec) -> str:
    """The commit the run's worktree was created at, from stored facts only, or ``""``.

    The controller records ``base '<name>' -> <commit>`` when the base name differs from the
    commit it resolved to; when it does not, the task's own ``base_commit`` already is that full
    commit. A frozen candidate's DSH context record names the same base. Nothing is read from the
    worktree, which the worker controlled. ``""`` (unknown) only ever makes cleanup stricter.
    """
    for note in store.notes_for(run_id):
        match = _BASE_NOTE.fullmatch(note)
        if match is not None:
            return match.group("commit")
    try:
        for record in store.dsh_context_for(run_id):
            if record.base_commit:
                return record.base_commit
    except StoreError:
        pass  # unreadable is "not recorded", which keeps the stricter gate
    named = spec.workspace.base_commit
    return named if is_full_oid(named) else ""


def _recorded_frozen_commit(store: Store, run_id: str, attempt_id: str) -> str:
    """The candidate commit ``attempt_id`` froze and kept a candidate ref for, or ``""``.

    Read from the DSH context record the controller writes right after the candidate ref. A commit
    made but refused after the freeze has no record and no ref, so it is not "expected": such a
    HEAD has to be retained by some ref, or cleanup refuses ``unretained_commit``.
    """
    try:
        records = store.dsh_context_for(run_id)
    except StoreError:
        return ""
    frozen = [record.candidate_commit for record in records if record.attempt_id == attempt_id]
    return frozen[-1] if frozen else ""


def _rootless_source(store: Store, run_id: str) -> tuple[str, GitRepo] | None:
    """``(repository path, the repository it opens)`` for a run recorded without provenance.

    Such a (legacy) run names its repository only on its root budget; ``None`` when it has none.
    The repository is what that path resolves to now.
    """
    root = store.root_budget_for_run(run_id)
    if root is None:
        return None
    path = str(root["repo_path"])
    return path, GitRepo.discover(Path(path))


def _read_candidate_ref(plan: CleanPlan, repo: GitRepo, run_id: str, attempt_id: str) -> bool:
    """Record the attempt's candidate ref and its target; ``False`` (refused) when unreadable."""
    plan.candidate_ref = repo.candidate_ref(run_id, attempt_id)
    try:
        plan.candidate_ref_target = repo.ref_target(plan.candidate_ref) or ""
    except GitError as exc:
        _refuse(plan, "git_query_failed", f"could not read {plan.candidate_ref}: {exc}")
        return False
    return True


def _refs_containing(repo: GitRepo, commit: str) -> list[str]:
    """Every branch, tag or HFlow ref whose history contains ``commit``."""
    out = repo.run(
        "for-each-ref", "--contains", commit, "--format=%(refname)", *_RETAINING_NAMESPACES
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _check_head_retained(plan: CleanPlan, repo: GitRepo, base_commit: str) -> None:
    """For a run without a receipt: allow only if removing the worktree drops no commit.

    That holds when HEAD is still the run's base (nothing was committed), or when a ref in
    :data:`_RETAINING_NAMESPACES` contains HEAD. Anything else refuses ``unretained_commit``: the
    commit is the only trace of what the worker did, and the operator decides whether to keep it.
    """
    head = plan.head
    if base_commit and head == base_commit:
        plan.reasons.append(
            f"no receipt: HEAD is still the run's base commit {head[:12]}, so nothing was "
            "committed in the worktree and removing it drops no commit"
        )
        return
    if plan.candidate_ref and plan.candidate_ref_target == head:
        plan.retained_by = [plan.candidate_ref]
        plan.reasons.append(
            f"no receipt: this run delivered nothing; HEAD {head[:12]} is the latest attempt's "
            f"frozen candidate and {plan.candidate_ref} points at it, so the commit stays "
            "reachable after the worktree is removed"
        )
        return
    try:
        refs = _refs_containing(repo, head)
    except GitError as exc:
        _refuse(plan, "git_query_failed", f"could not list the refs that contain HEAD: {exc}")
        return
    if refs:
        plan.retained_by = refs
        plan.reasons.append(
            f"no receipt: HEAD {head[:12]} is not the run's base, but {brief_list(refs, 3)} "
            "contains it, so the commit stays reachable after the worktree is removed"
        )
        return
    _refuse(
        plan,
        "unretained_commit",
        f"HEAD {head} is not the run's base commit ({base_commit[:12] or 'not recorded'}) and no "
        "branch, tag or HFlow ref contains it, so removing the worktree would leave that commit "
        f"unreferenced. Inspect it (git -C {shell_word(plan.path)} show {head[:12]}); to keep it, "
        f"create a ref (for example git branch <name> {head}), then run clean again",
    )


def plan_cleanup(store: Store, run_id: str) -> CleanPlan:
    """Inspect a run's workspace and decide whether removal is allowed. Changes nothing."""
    row = store.get_run(run_id)
    plan = CleanPlan(run_id=run_id, allowed=True)
    plan.workspace_state = str(row["worktree_state"])
    plan.task_state = str(row["task_state"])
    if plan.task_state not in {
        TaskState.ACCEPTED.value, TaskState.BLOCKED.value, TaskState.CANCELLED.value,
    }:
        _refuse(
            plan, "run_in_flight",
            f"the run is {plan.task_state}; only a terminal run's workspace may be released",
        )
    receipt = (
        ResultReceipt.model_validate(json.loads(row["receipt_json"]))
        if row["receipt_json"]
        else None
    )
    plan.receipt_present = receipt is not None
    # Only facts: the candidate commit and its ref are added once a ref was resolved (below).
    plan.keeps = [
        "the receipt and its evidence, stored outside the worktree"
        if receipt is not None
        else "the run's evidence, stored outside the worktree (this run has no receipt)",
        "the run's history, notes and any cleanup record",
    ]
    spec = TaskSpec.model_validate(json.loads(row["task_spec_json"]))
    recorded_path = row["worktree_path"] or (receipt.candidate.worktree if receipt else "")

    if str(row["worktree_state"]) == WORKSPACE_REMOVED:
        plan.allowed = False
        plan.already_removed = True
        plan.reasons.append("this run's workspace was already removed and recorded as such")
        return plan

    if spec.workspace.mode != "worktree" or not recorded_path:
        _refuse(plan, "no_managed_workspace", "this run did not use a managed Git worktree")
        return plan

    worktree = Path(recorded_path)
    plan.path = str(worktree)
    source_common = _open_recorded_source(plan, store, worktree)
    if source_common is None:
        return plan
    repo = _open_owned_worktree(plan, worktree, source_common)
    if repo is None:
        return plan
    attempt = _check_execution_state(plan, store, run_id, str(row["task_state"]))
    base_commit = _check_head(plan, store, repo, worktree, receipt=receipt, spec=spec, attempt=attempt)
    if base_commit is None:
        return plan
    try:
        status = repo.status_report(worktree)
        flagged = repo.index_flagged_paths(worktree)
    except Exception as exc:  # noqa: BLE001 - a failed read must refuse, not guess
        _refuse(plan, "status_failed", repr(exc))
        return plan
    _check_status(plan, status, flagged)
    _check_retention(plan, repo, receipt, base_commit)
    _check_ignored(plan, status)
    if plan.allowed and not plan.reasons:
        plan.reasons.append("the worktree is registered, clean, at the frozen candidate, and its ref resolves")
    return plan


def _open_recorded_source(plan: CleanPlan, store: Store, worktree: Path) -> str | None:
    """The recorded source repository (``plan.source``) and its common directory, or ``None``.

    From the run's workspace provenance, else a legacy run's root ledger repository; never from
    notes or receipt paths. ``None`` when that cannot be established - the plan then refuses.
    """
    try:
        plan.provenance = store.workspace_provenance_for(plan.run_id)
    except StoreError as exc:
        _refuse(plan, "provenance_unreadable", str(exc))
        return None
    if plan.provenance is not None and (
        str(worktree) != plan.provenance.worktree_path
        or worktree.resolve() != Path(plan.provenance.worktree_path)
    ):
        _refuse(plan, "provenance_mismatch", "the workspace path differs from its recorded source")
        return None
    try:
        if plan.provenance is not None:
            plan.source = GitRepo.discover(Path(plan.provenance.project_root))
            if plan.source.absolute_common_dir != plan.provenance.git_common_dir:
                _refuse(plan, "provenance_mismatch", "recorded repository now has a different Git common directory")
                return None
        else:
            source = _rootless_source(store, plan.run_id)
            if source is None:
                _refuse(plan, "workspace_provenance_missing", "legacy rootless run has no reliable recorded repository source")
                return None
            plan.root_repository, plan.source = source
        return plan.source.absolute_common_dir
    except (GitError, RefusedError, OSError) as exc:
        _refuse(plan, "provenance_unreadable", f"recorded repository source cannot be checked: {exc}")
        return None


def _open_owned_worktree(plan: CleanPlan, worktree: Path, source_common: str) -> GitRepo | None:
    """1. Ownership: the real registration, not a string comparison. ``None`` when refused."""
    if not worktree.exists():
        plan.allowed = False
        plan.reasons.append(
            "the recorded worktree path no longer exists and no removal was recorded: "
            "reporting MISSING rather than claiming a successful cleanup"
        )
        plan.workspace_state = WORKSPACE_MISSING
        return None
    try:
        repo = GitRepo.discover(worktree)
    except RefusedError as exc:
        _refuse(plan, "not_a_worktree", str(exc))
        return None
    except GitError as exc:
        _refuse(plan, "git_query_failed", str(exc))
        return None
    plan.common_dir = repo.absolute_common_dir
    if plan.common_dir != source_common:
        _refuse(plan, "provenance_mismatch", "Git common directory differs from its recorded source")
        return None
    try:
        registration = repo.worktree_registration(worktree)
        is_main = registration is not None and repo.is_main_worktree(worktree)
    except GitError as exc:
        _refuse(plan, "git_query_failed", str(exc))
        return None
    if registration is None:
        _refuse(plan, "not_registered", "git does not have this path registered as a worktree")
        return None
    plan.registered = True
    if is_main:
        _refuse(
            plan,
            "is_source_repository",
            "this path is the repository's main worktree, not a run worktree",
        )
        return None
    return repo


def _check_execution_state(
    plan: CleanPlan, store: Store, run_id: str, task_state: str
) -> sqlite3.Row | None:
    """2. Execution state: a name is not proof that a process stopped. Returns the open attempt."""
    attempt = store.open_attempt(run_id)
    if attempt is not None and str(attempt["state"]) in {
        AttemptState.ACTIVE.value,
        AttemptState.CREATED.value,
    }:
        _refuse(
            plan,
            "execution_active",
            f"attempt {attempt['attempt_id']} is {attempt['state']}; a stop must be confirmed first",
        )
    intent_at, cancel_receipt = store.cancel_state(run_id)
    # A stop that reached a run which had already ended decided nothing about it, and an
    # ``unknown`` answer then comes from a process that holds no handle (``hflow cancel`` runs in
    # its own process). Such a receipt neither confirms nor refutes anything, so the run keeps
    # exactly the gates it had before the stop - an attempt still live, a run in flight. A stop
    # that decided a *live* run, an answer that saw the work ``still_running``, and an intent
    # with no receipt at all keep refusing.
    stop_of_ended_run = (
        cancel_receipt is not None
        and cancel_receipt.run_already_ended
        and cancel_receipt.status == "unknown"
    )
    if intent_at and (
        cancel_receipt is None
        or (cancel_receipt.status != "confirmed_stopped" and not stop_of_ended_run)
    ):
        _refuse(
            plan,
            "stop_unconfirmed",
            "a cancellation was requested but never confirmed; the workspace must be kept",
        )
    elif intent_at and stop_of_ended_run:
        plan.reasons.append(
            "a stop was requested after this run had already ended and could not be confirmed "
            "from the stopping process; it decided nothing about the run, so cleanup applies the "
            "same gates as before the stop"
        )
    if task_state in {TaskState.RUNNING.value, TaskState.CHECKING.value}:
        _refuse(
            plan,
            "run_in_flight",
            f"the run is {task_state}; only a settled run's workspace may be released",
        )
    return attempt


def _check_head(
    plan: CleanPlan,
    store: Store,
    repo: GitRepo,
    worktree: Path,
    *,
    receipt: ResultReceipt | None,
    spec: TaskSpec,
    attempt: sqlite3.Row | None,
) -> str | None:
    """3. HEAD must still be the frozen candidate. Returns the run's recorded base commit (``""``
    when there is a receipt or none was recorded), or ``None`` when a read failed and planning
    stops there."""
    run_id = plan.run_id
    try:
        plan.head = repo.worktree_commit(worktree)
    except GitError as exc:
        _refuse(plan, "git_query_failed", f"could not read the worktree's HEAD: {exc}")
        return None
    base_commit = ""
    if receipt is not None:
        plan.expected_candidate = receipt.candidate.git_commit
        if not _read_candidate_ref(plan, repo, run_id, receipt.attempt_id):
            return None
        if plan.expected_candidate and plan.head != plan.expected_candidate:
            _refuse(
                plan,
                "head_drift",
                f"HEAD is {plan.head[:12]} but the frozen candidate is "
                f"{plan.expected_candidate[:12]}" + _inspect(plan.path, *_HEAD_DRIFT_INSPECT),
            )
    else:
        # No receipt: the expected HEAD comes from stored facts - the latest attempt's frozen
        # candidate when one was recorded, and the run's base commit (nothing committed yet).
        base_commit = _recorded_base_commit(store, run_id, spec)
        if attempt is not None:
            frozen = _recorded_frozen_commit(store, run_id, str(attempt["attempt_id"]))
            if frozen:
                plan.expected_candidate = frozen
                if not _read_candidate_ref(plan, repo, run_id, str(attempt["attempt_id"])):
                    return None
        if plan.expected_candidate and plan.head not in {plan.expected_candidate, base_commit}:
            _refuse(
                plan,
                "head_drift",
                f"HEAD is {plan.head[:12]} but the latest attempt's frozen candidate is "
                f"{plan.expected_candidate[:12]} and the run's base is "
                f"{base_commit[:12] or 'not recorded'}" + _inspect(plan.path, *_HEAD_DRIFT_INSPECT),
            )
    return base_commit


def _check_status(plan: CleanPlan, status: StatusReport, flagged: list[str]) -> None:
    """3, continued. Nothing unfrozen: no change, no flag that hides one, nothing uninterpretable."""
    plan.tracked_changes = list(status.changed)
    plan.ignored_paths = list(status.ignored)
    plan.unsupported = list(status.unsupported)
    if flagged:
        _refuse(
            plan, "index_flags_hide_changes",
            "Git index flags may hide uncommitted worktree bytes (assume-unchanged or "
            f"skip-worktree): {brief_list(flagged)}"
            # ``ls-files -v`` tags a flagged entry (lowercase letter or ``S``); status cannot see it.
            + _inspect(plan.path, "ls-files -v", "status --short --ignored"),
        )
    if status.changed:
        _refuse(
            plan,
            "unfrozen_changes",
            f"the worktree has changes that were never frozen: {brief_list(status.changed)}"
            + _inspect(plan.path, "status --short --ignored", "diff --stat"),
        )
    if status.unsupported:
        _refuse(plan, "unsupported_status", "; ".join(status.unsupported[:3]))


def _check_retention(
    plan: CleanPlan, repo: GitRepo, receipt: ResultReceipt | None, base_commit: str
) -> None:
    """4. The candidate must outlive the worktree; what it is kept by goes into ``keeps``."""
    if receipt is not None and receipt.candidate.git_commit:
        if not plan.candidate_ref:
            _refuse(plan, "no_candidate_ref", "the receipt has no candidate ref to check")
        elif plan.candidate_ref_target != receipt.candidate.git_commit:
            _refuse(
                plan,
                "candidate_ref_mismatch",
                f"{plan.candidate_ref} points at {plan.candidate_ref_target[:12] or 'nothing'}, "
                f"not the candidate {receipt.candidate.git_commit[:12]}",
            )
    elif receipt is None and not any(item["reason"] == "head_drift" for item in plan.refusals):
        # A failed candidate has no receipt; it still must not be silently discarded. Removing
        # the worktree drops the only reference HFlow knows to HEAD unless a ref outlives it.
        _check_head_retained(plan, repo, base_commit)
    if plan.candidate_ref and plan.candidate_ref_target:
        plan.keeps.insert(
            0,
            f"the candidate commit {plan.candidate_ref_target[:12]} and its ref "
            f"{plan.candidate_ref} (clean deletes no commit and no ref)",
        )
    other_refs = [ref for ref in plan.retained_by if ref != plan.candidate_ref]
    if other_refs:
        plan.keeps.insert(
            0, f"the commit at HEAD {plan.head[:12]}, reachable through {brief_list(other_refs, 3)}"
        )


def _check_ignored(plan: CleanPlan, status: StatusReport) -> None:
    """5. Ignored is not disposable: only the known artifacts go with the worktree."""
    unowned_ignored = [
        path for path in status.ignored if not matches_pattern(path, IGNORED_ARTIFACT_ALLOWLIST)
    ]
    if unowned_ignored:
        _refuse(
            plan,
            "unknown_ignored_files",
            "the worktree contains ignored files that are not known build artifacts and may be "
            f"the user's: {brief_list(unowned_ignored)}"
            + _inspect(plan.path, "status --short --ignored"),
        )
    if status.ignored:
        plan.reasons.append(
            "known artifacts inside the worktree will be removed with it: "
            + brief_list(status.ignored)
        )


def _recheck_cleanup_claim(store: Store, claimed: CleanPlan) -> dict | None:
    """Recheck each deletion against the identity claimed before any Git mutation."""
    if not Path(claimed.path).exists():
        return {"applied": False, **reconcile_cleanup(store, claimed.run_id)}
    current = plan_cleanup(store, claimed.run_id)
    identity = ("path", "provenance", "root_repository", "common_dir")
    identity_changed = any(getattr(current, key) != getattr(claimed, key) for key in identity)
    if not current.allowed or identity_changed:
        reason = (
            ("the workspace identity changed after the cleanup claim" if identity_changed else "")
            or "; ".join(item["detail"] for item in current.refusals)
            or "; ".join(current.reasons)
            or "the workspace identity changed after the cleanup claim"
        )
        store.finish_cleanup(claimed.run_id, error=reason[:400])
        return {
            "applied": False, "status": "REFUSED", "plan": current.as_dict(),
            "detail": "the cleanup guards or claimed workspace identity changed; nothing was removed",
        }
    return None


def apply_cleanup(store: Store, run_id: str, *, operator: str = "local-controller") -> dict:
    """Remove the run's workspace after re-checking the guards. Idempotent and reconcilable.

    Order matters: re-plan (the earlier preview is not a standing permission), claim the
    intent transactionally, run git, then confirm the filesystem *and* the registration
    before recording the outcome. Git and SQLite cannot be one transaction, so both results
    are verified separately. A partial or unverifiable removal keeps its cleanup intent for
    reconciliation instead of recording completion - and a later call reports that removal
    (:func:`_unfinished_removal`) rather than refusing the directory it left as "not a worktree".
    """
    plan = plan_cleanup(store, run_id)
    if plan.already_removed:
        return {"applied": False, "status": WORKSPACE_REMOVED, "detail": "already removed (idempotent)"}
    if not plan.allowed:
        unfinished = _unfinished_removal(store, run_id)
        if unfinished is not None:
            return {"applied": False, **unfinished}
        return {
            "applied": False, "status": "REFUSED", "plan": plan.as_dict(),
            "detail": "the cleanup guards refused; the workspace is untouched",
        }
    try:
        claim = store.record_cleanup_intent(
            run_id, operator, expected_path=plan.path, expected_provenance=plan.provenance,
            expected_root_repository=plan.root_repository,
        )
    except StoreError as exc:
        return {"applied": False, "status": "REFUSED", "detail": str(exc)}
    if claim.startswith("refused:"):
        return {"applied": False, "status": "REFUSED", "detail": claim}
    if claim == "already_removed":
        return {"applied": False, "status": WORKSPACE_REMOVED, "detail": "already removed (idempotent)"}
    if claim == "in_progress":
        return {
            "applied": False,
            "status": "REMOVING",
            "detail": "another cleanup already holds the intent for this run",
        }

    worktree = Path(plan.path)
    assert plan.source is not None  # an allowed plan opened its recorded source repository
    # A check's child process may still be closing its last handles when the run settles
    # (Windows keeps a directory undeletable until the last handle goes). One bounded retry
    # distinguishes "not yet released" from "someone is holding this open".
    #
    # Git runs in the recorded source repository - never inside the target, whose `.git` file a
    # failed `worktree remove` may already have deleted, and never from the main worktree, which
    # for a bare repository is the git directory itself (`safe.bareRepository=explicit` refuses
    # it). A failed attempt is judged by the registration in the common directory alone
    # (:func:`_after_failed_removal`): git deletes it even when it could not delete the directory.
    attempts: list[str] = []
    for attempt in range(2):
        if attempts:
            settled = _after_failed_removal(store, plan, worktree)
            if settled is not None:
                return {**settled, "attempts": attempts}
            time.sleep(1.5)
        refused = _recheck_cleanup_claim(store, plan)
        if refused is not None:
            return {**refused, "attempts": attempts} if attempts else refused
        try:
            plan.source.remove_worktree_checked(worktree)
        except (GitError, RefusedError) as exc:
            attempts.append(f"attempt {attempt + 1}: {exc}")
            continue
        attempts.append(f"attempt {attempt + 1}: removed")
        break
    else:
        settled = _after_failed_removal(store, plan, worktree)
        if settled is not None:
            return {**settled, "attempts": attempts}
        store.finish_cleanup(run_id, error=f"git worktree remove failed: {attempts[-1]}")
        return {
            "applied": False,
            "status": "FAILED",
            "detail": (
                f"git refused to remove the worktree ({attempts[-1]}) and removed nothing. HFlow "
                "does not force removal; close anything using that directory and re-run the same "
                "command."
            ),
            "attempts": attempts,
            "path": str(worktree),
        }

    # Both facts are checked independently: the directory, and git's registration.
    gone = not worktree.exists()
    try:
        still_registered = git_registration_exists(plan.common_dir, worktree)
    except Exception as exc:  # noqa: BLE001 - an unreadable registration is not a success
        return {
            "applied": False, "status": "REGISTRATION_UNKNOWN", "path": str(worktree),
            "detail": f"Git registration could not be checked: {exc}; cleanup intent kept",
        }
    if not gone and not still_registered:
        return {"applied": False, **_partial_removal(store, run_id, worktree), "attempts": attempts}
    if gone and not still_registered:
        store.finish_cleanup(run_id)
        # Only what exists is named: a run without a receipt has no receipt to keep, and a run
        # that never froze a candidate has no candidate ref.
        kept: list[str] = []
        if plan.candidate_ref and plan.candidate_ref_target:
            kept.append(f"the candidate ref {plan.candidate_ref}")
        kept.extend(ref for ref in plan.retained_by if ref != plan.candidate_ref)
        kept.append("the receipt and its evidence" if plan.receipt_present else "the run's evidence")
        return {
            "applied": True,
            "status": WORKSPACE_REMOVED,
            "path": str(worktree),
            "candidate_ref": plan.candidate_ref,
            "detail": "workspace removed; kept " + ", ".join(kept),
        }
    return {
        "applied": False,
        "status": "PARTIAL",
        "path": str(worktree),
        "detail": (
            f"removal could not be confirmed (directory gone: {gone}, still registered: "
            f"{still_registered}); nothing was forced"
        ),
    }


def git_registration_exists(common_dir: str, worktree: Path) -> bool:
    """Is this path still registered, judged from the repository's own metadata?"""
    target = worktree.resolve()
    return any(
        record.get("worktree") and Path(record["worktree"]).resolve() == target
        for record in registered_worktrees(common_dir)
    )


def _left_behind(worktree: Path) -> str:
    """What a removal git could not finish left in ``worktree``: its entries' names, read only."""
    try:
        names = sorted(entry.name for entry in os.scandir(worktree))
    except OSError as exc:
        return f"what it holds could not be listed ({exc})"
    if not names:
        return "it is empty"
    return f"it still holds {len(names)} entr{'y' if len(names) == 1 else 'ies'}: {brief_list(names)}"


def _partial_removal(store: Store, run_id: str, worktree: Path) -> dict:
    """``PARTIAL``: git dropped the registration but the directory remains. The intent stays.

    Git deletes a worktree's directory, then its registration - the registration even when the
    directory could not be deleted - so what remains is no longer a worktree to git, and the
    guards cannot be applied to it again. HFlow never deletes a directory itself (no ``rmtree``),
    so removing it is the operator's, and ``clean --reconcile`` then records the removal.
    """
    return {
        "status": "PARTIAL",
        "path": str(worktree),
        "detail": (
            f"git removed this worktree's registration but could not delete the directory "
            f"{worktree} (on Windows, typically a file something still holds open); "
            f"{_left_behind(worktree)}. Git no longer knows it as a worktree and HFlow never "
            "deletes a directory itself, so removing what is left by hand is yours to do once "
            "nothing uses it - what clean keeps (refs, receipt, evidence) lives outside it. Then "
            f"{hflow_command(store, 'clean', run_id, '--reconcile')} records the removal; the "
            "cleanup intent is kept until then"
        ),
    }


def _after_failed_removal(store: Store, plan: CleanPlan, worktree: Path) -> dict | None:
    """Where a ``git worktree remove`` that failed left things; ``None`` when nothing was removed.

    Asked of the repository's common directory only - the worktree's own ``.git`` may be gone.
    Still registered: git refused before deleting anything, so the guards can be rechecked and the
    removal retried or reported as refused. Not registered: git got as far as deleting, so the
    outcome is ``REMOVED`` (nothing left) or ``PARTIAL`` (a directory left), and the intent stays
    for that removal - never re-planned, and never released as if nothing had happened.
    """
    try:
        registered = git_registration_exists(plan.common_dir, worktree)
    except Exception as exc:  # noqa: BLE001 - an unreadable registration is not an answer
        return {
            "applied": False, "status": "REGISTRATION_UNKNOWN", "path": str(worktree),
            "detail": f"Git registration could not be checked: {exc}; cleanup intent kept",
        }
    if registered:
        return None
    if not worktree.exists():
        return {"applied": False, **reconcile_cleanup(store, plan.run_id), "path": str(worktree)}
    return {"applied": False, **_partial_removal(store, plan.run_id, worktree)}


def _unfinished_removal(store: Store, run_id: str) -> dict | None:
    """The outcome of an earlier removal that kept its intent, when it left something to report.

    ``PARTIAL`` (a directory git no longer registers) or ``REMOVED`` (it is gone now, recorded);
    ``None`` otherwise, including when no intent is pending - the guards' refusal stands then.
    """
    state = store.workspace_state(run_id)
    if not state["cleanup_intent_at"] or state["cleanup_done_at"]:
        return None
    reconciled = reconcile_cleanup(store, run_id)
    return reconciled if reconciled["status"] in {"PARTIAL", WORKSPACE_REMOVED} else None


def reconcile_cleanup(store: Store, run_id: str) -> dict:
    """After an interruption, decide this run's workspace state from recorded facts only.

    Combines the durable intent with the current filesystem and registration: no re-dispatch,
    no scanning, no touching any other directory.
    """
    state = store.workspace_state(run_id)
    path = state["worktree_path"]
    if not path:
        return {"status": "NO_WORKSPACE", "detail": "this run never had a managed workspace"}
    worktree = Path(path)
    exists = worktree.exists()
    if not exists and not state["cleanup_intent_at"] and not state["cleanup_done_at"]:
        return {
            "status": WORKSPACE_MISSING,
            "detail": "the path is gone with no cleanup intent: MISSING/unknown, not a success",
        }
    try:
        provenance = store.workspace_provenance_for(run_id)
        if provenance is not None:
            if provenance.worktree_path != str(worktree):
                raise StoreError("workspace provenance names a different path")
            common_dir = provenance.git_common_dir
        else:
            source = _rootless_source(store, run_id)
            if source is None:
                raise StoreError("legacy rootless run has no recorded repository source")
            common_dir = source[1].absolute_common_dir
        registered = git_registration_exists(common_dir, worktree)
    except Exception as exc:  # noqa: BLE001 - a failed read never proves removal
        return {
            "status": "REGISTRATION_UNKNOWN",
            "detail": f"Git registration could not be checked: {exc}; cleanup intent kept",
        }
    if not exists:
        if registered:
            return {
                "status": "PARTIAL", "detail": "the path is gone but Git registration remains; cleanup intent kept",
            }
        if not state["cleanup_done_at"]:
            store.finish_cleanup(run_id)
        return {"status": WORKSPACE_REMOVED, "detail": "directory and Git registration confirmed gone"}
    if not registered and state["cleanup_intent_at"] and not state["cleanup_done_at"]:
        return _partial_removal(store, run_id, worktree)
    return {
        "status": WORKSPACE_PRESENT if registered else "UNREGISTERED",
        "detail": f"path exists (registered={registered}); nothing was removed",
    }
