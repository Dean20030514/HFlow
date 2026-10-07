"""Controlled integration of an accepted candidate into a local branch (batch I2).

A run ends at ``LOCAL_CANDIDATE``: a frozen commit in a detached worktree, checked and reviewed.
Getting that change onto a branch the user works with is a *separate* delivery with its own
facts, so it has its own record (``integrations``) and its own receipt, and the run's
``ResultReceipt`` is never rewritten. The contract is ``docs/batch-i-integration-plan.md``; in
short:

``prepare``
    fixes the target tip ``T``, builds one integration commit ``M`` on top of it with Git plumbing
    only (``commit-tree``, and ``merge-tree --write-tree`` when the target moved since the task's
    base), keeps ``M`` reachable through an HFlow-owned ref, checks ``M`` out in a fresh detached
    worktree of its own and runs the run's approved checks there (phase ``integration-check``).
    Nothing the user owns is written: not their checkout, not their index, not their branches.
``apply``
    is the operator's explicit approval. ``--expect-target`` must name the tip the integration was
    checked against. The intent is committed to SQLite *before* the branch moves, and the branch
    moves only by Git's compare-and-set (``update-ref <ref> <new> <old>``). A branch that some
    worktree has checked out is never moved: that would leave the checkout's index and files at the
    old commit while its HEAD names the new one. The operator gets the exact command instead.
``reconcile``
    looks at Git and settles a record whose process died: an interrupted ``applying`` becomes
    ``integrated`` when the target contains ``M``, ``ready`` when it is still ``T``, ``stale``
    otherwise. It never re-runs the ref update and never re-runs a check.

No step here calls a model, dispatches an agent or spends any budget.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    DeliveryState,
    IntegrationReceipt,
    IntegrationRecord,
    IntegrationState,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    ResultReceipt,
    TaskSpec,
    TaskState,
    VerificationResult,
)
from .gitworkspace import MIN_INTEGRATION_GIT, GitError, GitRepo
from .ids import new_integration_id, utc_now
from .ownership import ProcessIdentity, current_identity, probe
from .paths import database_path, default_data_dir
from .store import IntegrationConflict, IntegrationNotFound, Store
from .verify import CheckRunners, verify_candidate
from .workspace import candidate_fingerprint, paths_outside_scope

#: The evidence kind of a check executed on an integration tree. Kept apart from the run's own
#: ``verification`` rows so the acceptance path can never read an integration result as its own.
EVIDENCE_KIND = "integration-check"

#: States whose record is settled: nothing further happens to it without a new ``prepare``.
_SETTLED = {
    IntegrationState.INTEGRATED,
    IntegrationState.CONFLICT,
    IntegrationState.CHECKS_FAILED,
    IntegrationState.STALE,
    IntegrationState.INTERRUPTED,
    IntegrationState.SUPERSEDED,
    IntegrationState.FAILED,
}


@dataclass
class IntegrationOutcome:
    """What one integrate command did, from stored facts.

    ``handoff`` lists commands for the operator to run themselves; HFlow never runs them.
    """

    record: IntegrationRecord
    receipt: IntegrationReceipt | None = None
    notes: list[str] = field(default_factory=list)
    handoff: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "integration": self.record.model_dump(mode="json"),
            "receipt": self.receipt.model_dump(mode="json") if self.receipt else None,
            "notes": list(self.notes),
            "handoff": list(self.handoff),
        }


class IntegrationBusy(RefusedError):
    """The process recorded on an active integration may still be running; nothing was written."""


def _refuse(code: RefusalCode, message: str) -> RefusedError:
    return RefusedError(code, message)


def _absolute_common_dir(repo: GitRepo) -> str:
    return str((repo.root / repo.common_dir).resolve())


def _contains(repo: GitRepo, tip: str | None, commit: str) -> bool:
    """Does the branch tip ``tip`` contain ``commit``? ``None`` (no branch) contains nothing."""
    if not tip:
        return False
    return tip == commit or repo.is_ancestor(commit, tip)


def _run_receipt(store: Store, run_id: str) -> tuple[Any, ResultReceipt, TaskSpec]:
    row = store.get_run(run_id)
    if row["task_state"] != TaskState.ACCEPTED.value or not row["receipt_json"]:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"run {run_id} is {row['task_state']} with no delivery receipt; only an ACCEPTED run's "
            "candidate can be integrated",
        )
    receipt = ResultReceipt.model_validate(json.loads(row["receipt_json"]))
    spec = TaskSpec.model_validate(json.loads(row["task_spec_json"]))
    return row, receipt, spec


def _open_repo(store: Store, run_id: str) -> tuple[GitRepo, str]:
    """The run's own repository, identified by what the run recorded - never by the cwd."""
    provenance = store.workspace_provenance_for(run_id)
    if provenance is not None:
        root, common = provenance.project_root, provenance.git_common_dir
    else:
        binding = store.admission_binding_for(run_id)
        if binding is None or not binding.git_common_dir:
            raise _refuse(
                RefusalCode.NOT_INTEGRABLE,
                f"run {run_id} recorded no repository identity (no workspace provenance and no "
                "admission binding naming a Git common directory), so the repository its candidate "
                "belongs to cannot be identified",
            )
        root, common = binding.project_root, binding.git_common_dir
    return _repo_at(root, common, owner=f"run {run_id}")


def _repo_at(root: str, common: str, *, owner: str) -> tuple[GitRepo, str]:
    try:
        repo = GitRepo(Path(root))
    except GitError as exc:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"the repository {owner} recorded ({root}) cannot be opened: {exc}",
        ) from exc
    actual = _absolute_common_dir(repo)
    if common and str(Path(common).resolve()) != actual:
        raise _refuse(
            RefusalCode.PROJECT_MISMATCH,
            f"{root} now resolves to the Git common directory {actual}, not the {common} "
            f"{owner} recorded; refusing to integrate into a different repository",
        )
    version = repo.git_version()
    if version[:2] < MIN_INTEGRATION_GIT:
        needed = ".".join(str(part) for part in MIN_INTEGRATION_GIT)
        raise _refuse(
            RefusalCode.NOT_IMPLEMENTED,
            f"integration needs Git {needed} or later (`merge-tree --write-tree --merge-base`); "
            f"this Git is {'.'.join(str(part) for part in version)}",
        )
    return repo, actual


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def prepare_integration(
    store: Store,
    *,
    run_id: str,
    target_branch: str,
    project: ProjectConfig,
    runners: CheckRunners,
    data_dir: Path,
    allow_fake_checks: bool = False,
    controller_build: str = "unrecorded",
) -> IntegrationOutcome:
    """Build and check one integration commit for an accepted run. Never moves the target.

    Refusals (``RefusedError``) happen before any record exists. Once the record exists, every
    outcome - including a Git failure - is a recorded state, never an exception, except an
    interrupt, which leaves ``preparing``/``checking`` for :func:`reconcile_integration`.
    """
    row, receipt, spec = _run_receipt(store, run_id)
    if receipt.delivery_state is not DeliveryState.LOCAL_CANDIDATE:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"run {run_id}'s receipt is {receipt.delivery_state.value}, not LOCAL_CANDIDATE",
        )
    candidate = receipt.candidate.git_commit
    base = receipt.candidate.base_commit
    if not candidate or not receipt.candidate.git_tree:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"run {run_id} delivered no Git candidate (it did not run in a Git worktree), so there "
            "is no commit to integrate",
        )
    if project.project_id != row["project_id"]:
        raise _refuse(
            RefusalCode.PROJECT_MISMATCH,
            f"the project contract is for {project.project_id!r}; run {run_id} belongs to "
            f"{row['project_id']!r}",
        )
    if project.checks_digest() != row["checks_digest"]:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"the project's approved checks changed since run {run_id} (checks digest "
            f"{project.checks_digest()} vs the run's {row['checks_digest']}); an integration is "
            "checked with the checks the run was accepted under, so use that contract",
        )
    check_map = project.check_map()
    for check_id in spec.required_check_ids():
        check = check_map.get(check_id)
        if check is None:  # pragma: no cover - the digest above already pins the checks
            raise _refuse(RefusalCode.UNKNOWN_CHECK, f"check {check_id!r} is not in the contract")
        if check.kind == "fake" and not allow_fake_checks:
            raise _refuse(
                RefusalCode.NOT_INTEGRABLE,
                f"check {check_id!r} is kind=fake: it runs no program, so it cannot verify an "
                "integration tree that is about to land on a real branch",
            )

    repo, common_dir = _open_repo(store, run_id)
    try:
        target_ref = repo.branch_ref(target_branch)
    except GitError as exc:
        raise _refuse(RefusalCode.INVALID_SPEC, f"--target {target_branch!r}: {exc}") from exc
    tip = repo.ref_target(target_ref)
    if tip is None:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"there is no branch {target_ref} in {repo.root}; HFlow integrates into an existing "
            "local branch and never creates one",
        )
    if not repo.ref_spelled_exactly(target_ref):
        # A case-insensitive file system resolves `MAIN` through the loose file of `main`; every
        # later comparison (which worktree has it checked out, which ref an apply locks) would then
        # miss the real branch, so the name must be the branch's exact spelling.
        raise _refuse(
            RefusalCode.INVALID_SPEC,
            f"--target {target_branch!r} resolves to {tip} only through a case-insensitive file "
            f"system: no ref is named exactly {target_ref}. Use the branch's exact spelling "
            "(`git branch --list` shows it)",
        )
    for label, commit in (("candidate", candidate), ("task base", base)):
        if not commit or not repo.commit_exists(commit):
            raise _refuse(
                RefusalCode.NOT_INTEGRABLE,
                f"the {label} commit {commit or '(none recorded)'} is not in {repo.root}",
            )
    if not repo.is_ancestor(base, tip):
        raise _refuse(
            RefusalCode.TARGET_MOVED,
            f"{target_ref} is at {tip}, which does not contain the task's base {base}: the branch "
            "was rewritten or is a different line of history. HFlow does not guess how the "
            "candidate relates to it; start a new task revision on the branch's current tip",
        )
    if _contains(repo, tip, candidate):
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"{target_ref} at {tip} already contains the candidate commit {candidate}; there is "
            "nothing to integrate",
        )
    _refuse_if_already_integrated(
        store, repo, run_id, target_ref, tip, controller_build=controller_build
    )

    identity = current_identity()
    now = utc_now()
    record = IntegrationRecord(
        integration_id=new_integration_id(),
        run_id=run_id,
        task_id=str(row["task_id"]),
        attempt_id=receipt.attempt_id,
        git_common_dir=common_dir,
        repo_root=str(repo.root),
        target_ref=target_ref,
        target_tip=tip,
        base_commit=base,
        candidate_commit=candidate,
        state=IntegrationState.PREPARING,
        checks_digest=str(row["checks_digest"]),
        owner_pid=identity.pid,
        owner_created=identity.created,
        owner_host=identity.host,
        created_at=now,
        updated_at=now,
    )
    try:
        record = store.create_integration(record)
    except IntegrationConflict as exc:
        raise _refuse(RefusalCode.NOT_INTEGRABLE, str(exc)) from exc

    notes: list[str] = []
    candidate_ref = repo.candidate_ref(run_id, receipt.attempt_id)
    if repo.ref_target(candidate_ref) != candidate:
        notes.append(
            f"the run's candidate ref {candidate_ref} no longer points at {candidate}; the "
            "integration ref keeps the integrated change reachable instead"
        )
    try:
        return _build_and_check(
            store,
            record,
            repo=repo,
            receipt=receipt,
            spec=spec,
            project=project,
            runners=runners,
            data_dir=data_dir,
            notes=notes,
        )
    except (GitError, OSError) as exc:
        return _fail(store, repo, record.integration_id, f"integration could not be prepared: {exc}")


def _refuse_if_already_integrated(
    store: Store,
    repo: GitRepo,
    run_id: str,
    target_ref: str,
    tip: str,
    *,
    controller_build: str,
) -> None:
    """Refuse a prepare whose change already reached this target through an earlier integration.

    A squashed integration commit is not the candidate commit, so "the target contains the
    candidate" never sees it. A ``ready`` record whose commit the target now contains (the hand
    merge after a hand-off) is first recorded as integrated - superseding it instead would throw
    away the only record of that delivery.
    """
    for earlier in store.integrations_for(run_id):
        if earlier.target_ref != target_ref or not earlier.integration_commit:
            continue
        if earlier.state is IntegrationState.READY and _contains(
            repo, tip, earlier.integration_commit
        ):
            try:
                _settle_moved_target(
                    store,
                    repo,
                    earlier,
                    tip,
                    expected=IntegrationState.READY,
                    build=controller_build,
                )
            except IntegrationConflict:
                pass  # another process settled it first; the re-read below decides
            earlier = store.integration(earlier.integration_id) or earlier
        if earlier.state is IntegrationState.INTEGRATED or _contains(
            repo, tip, earlier.integration_commit
        ):
            raise _refuse(
                RefusalCode.NOT_INTEGRABLE,
                f"run {run_id} was already integrated into {target_ref}: integration "
                f"{earlier.integration_id} ({earlier.state.value}) commit "
                f"{earlier.integration_commit} is contained in {tip}; nothing was written",
            )


def _fail(store: Store, repo: GitRepo, integration_id: str, detail: str) -> IntegrationOutcome:
    """Record a failure of an integration that is still being prepared, removing its worktree."""
    current = store.integration(integration_id)
    assert current is not None
    worktree_state, removal = _release_worktree(repo, current)
    try:
        record = store.update_integration(
            integration_id,
            expected=[IntegrationState.PREPARING, IntegrationState.CHECKING],
            state=IntegrationState.FAILED,
            worktree_state=worktree_state,
            detail=(detail + (f"; {removal}" if removal else ""))[:2000],
        )
    except IntegrationConflict:
        record = store.integration(integration_id) or current
    return IntegrationOutcome(record=record, notes=[detail])


def _commit_message(record: IntegrationRecord) -> str:
    return (
        f"hflow: integrate {record.task_id} (run {record.run_id})\n\n"
        f"Candidate: {record.candidate_commit}\n"
        f"Task base: {record.base_commit}\n"
        f"Target: {record.target_ref} at {record.target_tip}\n"
        f"Integration: {record.integration_id}\n"
    )


def _build_and_check(
    store: Store,
    record: IntegrationRecord,
    *,
    repo: GitRepo,
    receipt: ResultReceipt,
    spec: TaskSpec,
    project: ProjectConfig,
    runners: CheckRunners,
    data_dir: Path,
    notes: list[str],
) -> IntegrationOutcome:
    integration_id = record.integration_id
    tip, base, candidate = record.target_tip, record.base_commit, record.candidate_commit

    # --- one commit on top of the target tip, built without any working tree --------------
    if tip == base:
        # The target has not moved: the integrated tree *is* the candidate's tree. A new single
        # commit on the base keeps every earlier (rejected) repair round out of the branch.
        mode = "squash"
        tree = repo.tree_of(candidate)
    else:
        mode = "replayed"
        merged = repo.merge_tree(base=base, ours=tip, theirs=candidate)
        if not merged.clean:
            conflicts = list(merged.conflicts)
            record = store.update_integration(
                integration_id,
                expected=IntegrationState.PREPARING,
                state=IntegrationState.CONFLICT,
                mode=mode,
                conflict_paths=conflicts,
                detail=(
                    f"{record.target_ref} moved from the task's base {base} to {tip}, and the "
                    f"candidate's change does not merge onto it cleanly ({len(conflicts)} "
                    "conflicted path(s)). Nothing was committed or written; resolving it is a new "
                    "task revision based on the current tip, with its own approval"
                ),
            )
            return IntegrationOutcome(record=record, notes=[*notes, record.detail])
        tree = merged.tree
    if tree == repo.tree_of(tip):
        return _fail(
            store,
            repo,
            integration_id,
            f"merging the candidate onto {record.target_ref} at {tip} changes nothing: the "
            "change is already present in the target",
        )
    commit = repo.commit_tree(tree, parents=[tip], message=_commit_message(record))

    # --- the integrated change is the candidate's change, inside the task's scope -------------
    paths = repo.diff_paths(tip, commit)
    delivered = set(receipt.candidate_paths)
    unexpected = [path for path in paths if path not in delivered]
    outside = paths_outside_scope(paths, spec.scope, project.write_deny)
    if unexpected or outside:
        named = sorted({*unexpected, *outside})
        return _fail(
            store,
            repo,
            integration_id,
            "the integration commit changes paths the accepted delivery did not, or that the "
            "task's scope does not allow: " + ", ".join(named[:5])
            + (f" (+{len(named) - 5} more)" if len(named) > 5 else ""),
        )
    integration_ref = repo.integration_ref(record.run_id, integration_id)
    repo.ensure_ref(integration_ref, commit)
    record = store.update_integration(
        integration_id,
        expected=IntegrationState.PREPARING,
        mode=mode,
        integration_commit=commit,
        integration_tree=tree,
        integration_ref=integration_ref,
        paths=paths,
    )

    # --- the approved checks, run on the integration commit in a worktree of its own -------
    # The path is recorded before the worktree exists, so a process that dies in between leaves
    # a record that names what reconcile has to look for, never an unrecorded directory.
    record = store.update_integration(
        integration_id,
        expected=IntegrationState.PREPARING,
        worktree_path=str(repo.worktree_parent() / integration_id),
    )
    worktree = repo.create_worktree(integration_id, commit)
    record = store.update_integration(
        integration_id,
        expected=IntegrationState.PREPARING,
        state=IntegrationState.CHECKING,
        worktree_path=str(worktree),
        worktree_state="PRESENT",
    )
    fingerprint = candidate_fingerprint(worktree, spec.scope)

    def artifact_dir(check_id: str, evidence_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in check_id) or "check"
        directory = data_dir / "artifacts" / (evidence_id or "unknown") / safe
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    verification = verify_candidate(
        store=store,
        spec=spec,
        project=project,
        project_root=worktree,
        project_checks_digest=record.checks_digest,
        candidate_fingerprint=fingerprint,
        attempt_id=record.attempt_id,
        run_id=record.run_id,
        runners=runners,
        artifact_factory=artifact_dir,
        # An integration tree is checked by an execution of its own, every time.
        force_refresh=True,
        remove_bytecode=True,
        committed_bytecode=repo.committed_bytecode_paths(commit),
        evidence_kind=EVIDENCE_KIND,
    )
    after = candidate_fingerprint(worktree, spec.scope)
    tracked = repo.run(
        "status", "--porcelain=v1", "-z", "--untracked-files=no", cwd=worktree
    ).strip("\x00")
    problems: list[str] = []
    if verification.status == "failed":
        problems.append(f"approved checks failed on the integration tree: {verification.detail}")
    if after != fingerprint:
        problems.append(
            "the scoped files changed while the checks ran, so the evidence does not describe the "
            "integration commit"
        )
    if tracked:
        problems.append(
            "a check modified tracked files in the integration worktree, so the evidence does not "
            "describe the integration commit"
        )
    worktree_state, removal = _release_worktree(repo, record)
    if removal:
        notes.append(removal)
    if verification.status == "not_run":
        notes.append(
            "the task's acceptance names no check, so nothing was run on the integration tree "
            "(the run was accepted the same way)"
        )
    if problems:
        state = IntegrationState.CHECKS_FAILED
        detail = "; ".join(problems)
    else:
        state = IntegrationState.READY
        detail = (
            f"integration commit {commit} ({mode}) on {record.target_ref} at {tip} passed "
            f"{len(verification.evidence_ids)} approved check(s) in its own worktree"
        )
    record = store.update_integration(
        integration_id,
        expected=IntegrationState.CHECKING,
        state=state,
        fingerprint=fingerprint,
        evidence_ids=list(verification.evidence_ids),
        worktree_state=worktree_state,
        detail=detail[:2000],
    )
    outcome = IntegrationOutcome(record=record, notes=[*notes, detail])
    if state is IntegrationState.READY:
        outcome.handoff = _ready_handoff(store, repo, record)
    return outcome


def _integration_worktrees(repo: GitRepo, record: IntegrationRecord) -> list[Path]:
    """Registered worktrees this integration may have created, including a staging path.

    ``create_worktree`` adds ``<id>.staging-<pid>`` and then renames it to ``<id>``, so a process
    that died in between leaves a registration under the staging name.
    """
    parent = repo.worktree_parent().resolve()
    found: list[Path] = []
    for block in repo.worktree_blocks():
        recorded = block.get("worktree")
        if not recorded:
            continue
        path = Path(recorded).resolve()
        if path.parent == parent and (
            path.name == record.integration_id
            or path.name.startswith(f"{record.integration_id}.staging-")
        ):
            found.append(path)
    return found


def _release_worktree(repo: GitRepo, record: IntegrationRecord) -> tuple[str, str]:
    """Remove the integration's worktree(s) without force. Returns (worktree_state, note).

    Looked up by registration, not by the recorded state alone: a process can die after
    ``worktree add`` and before recording it, or after the removal and before recording that.
    """
    if record.worktree_state in {"REMOVED", "LEFT"}:
        return record.worktree_state, ""
    registered = _integration_worktrees(repo, record)
    if not registered:
        recorded = Path(record.worktree_path) if record.worktree_path else None
        if recorded is not None and recorded.exists():
            return "LEFT", (
                f"{recorded} exists but is not a worktree registered with this repository; it "
                "was left untouched"
            )
        return ("REMOVED" if record.worktree_state == "PRESENT" else "NONE"), ""
    left: list[str] = []
    for path in registered:
        if repo.is_main_worktree(path):  # pragma: no cover - never named after an integration
            left.append(f"{path} is the repository's main worktree")
            continue
        try:
            repo.remove_worktree_checked(path)
        except GitError as exc:
            left.append(f"{path} ({exc})")
    if left:
        return "LEFT", (
            "the integration worktree was left in place: `git worktree remove` (without --force) "
            "refused " + "; ".join(left) + ". It holds no delivery - the integration commit is "
            "kept by its ref - so it can be removed by hand once inspected"
        )
    return "REMOVED", ""


#: Characters PowerShell reads inside a double-quoted string: ``$`` and the backtick expand, and
#: the typographic double quotes end the string as ``"`` does.
_POWERSHELL_DOUBLE_QUOTED_SPECIAL = frozenset('"`$\u201c\u201d\u201e')
#: The characters PowerShell takes for a single quote; doubling one keeps it literal.
_POWERSHELL_SINGLE_QUOTES = frozenset("'\u2018\u2019\u201a\u201b")


def _shell_word(text: str, *, windows: bool = os.name == "nt") -> str:
    """The path ``text`` as one word of a command an operator pastes into their shell.

    A printed command is for PowerShell on Windows (PowerShell 7 or Windows PowerShell 5.1) and
    for ``sh`` elsewhere - not for cmd.exe, which expands ``%VAR%`` inside double quotes and hands
    PowerShell's single quotes to the program as part of the argument. On Windows a path goes in
    double quotes unless it holds a character PowerShell would expand there or end the string at;
    then it goes in PowerShell's literal single quotes, each single quote doubled. A path ending
    in a backslash - a root, ``C:\\`` or ``\\\\server\\share\\``, is the only directory printed
    with one - gets a ``.`` appended first: the same directory, with no backslash before the
    closing quote. Windows PowerShell 5.1 re-quotes an argument that holds a space for the program,
    and a backslash there escapes that quote (observed: ``"C:\\my dir\\" next`` reached the program
    as the one argument ``C:\\my dir" next``). Elsewhere :func:`shlex.quote`.
    """
    if not windows:
        return shlex.quote(text)
    if text.endswith("\\"):
        text += "."
    if not any(char in _POWERSHELL_DOUBLE_QUOTED_SPECIAL for char in text):
        return f'"{text}"'
    doubled = (char * 2 if char in _POWERSHELL_SINGLE_QUOTES else char for char in text)
    return "'" + "".join(doubled) + "'"


def _named_data_dir(store: Store) -> str | None:
    """The data directory a printed command must name, or ``None`` when the default reaches it.

    ``--data-dir`` selects the ledger (``<data-dir>/hflow.sqlite``) an ``hflow`` command opens,
    and an integration exists only in the ledger it was prepared in. A command printed without
    it opens the default ledger (``HFLOW_DATA_DIR``, else the platform directory) and exits 4
    (unknown integration) for anyone who passed another one. Absolute, so the command works from
    any working directory.
    """
    path = getattr(store, "path", None)
    if path is None or str(path) == ":memory:":
        return None
    ledger = os.path.normcase(os.path.abspath(path))
    if ledger == os.path.normcase(os.path.abspath(database_path(default_data_dir()))):
        return None
    return os.path.dirname(os.path.abspath(path))


def _hflow_command(store: Store, *words: str) -> str:
    """A follow-up ``hflow`` command that can be pasted as printed.

    ``words`` are fixed subcommand names, validated ids and full object ids, which need no
    quoting. The data directory is appended when the ledger is not the default one
    (:func:`_named_data_dir`), quoted by :func:`_shell_word`.
    """
    command = ["hflow", *words]
    data_dir = _named_data_dir(store)
    if data_dir is not None:
        command += ["--data-dir", _shell_word(data_dir)]
    return " ".join(command)


def _apply_command(store: Store, record: IntegrationRecord) -> str:
    return _hflow_command(
        store, "integrate", "apply", record.integration_id, "--expect-target", record.target_tip
    )


def _ready_handoff(store: Store, repo: GitRepo, record: IntegrationRecord) -> list[str]:
    commands = [_apply_command(store, record)]
    checked_out = repo.checked_out_at(record.target_ref)
    if checked_out:
        commands = [
            f"git -C {_shell_word(str(checked_out[0]))} merge --ff-only "
            f"{record.integration_commit}",
            _hflow_command(store, "integrate", "reconcile", record.integration_id),
        ]
    return commands


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def apply_integration(
    store: Store,
    *,
    integration_id: str,
    expect_target: str,
    applied_by: str,
    controller_build: str,
) -> IntegrationOutcome:
    """Move the target branch to the integration commit, by compare-and-set only."""
    record = _record(store, integration_id)
    if record.state is IntegrationState.INTEGRATED:
        return IntegrationOutcome(
            record=record,
            receipt=store.integration_receipt(integration_id),
            notes=["already integrated; nothing was written"],
        )
    if record.state is IntegrationState.APPLYING:
        reconciled = reconcile_integration(
            store, integration_id=integration_id, controller_build=controller_build
        )
        if reconciled.record.state is not IntegrationState.READY:
            return reconciled
        record = reconciled.record
    if record.state is not IntegrationState.READY:
        raise _refuse(
            RefusalCode.NOT_INTEGRABLE,
            f"integration {integration_id} is {record.state.value}; only a ready integration can be "
            "applied (prepare a new one)",
        )
    if expect_target != record.target_tip:
        raise _refuse(
            RefusalCode.TARGET_MOVED,
            f"--expect-target {expect_target!r} is not the tip {record.target_tip} this integration "
            f"was built and checked against; nothing was written",
        )
    _run_receipt(store, record.run_id)  # the run must still be ACCEPTED with its receipt
    repo, _common = _repo_at(
        record.repo_root, record.git_common_dir, owner=f"integration {integration_id}"
    )
    commit = record.integration_commit
    if repo.ref_target(record.integration_ref) != commit:
        raise _refuse(
            RefusalCode.EVIDENCE_STALE,
            f"{record.integration_ref} no longer points at the integration commit {commit}; "
            "prepare a new integration",
        )
    _require_current_evidence(store, record)

    current = repo.ref_target(record.target_ref)
    if current != record.target_tip:
        return _settle_moved_target(
            store, repo, record, current, expected=IntegrationState.READY, build=controller_build
        )
    lock = Path(record.git_common_dir) / (record.target_ref + ".lock")
    if lock.exists():
        raise _refuse(
            RefusalCode.TARGET_CHECKED_OUT,
            f"{lock} exists: another Git process may be updating {record.target_ref}, or one "
            "crashed and left its lock. HFlow never deletes a Git lock file; nothing was written",
        )
    checked_out = repo.checked_out_at(record.target_ref)
    if checked_out:
        where = ", ".join(str(path) for path in checked_out)
        note = (
            f"{record.target_ref} is checked out at {where}. Moving it there would leave that "
            "checkout's index and files at the old commit while its HEAD names the new one, so "
            "HFlow did not move it. Merge it yourself in that checkout, then reconcile"
        )
        store.record_note(record.run_id, f"integration: {integration_id}: {note}")
        return IntegrationOutcome(
            record=record, notes=[note], handoff=_ready_handoff(store, repo, record)
        )

    identity = current_identity()
    try:
        record = store.update_integration(
            integration_id,
            expected=IntegrationState.READY,
            state=IntegrationState.APPLYING,
            apply_intent_at=utc_now(),
            applied_by=applied_by,
            owner_pid=identity.pid,
            owner_created=identity.created,
            owner_host=identity.host,
        )
    except IntegrationConflict as exc:
        raise _refuse(RefusalCode.NOT_INTEGRABLE, str(exc)) from exc
    try:
        repo.update_ref_cas(
            record.target_ref,
            commit,
            record.target_tip,
            reason=f"hflow integrate {integration_id} (run {record.run_id})",
        )
    except GitError as exc:
        observed = repo.ref_target(record.target_ref)
        if observed == record.target_tip:
            record = store.update_integration(
                integration_id,
                expected=IntegrationState.APPLYING,
                state=IntegrationState.READY,
                applied_by="",
                detail=f"the ref update failed and {record.target_ref} is unchanged: {exc}"[:2000],
            )
            return IntegrationOutcome(
                record=record, notes=[record.detail], handoff=[_apply_command(store, record)]
            )
        return _settle_moved_target(
            store,
            repo,
            record,
            observed,
            expected=IntegrationState.APPLYING,
            build=controller_build,
            basis="observed_after_interruption",
            error=str(exc),
        )
    receipt = _receipt(store, record, basis="hflow_ref_update", build=controller_build)
    try:
        record = store.finalize_integration(
            integration_id, expected=IntegrationState.APPLYING, receipt=receipt
        )
    except IntegrationConflict:
        # A reconcile settled this record between the update and here (it saw the target
        # contain the commit); the branch moved exactly once either way.
        current = _record(store, integration_id)
        return IntegrationOutcome(
            record=current,
            receipt=store.integration_receipt(integration_id),
            notes=[
                f"{record.target_ref} moved {record.target_tip} -> {commit} by compare-and-set; "
                f"another process recorded the result first ({current.state.value})"
            ],
        )
    notes = [f"{record.target_ref} moved {record.target_tip} -> {commit} by compare-and-set"]
    after = repo.checked_out_at(record.target_ref)
    if after:
        notes.append(
            f"{record.target_ref} was checked out at {', '.join(str(p) for p in after)} right "
            "after the update; that checkout's index and files may still show the old tip"
        )
    return IntegrationOutcome(record=record, receipt=receipt, notes=notes)


def _require_current_evidence(store: Store, record: IntegrationRecord) -> None:
    rows = {str(r["evidence_id"]): r for r in store.evidence_for(record.run_id, kind=EVIDENCE_KIND)}
    for evidence_id in record.evidence_ids:
        row = rows.get(evidence_id)
        if (
            row is None
            or str(row["status"]) != "passed"
            or str(row["candidate_fingerprint"]) != record.fingerprint
            or str(row["checks_digest"]) != record.checks_digest
        ):
            raise _refuse(
                RefusalCode.EVIDENCE_STALE,
                f"integration evidence {evidence_id} is missing or no longer describes the "
                "integration tree; prepare a new integration",
            )


def _settle_moved_target(
    store: Store,
    repo: GitRepo,
    record: IntegrationRecord,
    current: str | None,
    *,
    expected: IntegrationState,
    build: str,
    basis: str = "operator_merge_observed",
    error: str = "",
) -> IntegrationOutcome:
    """The target is not where the record expects it: integrated already, or stale."""
    suffix = f" (the ref update reported: {error})" if error else ""
    if _contains(repo, current, record.integration_commit):
        receipt = _receipt(store, record, basis=basis, build=build)
        record = store.finalize_integration(
            record.integration_id, expected=expected, receipt=receipt
        )
        return IntegrationOutcome(
            record=record,
            receipt=receipt,
            notes=[
                f"{record.target_ref} at {current} contains the integration commit "
                f"{record.integration_commit}; recorded as integrated ({basis}){suffix}"
            ],
        )
    record = store.update_integration(
        record.integration_id,
        expected=expected,
        state=IntegrationState.STALE,
        detail=(
            f"{record.target_ref} is at {current or '(deleted)'}, not the checked tip "
            f"{record.target_tip}, and does not contain the integration commit; nothing was "
            f"written to it{suffix}. Prepare a new integration against the current tip"
        )[:2000],
    )
    return IntegrationOutcome(record=record, notes=[record.detail])


def _receipt(
    store: Store, record: IntegrationRecord, *, basis: str, build: str
) -> IntegrationReceipt:
    _row, run_receipt, _spec = _run_receipt(store, record.run_id)
    limitations = [
        f"integrated into the local branch {record.target_ref} only; nothing was pushed, merged "
        "elsewhere or published",
        "the integration commit is authored by HFlow's fixed identity, not by a person",
    ]
    if record.mode == "replayed":
        limitations.append(
            f"the target had moved from the task's base {record.base_commit} to "
            f"{record.target_tip}; Git's three-way merge replayed the candidate's change onto it "
            "and the approved checks ran on the merged tree, which was not reviewed again (the "
            "review covered the task's base to the candidate)"
        )
    if basis == "operator_merge_observed":
        limitations.append(
            "HFlow did not move the target: it observed that the branch contains the integration "
            "commit, so someone merged it; how is not recorded"
        )
    elif basis == "observed_after_interruption":
        limitations.append(
            "the apply did not finish cleanly; the branch was observed afterwards to contain the "
            "integration commit"
        )
    if not record.evidence_ids:
        limitations.append(
            "the task's acceptance names no check, so no check ran on the integration tree"
        )
    return IntegrationReceipt(
        integration_id=record.integration_id,
        run_id=record.run_id,
        task_id=record.task_id,
        attempt_id=record.attempt_id,
        runtime_build=build,
        candidate=run_receipt.candidate,
        target_ref=record.target_ref,
        target_tip_before=record.target_tip,
        integration_commit=record.integration_commit,
        integration_tree=record.integration_tree,
        mode=record.mode or "squash",
        paths=list(record.paths),
        verification=VerificationResult(
            status="passed" if record.evidence_ids else "not_run",
            evidence_ids=list(record.evidence_ids),
            detail=f"phase integration-check over fingerprint {record.fingerprint}",
            workspace=record.worktree_path,
        ),
        delivery_state=DeliveryState.INTEGRATED,
        basis=basis,  # type: ignore[arg-type]
        integrated_at=utc_now(),
        applied_by=record.applied_by if basis != "operator_merge_observed" else "",
        limitations=limitations,
    )


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------


def _record(store: Store, integration_id: str) -> IntegrationRecord:
    record = store.integration(integration_id)
    if record is None:
        raise IntegrationNotFound(f"no integration has id {integration_id}")
    return record


def _owner_check(
    record: IntegrationRecord, *, owner_gone_attested: bool
) -> tuple[str | None, str, bool]:
    """(why the record must not be settled yet or ``None``, what the decision rests on, whether
    it rests on the operator's attestation).

    ``matching`` always refuses: that exact process runs. ``unknown`` (no creation time recorded,
    another host, a pid this user may not open) refuses unless the operator attests the process is
    gone; the attestation is recorded with the result, never presented as an observation.
    """
    if record.owner_pid is None:
        return None, "no process was recorded for it", False
    observed = probe(
        ProcessIdentity(
            pid=int(record.owner_pid), created=record.owner_created, host=record.owner_host
        )
    )
    if observed.verdict == "gone":
        return None, f"its process was observed gone ({observed.detail})", False
    if observed.verdict == "unknown" and owner_gone_attested:
        return (
            None,
            f"the operator attests its process is gone (HFlow could not tell: {observed.detail})",
            True,
        )
    doing = "applying" if record.state is IntegrationState.APPLYING else "preparing"
    words = "is still running" if observed.verdict == "matching" else "cannot be judged from here"
    hint = (
        "" if observed.verdict == "matching"
        else "; if you know it has exited, reconcile again with --owner-gone"
    )
    return (
        f"the process {doing} integration {record.integration_id} (pid {record.owner_pid} on "
        f"{record.owner_host}) {words} ({observed.detail}); nothing was written{hint}",
        "",
        False,
    )


def reconcile_integration(
    store: Store,
    *,
    integration_id: str,
    controller_build: str,
    owner_gone_attested: bool = False,
    attested_by: str = "",
) -> IntegrationOutcome:
    """Settle a record from what Git shows. Never re-runs the ref update or a check.

    A concurrent change to the same record (an apply finishing, another reconcile) is reported
    from the record as it now stands instead of escaping as an error.
    """
    try:
        return _reconcile(
            store,
            integration_id=integration_id,
            controller_build=controller_build,
            owner_gone_attested=owner_gone_attested,
            attested_by=attested_by,
        )
    except IntegrationConflict as exc:
        current = _record(store, integration_id)
        return IntegrationOutcome(
            record=current,
            receipt=store.integration_receipt(integration_id),
            notes=[f"another process changed this integration meanwhile ({exc}); shown as stored"],
        )


def _reconcile(
    store: Store,
    *,
    integration_id: str,
    controller_build: str,
    owner_gone_attested: bool,
    attested_by: str,
) -> IntegrationOutcome:
    record = _record(store, integration_id)
    if record.state in _SETTLED:
        return IntegrationOutcome(
            record=record,
            receipt=store.integration_receipt(integration_id),
            notes=[f"integration {integration_id} is {record.state.value}; nothing to reconcile"],
        )
    if record.state in {
        IntegrationState.PREPARING,
        IntegrationState.CHECKING,
        IntegrationState.APPLYING,
    }:
        alive, basis, attested = _owner_check(record, owner_gone_attested=owner_gone_attested)
        if alive is not None:
            raise IntegrationBusy(RefusalCode.RUN_CLAIMED_BY_OTHER, alive)
        if attested:
            store.record_note(
                record.run_id,
                f"integration: {integration_id}: reconciled after {basis}; attested by "
                f"{attested_by or 'unknown'} (OS user; recorded, not authenticated)",
            )
    repo, _common = _repo_at(
        record.repo_root, record.git_common_dir, owner=f"integration {integration_id}"
    )
    if record.state in {IntegrationState.PREPARING, IntegrationState.CHECKING}:
        worktree_state, removal = _release_worktree(repo, record)
        record = store.update_integration(
            integration_id,
            expected=[IntegrationState.PREPARING, IntegrationState.CHECKING],
            state=IntegrationState.INTERRUPTED,
            worktree_state=worktree_state,
            detail=(
                f"the process preparing this integration exited while it was {record.state.value}; "
                "no check result is used and the target was never written"
                + (f"; {removal}" if removal else "")
            )[:2000],
        )
        return IntegrationOutcome(record=record, notes=[record.detail])
    current = repo.ref_target(record.target_ref)
    if record.state is IntegrationState.APPLYING:
        if current == record.target_tip:
            record = store.update_integration(
                integration_id,
                expected=IntegrationState.APPLYING,
                state=IntegrationState.READY,
                applied_by="",
                detail=(
                    f"an apply was interrupted before {record.target_ref} moved; it is still at "
                    f"{record.target_tip}, so the integration is ready again"
                ),
            )
            return IntegrationOutcome(
                record=record, notes=[record.detail], handoff=_ready_handoff(store, repo, record)
            )
        return _settle_moved_target(
            store,
            repo,
            record,
            current,
            expected=IntegrationState.APPLYING,
            build=controller_build,
            basis="observed_after_interruption",
        )
    # READY: the operator may have merged it by hand, or the target may have moved on.
    if current == record.target_tip:
        return IntegrationOutcome(
            record=record,
            notes=[f"{record.target_ref} is still at {record.target_tip}; ready to apply"],
            handoff=_ready_handoff(store, repo, record),
        )
    return _settle_moved_target(
        store, repo, record, current, expected=IntegrationState.READY, build=controller_build
    )
