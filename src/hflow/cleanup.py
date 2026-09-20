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
3. HEAD must still be the frozen candidate, with no unfrozen changes;
4. the candidate must be reachable through a ref that outlives the worktree;
5. untracked and ignored files are refused unless they match the explicit artifact policy -
   an ignored `.env` is a user's file, not garbage.

Anything unclear refuses and keeps the scene, which is the correct outcome.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import AttemptState, RefusalCode, RefusedError, ResultReceipt, TaskSpec, TaskState
from .gitworkspace import IGNORED_ARTIFACT_ALLOWLIST, GitError, GitRepo
from .store import Store
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
        }


def _refuse(plan: CleanPlan, code: str, detail: str) -> None:
    plan.allowed = False
    plan.refusals.append({"reason": code, "detail": detail})


def plan_cleanup(store: Store, run_id: str) -> CleanPlan:
    """Inspect a run's workspace and decide whether removal is allowed. Changes nothing."""
    row = store.get_run(run_id)
    plan = CleanPlan(run_id=run_id, allowed=True)
    plan.workspace_state = str(row["worktree_state"])
    plan.task_state = str(row["task_state"])
    plan.keeps = [
        "the candidate commit and its ref (delivery is not deleted by clean)",
        "the receipt and its evidence, stored outside the worktree",
        "the run's history, notes and any cleanup record",
    ]

    receipt = (
        ResultReceipt.model_validate(__import__("json").loads(row["receipt_json"]))
        if row["receipt_json"]
        else None
    )
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
    plan.common_dir = str(repo.common_dir)
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
    if intent_at and (cancel_receipt is None or cancel_receipt.status != "confirmed_stopped"):
        _refuse(
            plan,
            "stop_unconfirmed",
            "a cancellation was requested but never confirmed; the workspace must be kept",
        )
    if str(row["task_state"]) in {TaskState.RUNNING.value, TaskState.CHECKING.value}:
        _refuse(
            plan,
            "run_in_flight",
            f"the run is {row['task_state']}; only a settled run's workspace may be released",
        )

    # --- 3. HEAD must still be the frozen candidate, with nothing unfrozen
    plan.head = repo.worktree_commit(worktree)
    if receipt is not None:
        plan.expected_candidate = receipt.candidate.git_commit
        plan.candidate_ref = repo.candidate_ref(run_id, receipt.attempt_id)
        plan.candidate_ref_target = repo.ref_target(plan.candidate_ref) or ""
    if plan.expected_candidate and plan.head != plan.expected_candidate:
        _refuse(
            plan,
            "head_drift",
            f"HEAD is {plan.head[:12]} but the frozen candidate is {plan.expected_candidate[:12]}",
        )
    try:
        status = repo.status_report(worktree)
    except (GitError, Exception) as exc:  # noqa: BLE001 - a failed read must refuse, not guess
        _refuse(plan, "status_failed", repr(exc))
        return plan
    plan.tracked_changes = list(status.changed)
    plan.ignored_paths = list(status.ignored)
    plan.unsupported = list(status.unsupported)
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
    elif receipt is None:
        # A failed candidate has no receipt; it still must not be silently discarded.
        plan.reasons.append(
            "no receipt: this run has no delivered candidate, so cleanup would discard an "
            "unaccepted result - allowed only because the candidate ref keeps it reachable"
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


def apply_cleanup(store: Store, run_id: str, *, operator: str = "local-controller") -> dict:
    """Remove the run's workspace after re-checking the guards. Idempotent and reconcilable.

    Order matters: re-plan (the earlier preview is not a standing permission), claim the
    intent transactionally, run git, then confirm the filesystem *and* the registration
    before recording the outcome. Git and SQLite cannot be one transaction, so both results
    are verified separately and a partial outcome is recorded as an error rather than as
    success.
    """
    claim = store.record_cleanup_intent(run_id, operator)
    if claim == "already_removed":
        return {"applied": False, "status": WORKSPACE_REMOVED, "detail": "already removed (idempotent)"}
    if claim == "in_progress":
        return {
            "applied": False,
            "status": "REMOVING",
            "detail": "another cleanup already holds the intent for this run",
        }

    plan = plan_cleanup(store, run_id)
    if not plan.allowed:
        store.finish_cleanup(run_id, error="; ".join(item["detail"] for item in plan.refusals)[:400])
        return {
            "applied": False,
            "status": "REFUSED",
            "plan": plan.as_dict(),
            "detail": "the cleanup guards refused; the workspace is untouched",
        }

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
    except RefusedError:
        command_root = Path(plan.common_dir).parent if plan.common_dir else worktree.parent
    attempts: list[str] = []
    for attempt in range(2):
        try:
            GitRepo(command_root).remove_worktree_checked(worktree)
            attempts.append(f"attempt {attempt + 1}: removed")
            break
        except (GitError, RefusedError) as exc:
            attempts.append(f"attempt {attempt + 1}: {exc}")
            if attempt == 0:
                time.sleep(1.5)
                continue
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
    except Exception:  # noqa: BLE001 - an unreadable registration is not a success
        still_registered = True
    if gone and not still_registered:
        store.finish_cleanup(run_id)
        return {
            "applied": True,
            "status": WORKSPACE_REMOVED,
            "path": str(worktree),
            "candidate_ref": plan.candidate_ref,
            "detail": "workspace removed; the candidate ref and the receipt were kept",
        }
    store.finish_cleanup(
        run_id,
        error=f"partial removal: directory_gone={gone} registration_present={still_registered}",
    )
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
    registered = False
    if exists:
        try:
            registered = git_registration_exists(str(GitRepo.discover(worktree).common_dir), worktree)
        except (GitError, RefusedError):
            registered = True  # unreadable means "not proven removed"
    if not exists and state["cleanup_done_at"]:
        return {"status": WORKSPACE_REMOVED, "detail": "removed and recorded"}
    if not exists and not state["cleanup_done_at"]:
        if state["cleanup_intent_at"]:
            store.finish_cleanup(run_id)
            return {
                "status": WORKSPACE_REMOVED,
                "detail": "a cleanup intent existed and the path is gone; recorded as removed",
            }
        return {
            "status": WORKSPACE_MISSING,
            "detail": "the path is gone with no cleanup intent: MISSING/unknown, not a success",
        }
    return {
        "status": WORKSPACE_PRESENT if registered else "UNREGISTERED",
        "detail": f"path exists (registered={registered}); nothing was removed",
    }
