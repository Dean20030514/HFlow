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

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    AttemptState, RefusedError, ResultReceipt, TaskSpec, TaskState,
    WorkspaceProvenance,
)
from .gitworkspace import IGNORED_ARTIFACT_ALLOWLIST, GitError, GitRepo, _base_env
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


#: A full commit id (SHA-1 or SHA-256 object format).
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: The note the controller writes when it resolves the task's base name to a commit.
_BASE_NOTE = re.compile(r"base .+ -> (?P<commit>[0-9a-f]{40}(?:[0-9a-f]{24})?)")
#: The namespaces whose refs count as keeping a commit: a branch, a tag, or an HFlow ref. A
#: remote-tracking ref is not one - a fetch may prune it - and neither is a worktree's own HEAD.
_RETAINING_NAMESPACES = ("refs/heads", "refs/tags", "refs/hflow")


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
    return named if _FULL_COMMIT.fullmatch(named) else ""


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
            f"no receipt: HEAD {head[:12]} is not the run's base, but "
            + ", ".join(refs[:3])
            + (f" (+{len(refs) - 3} more)" if len(refs) > 3 else "")
            + " contains it, so the commit stays reachable after the worktree is removed"
        )
        return
    _refuse(
        plan,
        "unretained_commit",
        f"HEAD {head} is not the run's base commit ({base_commit[:12] or 'not recorded'}) and no "
        "branch, tag or HFlow ref contains it, so removing the worktree would leave that commit "
        f"unreferenced. Inspect it (git -C \"{plan.path}\" show {head[:12]}); to keep it, create "
        f"a ref (for example git branch <name> {head}), then run clean again",
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
        ResultReceipt.model_validate(__import__("json").loads(row["receipt_json"]))
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
    spec = TaskSpec.model_validate(__import__("json").loads(row["task_spec_json"]))
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
    try:
        plan.provenance = store.workspace_provenance_for(run_id)
    except StoreError as exc:
        _refuse(plan, "provenance_unreadable", str(exc))
        return plan
    if plan.provenance is not None and (
        str(worktree) != plan.provenance.worktree_path
        or worktree.resolve() != Path(plan.provenance.worktree_path)
    ):
        _refuse(plan, "provenance_mismatch", "the workspace path differs from its recorded source")
        return plan
    try:
        if plan.provenance is not None:
            source_repo = GitRepo.discover(Path(plan.provenance.project_root))
            source_common = str((source_repo.root / source_repo.common_dir).resolve())
            if source_common != plan.provenance.git_common_dir:
                _refuse(plan, "provenance_mismatch", "recorded repository now has a different Git common directory")
                return plan
        else:
            root = store.root_budget_for_run(run_id)
            if root is None:
                _refuse(plan, "workspace_provenance_missing", "legacy rootless run has no reliable recorded repository source")
                return plan
            plan.root_repository = str(root["repo_path"])
            source_repo = GitRepo.discover(Path(plan.root_repository))
            source_common = str((source_repo.root / source_repo.common_dir).resolve())
    except (GitError, RefusedError, OSError) as exc:
        _refuse(plan, "provenance_unreadable", f"recorded repository source cannot be checked: {exc}")
        return plan

    # --- 1. ownership: the real registration, not a string comparison
    if not worktree.exists():
        plan.allowed = False
        plan.reasons.append(
            "the recorded worktree path no longer exists and no removal was recorded: "
            "reporting MISSING rather than claiming a successful cleanup"
        )
        plan.workspace_state = WORKSPACE_MISSING
        return plan
    try:
        repo = GitRepo.discover(worktree)
    except RefusedError as exc:
        _refuse(plan, "not_a_worktree", str(exc))
        return plan
    plan.common_dir = str((repo.root / repo.common_dir).resolve())
    if plan.common_dir != source_common:
        _refuse(plan, "provenance_mismatch", "Git common directory differs from its recorded source")
        return plan
    try:
        registration = repo.worktree_registration(worktree)
    except GitError as exc:
        _refuse(plan, "git_query_failed", str(exc))
        return plan
    if registration is None:
        _refuse(plan, "not_registered", "git does not have this path registered as a worktree")
        return plan
    plan.registered = True
    if repo.is_main_worktree(worktree):
        _refuse(
            plan,
            "is_source_repository",
            "this path is the repository's main worktree, not a run worktree",
        )
        return plan
    registered_common = registration.get("worktree")
    if registered_common and Path(registered_common).resolve() != worktree.resolve():
        _refuse(
            plan,
            "registration_mismatch",
            f"git registers {registered_common} for this entry, not {worktree}",
        )
        return plan

    # --- 2. execution state: a name is not proof that a process stopped
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
    if str(row["task_state"]) in {TaskState.RUNNING.value, TaskState.CHECKING.value}:
        _refuse(
            plan,
            "run_in_flight",
            f"the run is {row['task_state']}; only a settled run's workspace may be released",
        )

    # --- 3. HEAD must still be the frozen candidate, with nothing unfrozen
    plan.head = repo.worktree_commit(worktree)
    base_commit = ""
    if receipt is not None:
        plan.expected_candidate = receipt.candidate.git_commit
        plan.candidate_ref = repo.candidate_ref(run_id, receipt.attempt_id)
        plan.candidate_ref_target = repo.ref_target(plan.candidate_ref) or ""
        if plan.expected_candidate and plan.head != plan.expected_candidate:
            _refuse(
                plan,
                "head_drift",
                f"HEAD is {plan.head[:12]} but the frozen candidate is "
                f"{plan.expected_candidate[:12]}",
            )
    else:
        # No receipt: the expected HEAD comes from stored facts - the latest attempt's frozen
        # candidate when one was recorded, and the run's base commit (nothing committed yet).
        base_commit = _recorded_base_commit(store, run_id, spec)
        if attempt is not None:
            frozen = _recorded_frozen_commit(store, run_id, str(attempt["attempt_id"]))
            if frozen:
                plan.expected_candidate = frozen
                plan.candidate_ref = repo.candidate_ref(run_id, str(attempt["attempt_id"]))
                plan.candidate_ref_target = repo.ref_target(plan.candidate_ref) or ""
        if plan.expected_candidate and plan.head not in {plan.expected_candidate, base_commit}:
            _refuse(
                plan,
                "head_drift",
                f"HEAD is {plan.head[:12]} but the latest attempt's frozen candidate is "
                f"{plan.expected_candidate[:12]} and the run's base is "
                f"{base_commit[:12] or 'not recorded'}",
            )
    try:
        status = repo.status_report(worktree)
        flagged = repo.index_flagged_paths(worktree)
    except (GitError, Exception) as exc:  # noqa: BLE001 - a failed read must refuse, not guess
        _refuse(plan, "status_failed", repr(exc))
        return plan
    plan.tracked_changes = list(status.changed)
    plan.ignored_paths = list(status.ignored)
    plan.unsupported = list(status.unsupported)
    if flagged:
        _refuse(
            plan, "index_flags_hide_changes",
            "Git index flags may hide uncommitted worktree bytes (assume-unchanged or "
            "skip-worktree): " + ", ".join(flagged[:5]),
        )
    if status.changed:
        _refuse(
            plan,
            "unfrozen_changes",
            "the worktree has changes that were never frozen: "
            + ", ".join(status.changed[:5]),
        )
    if status.unsupported:
        _refuse(plan, "unsupported_status", "; ".join(status.unsupported[:3]))

    # --- 4. the candidate must outlive the worktree
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
            0,
            f"the commit at HEAD {plan.head[:12]}, reachable through "
            + ", ".join(other_refs[:3])
            + (f" (+{len(other_refs) - 3} more)" if len(other_refs) > 3 else ""),
        )

    # --- 5. ignored is not disposable
    unowned_ignored = [
        path for path in status.ignored if not matches_pattern(path, IGNORED_ARTIFACT_ALLOWLIST)
    ]
    if unowned_ignored:
        _refuse(
            plan,
            "unknown_ignored_files",
            "the worktree contains ignored files that are not known build artifacts and may be "
            "the user's: " + ", ".join(unowned_ignored[:5]),
        )
    if status.ignored:
        plan.reasons.append(
            "known artifacts inside the worktree will be removed with it: "
            + ", ".join(status.ignored[:5])
        )

    if plan.allowed and not plan.reasons:
        plan.reasons.append("the worktree is registered, clean, at the frozen candidate, and its ref resolves")
    return plan


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
    reconciliation instead of recording completion.
    """
    plan = plan_cleanup(store, run_id)
    if plan.already_removed:
        return {"applied": False, "status": WORKSPACE_REMOVED, "detail": "already removed (idempotent)"}
    if not plan.allowed:
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

    refused = _recheck_cleanup_claim(store, plan)
    if refused is not None:
        return refused

    worktree = Path(plan.path)
    # A check's child process may still be closing its last handles when the run settles
    # (Windows keeps a directory undeletable until the last handle goes). One bounded retry
    # distinguishes "not yet released" from "someone is holding this open".
    #
    # The retry calls git from the *main worktree*, not from inside the target: a failed
    # `worktree remove` may already have deleted the target's `.git` file, after which
    # discovering a repository through that path fails and would report a misleading
    # "not a git repository" instead of the real reason.
    try:
        driver_repo = GitRepo.discover(worktree)
        command_root = driver_repo.main_worktree() or driver_repo.root
    except (RefusedError, GitError) as exc:
        return {
            "applied": False, "status": "REGISTRATION_UNKNOWN", "path": str(worktree),
            "detail": f"workspace repository could not be checked: {exc}; cleanup intent kept",
        }
    attempts: list[str] = []
    for attempt in range(2):
        refused = _recheck_cleanup_claim(store, plan)
        if refused is not None:
            return {**refused, "attempts": attempts}
        try:
            GitRepo(command_root).remove_worktree_checked(worktree)
            attempts.append(f"attempt {attempt + 1}: removed")
            break
        except (GitError, RefusedError) as exc:
            attempts.append(f"attempt {attempt + 1}: {exc}")
            if attempt == 0:
                time.sleep(1.5)
                continue
            if not worktree.exists():
                reconciled = reconcile_cleanup(store, run_id)
                return {"applied": False, **reconciled, "attempts": attempts, "path": str(worktree)}
            store.finish_cleanup(run_id, error=f"git worktree remove failed: {exc}")
            return {
                "applied": False,
                "status": "FAILED",
                "detail": (
                    f"git refused to remove the worktree: {exc}. HFlow does not force removal; "
                    "close anything using that directory and re-run the same command."
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
    import subprocess

    completed = subprocess.run(  # noqa: S603,S607 - read-only query
        ["git", "--git-dir", common_dir, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=_base_env(),
    )
    if completed.returncode != 0:
        raise GitError(completed.stderr.strip()[:200])
    for line in completed.stdout.splitlines():
        if line.startswith("worktree ") and Path(line.split(" ", 1)[1]).resolve() == worktree.resolve():
            return True
    return False


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
            root = store.root_budget_for_run(run_id)
            if root is None:
                raise StoreError("legacy rootless run has no recorded repository source")
            source_repo = GitRepo.discover(Path(root["repo_path"]))
            common_dir = str((source_repo.root / source_repo.common_dir).resolve())
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
    return {
        "status": WORKSPACE_PRESENT if registered else "UNREGISTERED",
        "detail": f"path exists (registered={registered}); nothing was removed",
    }
