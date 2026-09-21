"""The deterministic controller: a small finite state machine, not a workflow DSL.

Division of labour (plan 4.2): this module owns state, locks, counters, fingerprints,
budget reservation, process lifecycle and evidence. A driver owns whatever the
Harness does. The controller never asks an agent whether a task is done, and never
lets one write a receipt.

Two invariants are worth reading the code for:

* budget is reserved *before* dispatch and in the same transaction as the state
  change, so "no money spent on a refused dispatch" is a database property;
* every result is applied as a compare-and-set on the run's current attempt, so a
  late answer from an old attempt is recorded as ``SUPERSEDED`` and changes nothing.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from .admission import validate_task_spec
from .authorization import AuthorizationRecord
from .contracts import (
    AttemptState,
    CancellationReceipt,
    CandidateSnapshot,
    CheckPhase,
    DeliveryState,
    EvidenceStatus,
    HarnessDriver,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    IsolationLevel,
    RefusalCode,
    RefusedError,
    ReconcileOutcome,
    ResultReceipt,
    ReviewResult,
    RunInspection,
    RunRequest,
    RunSummary,
    TaskSpec,
    TaskState,
    UsageFacts,
    VerificationResult,
    canonical_json,
)
from .ids import (
    new_attempt_id,
    new_invocation_id,
    new_reservation_id,
    new_run_id,
    parse_ts,
    utc_now,
)
from .paths import default_data_dir
from .review import REVIEW_MISSING, review_input_error
from .drivers.base import assert_driver_shape
from .drivers.acpx_dsh import ENV_ALLOW_WRITES
from .drivers.fake import ProcessGuard
from .gitworkspace import IGNORED_ARTIFACT_ALLOWLIST, CandidateFreeze, GitError, GitRepo, GitStatusParseError
from .store import RunNotFound, Store, StoreError
from .verify import CheckRunners, verify_candidate
from .workspace import candidate_fingerprint, changed_paths, manifest, paths_outside_scope

#: How long a reservation may stay open before it is considered abandoned.
RESERVATION_TTL_SECONDS = 1800


class RunOutcome:
    """What ``run`` returns. Mirrors ``ResultReceipt`` but also covers refusals."""

    def __init__(
        self,
        *,
        run_id: str,
        task_state: TaskState,
        phase: CheckPhase | None,
        delivery_state: DeliveryState,
        receipt: ResultReceipt | None = None,
        block_code: RefusalCode | None = None,
        block_reason: str | None = None,
        turns_reserved: int = 0,
        turns_limit: int = 0,
        implementer_invocations: int = 0,
        reviewer_invocations: int = 0,
        workspace_matches_receipt: bool | None = None,
        notes: list[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_state = task_state
        self.phase = phase
        self.delivery_state = delivery_state
        self.receipt = receipt
        self.block_code = block_code
        self.block_reason = block_reason
        self.turns_reserved = turns_reserved
        self.turns_limit = turns_limit
        self.implementer_invocations = implementer_invocations
        self.reviewer_invocations = reviewer_invocations
        #: ``None`` means it could not be evaluated (no receipt / workspace gone).
        #: This is a drift check against the recorded scope fingerprint, not re-verification.
        self.workspace_matches_receipt = workspace_matches_receipt
        self.notes = notes or []

    @property
    def driver_invocations(self) -> int:
        """Total driver processes started for this run (implementer + reviewer)."""
        return self.implementer_invocations + self.reviewer_invocations

    def __repr__(self) -> str:
        return (
            f"RunOutcome(run_id={self.run_id!r}, task_state={self.task_state.value}, "
            f"phase={self.phase.value if self.phase else None}, "
            f"implementer_invocations={self.implementer_invocations}, "
            f"reviewer_invocations={self.reviewer_invocations}, "
            f"block_code={self.block_code.value if self.block_code else None})"
        )


def _summary_from_row(row: object, *, workspace_matches_receipt: bool | None = None) -> RunSummary:
    data = dict(row)  # type: ignore[arg-type]
    return RunSummary(
        run_id=data["run_id"],
        task_id=data["task_id"],
        task_revision=data["task_revision"],
        spec_digest=data["spec_digest"],
        task_state=TaskState(data["task_state"]),
        phase=CheckPhase(data["phase"]) if data["phase"] else None,
        delivery_state=DeliveryState(data["delivery_state"]),
        claimed_by=data["claimed_by"],
        agent_turns_reserved=data["turns_reserved"],
        agent_turns_limit=data["turn_limit"],
        agent_turns_observed=data["turns_observed"],
        block_code=data["block_code"],
        block_reason=data["block_reason"],
        workspace_matches_receipt=workspace_matches_receipt,
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def workspace_drift(store: Store, run_id: str, project_root: Path) -> bool | None:
    """Read-only drift check used by ``status``/``report``.

    ``None`` when there is nothing to compare (no receipt, or the workspace is gone).
    ``False`` when the scoped files changed after acceptance, meaning the stored
    ``ACCEPTED`` status describes a historical candidate rather than the current tree.

    Limits, stated so the value is not over-read: the comparison reuses the candidate
    fingerprint recorded at acceptance - a content hash over the declared write scope.
    It is not a Git tree hash, not a whole-repository cache key, and running it does
    **not** re-verify anything.
    """
    row = store.get_run(run_id)
    if not row["receipt_json"] or not Path(project_root).exists():
        return None
    receipt = ResultReceipt.model_validate(json.loads(row["receipt_json"]))
    spec = TaskSpec.model_validate(json.loads(row["task_spec_json"]))
    root = Path(project_root)
    if spec.workspace.mode == "worktree":
        # The candidate lives in its worktree, so a change to the *user's* checkout is not
        # drift of this candidate. The worktree is identified by the frozen path.
        recorded = receipt.candidate.worktree
        if not recorded or not Path(recorded).exists():
            return None
        root = Path(recorded)
    return candidate_fingerprint(root, spec.scope) == receipt.candidate.fingerprint


class PreflightCheck(Protocol):
    """A zero-model check that must hold before any real Harness call.

    Ordering is the point: this runs *before* an authorization is consumed, before any
    credential is read and before a workspace is created. A stale or broken launch binding is
    a reason to stop, not a reason to spend a submission finding out.
    """

    def __call__(self) -> tuple[bool, str]: ...


class Controller:
    def __init__(
        self,
        store: Store,
        driver: HarnessDriver,
        *,
        controller_build: str,
        runners: CheckRunners | None = None,
        controller_id: str = "local-controller",
        reservation_ttl_seconds: int = RESERVATION_TTL_SECONDS,
        review_isolation: IsolationLevel = IsolationLevel.PROMPT_ONLY,
        data_dir: Path | None = None,
        authorization: AuthorizationRecord | None = None,
        preflight: PreflightCheck | None = None,
    ) -> None:
        self.store = store
        self.driver = driver
        self.controller_build = controller_build
        self.runners = runners or CheckRunners.offline_default()
        self.controller_id = controller_id
        self.reservation_ttl_seconds = reservation_ttl_seconds
        #: Set only for a real Harness run, and only from a user-provenance artifact. Its
        #: allowance is consumed transactionally immediately before each dispatch.
        self.authorization = authorization
        #: Obligatory when an authorization is present: proves the launch binding still works
        #: without spending a submission.
        self.preflight = preflight
        #: Scratch root a driver may use for invocation-scoped state. Deliberately outside
        #: the project checkout: scaffolding in the workspace would show up as a candidate
        #: change and dirty the tree under test.
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        #: Recorded isolation for reviews. The driver cannot raise it by claiming so.
        self.review_isolation = review_isolation
        #: Workspace the current run targets; set by ``run_task``. Only used for the
        #: read-only drift check in ``workspace_matches_receipt``.
        self.project_root: Path | None = None
        assert_driver_shape(driver)

    # -- public entry points -------------------------------------------------

    def run_task(self, request: RunRequest) -> RunOutcome:
        spec = request.task
        project = request.project
        project_root = Path(request.project_root)

        report = validate_task_spec(spec, project, project_root)
        if not report.ok:
            first = report.issues[0]
            raise RefusedError(first.code, f"task {spec.task_id} refused: {first.detail}")

        self.project_root = project_root
        spec_digest = spec.spec_digest()

        # --- real-run gate: the artifact is recorded first, then the zero-model preflight.
        # Nothing expensive happens for an unauthorized real run - no credential read, no
        # workspace, no budget reservation, no process. The artifact is registered *before*
        # the preflight so that even a refused attempt leaves an auditable record of what was
        # authorized and that nothing was spent. Its allowance is claimed later, at the moment
        # a dispatch is actually certain, so a duplicate submission (which correctly dispatches
        # nothing) cannot consume it.
        if self.authorization is not None:
            self.store.register_authorization(self.authorization.as_store_record())
            report = self._preflight_report()
            if not report[0]:
                raise RefusedError(
                    RefusalCode.NOT_IMPLEMENTED,
                    f"zero-model preflight failed for this execution binding: {report[1]}. "
                    "No authorization allowance was consumed and nothing was dispatched.",
                )

        existing = self.store.find_run_by_spec_digest(project.project_id, spec_digest)

        if existing is not None:
            run_id = existing["run_id"]
            state = TaskState(existing["task_state"])
            live_attempt = self.store.open_attempt(run_id)
            if state is TaskState.ACCEPTED:
                return self._outcome_for(run_id, notes=["identical TaskSpec: returning the existing accepted run"])
            if state in {TaskState.BLOCKED, TaskState.CANCELLED}:
                return self._outcome_for(
                    run_id,
                    notes=[f"identical TaskSpec: existing run is {state.value}, not re-dispatched"],
                )
            if live_attempt is not None:
                return self._outcome_for(
                    run_id,
                    notes=[
                        "identical TaskSpec already has a live attempt; no second dispatch "
                        "(explicit new-run override is deferred to M3)"
                    ],
                )
            # Admitted but never dispatched (for example an interrupted controller):
            # continue the existing run instead of opening a second one.
            if not self.store.claim_run(run_id, self.controller_id):
                raise RefusedError(
                    RefusalCode.RUN_CLAIMED_BY_OTHER,
                    f"run {run_id} is owned by another controller; one owner per project at a time",
                )
            return self._drive(run_id, request)

        run_id = new_run_id()
        row = self.store.create_run(
            run_id=run_id,
            project_id=project.project_id,
            spec=spec,
            spec_digest=spec_digest,
            controller_build=self.controller_build,
            checks_digest=project.checks_digest(),
            turn_limit=spec.budget.max_agent_turns,
            repair_limit=spec.budget.max_repair_cycles,
        )
        run_id = row["run_id"]  # a concurrent identical submit may have won the insert

        if not self.store.claim_run(run_id, self.controller_id):
            raise RefusedError(
                RefusalCode.RUN_CLAIMED_BY_OTHER,
                f"run {run_id} is owned by another controller; one owner per project at a time",
            )

        return self._drive(run_id, request)

    def resume(self, run_id: str) -> RunOutcome:
        """Continue the state machine. Never replays a prompt and never re-dispatches."""
        row = self.store.get_run(run_id)
        state = TaskState(row["task_state"])
        if state is TaskState.BLOCKED and row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value:
            self.reconcile(run_id)
            return self._outcome_for(
                run_id,
                notes=[
                    "reconciled an interrupted attempt; this build does not re-dispatch on an "
                    "unknown outcome - submit a new revision to proceed"
                ],
            )
        return self._outcome_for(
            run_id,
            notes=[f"resume is a no-op for a run in state {state.value} in this build"],
        )

    def reconcile(self, run_id: str) -> ReconcileOutcome:
        """Observe an interrupted attempt. Must not start new model work."""
        attempt = self.store.open_attempt(run_id)
        if attempt is None or not attempt["invocation_id"]:
            return ReconcileOutcome.NOT_STARTED
        result = self.driver.reconcile(attempt["invocation_id"])
        self.store.record_reconcile(attempt["attempt_id"], result.model_dump(mode="json"))
        return result.outcome

    def cancel(self, run_id: str) -> CancellationReceipt:
        """Request a stop: record the intent first, then ask the driver, then believe facts.

        Idempotent: a recorded intent plus a recorded receipt short-circuits. No prompt is
        sent, no budget is charged, and a confirmed stop is reported as a *local process*
        fact - never as a successful protocol cancellation or a known business result.
        """
        intent_at, existing_receipt = self.store.cancel_state(run_id)
        if existing_receipt is not None:
            return existing_receipt
        row = self.store.get_run(run_id)
        if TaskState(row["task_state"]) is TaskState.ACCEPTED:
            # History is not rewritten: a stop request after acceptance is recorded as a fact,
            # but it cannot un-accept a delivered candidate.
            receipt = CancellationReceipt(
                invocation_id="",
                status="confirmed_stopped",
                mechanism="none",
                local_process_stopped=True,
                detail="the run was already ACCEPTED and its candidate frozen; nothing was stopped "
                "and the delivery stands",
            )
            self.store.record_cancel_receipt(run_id, receipt)
            self.store.record_note(
                run_id, "a stop was requested after acceptance; the accepted candidate is unchanged"
            )
            return receipt
        intent_at = self.store.record_cancel_intent(run_id)

        attempt = self.store.open_attempt(run_id)
        if attempt is None or not attempt["invocation_id"]:
            receipt = CancellationReceipt(
                invocation_id="",
                status="confirmed_stopped",
                mechanism="none",
                local_process_stopped=True,
                detail="run cancelled before any invocation was dispatched",
            )
            self.store.record_cancel_receipt(run_id, receipt)
            self.store.set_blocked(
                run_id, RefusalCode.CANCELLED_BY_OPERATOR, f"cancelled before dispatch at {intent_at}"
            )
            return receipt

        receipt = self._driver_cancel(attempt["invocation_id"], attempt["attempt_id"])
        self.store.record_cancel_receipt(run_id, receipt)
        if receipt.status == "confirmed_stopped":
            try:
                self.store.finish_attempt(
                    run_id=run_id,
                    attempt_id=attempt["attempt_id"],
                    state=AttemptState.CANCELLED,
                    outcome=InvocationOutcome.CANCELLED,
                    result=receipt.model_dump(mode="json"),
                    block_code=RefusalCode.CANCELLED_BY_OPERATOR,
                )
            except StoreError:
                pass  # the attempt may already be terminal; the receipt is still recorded
            self.store.set_blocked(
                run_id,
                RefusalCode.CANCELLED_BY_OPERATOR,
                f"stop confirmed ({receipt.mechanism}); local execution stopped, business result unknown",
            )
        else:
            self.store.set_blocked(
                run_id,
                RefusalCode.OUTCOME_UNKNOWN,
                f"stop could not be confirmed: {receipt.status}; work may still be running",
            )
        return receipt

    def _driver_cancel(self, invocation_id: str, attempt_id: str) -> CancellationReceipt:
        """Prefer the handle API when the driver offers it; degrade to the plain contract."""
        handles = getattr(self.driver, "_handles", None)
        cancel_handle = getattr(self.driver, "cancel_handle", None)
        if isinstance(handles, dict) and callable(cancel_handle) and invocation_id in handles:
            try:
                return cancel_handle(handles[invocation_id])  # type: ignore[no-any-return]
            except Exception as exc:  # noqa: BLE001 - a broken stop must not look like success
                return CancellationReceipt(
                    invocation_id=invocation_id,
                    status="unknown",
                    mechanism="none",
                    detail=f"driver raised while stopping: {exc!r}",
                )
        try:
            return self.driver.cancel(invocation_id)
        except Exception as exc:  # noqa: BLE001
            return CancellationReceipt(
                invocation_id=invocation_id,
                status="unknown",
                mechanism="none",
                detail=f"driver raised while stopping: {exc!r}",
            )

    # -- the state machine ---------------------------------------------------

    def _preflight_report(self) -> tuple[bool, str]:
        if self.preflight is None:
            return False, "no zero-model preflight is wired for this execution binding"
        try:
            return self.preflight()
        except Exception as exc:  # noqa: BLE001 - a broken preflight is a refusal, not a crash
            return False, f"preflight raised {exc!r}"

    def _claim_submission(self, run_id: str, purpose: str) -> int:
        """Consume one authorized top-level submission, or refuse to dispatch.

        Called immediately before a real dispatch (implementer, or the separate reviewer
        invocation). The store's UPDATE is the gate, so a restart or a resubmission cannot
        restore allowance.
        """
        assert self.authorization is not None
        try:
            used = self.store.claim_authorized_submission(self.authorization.authorization_id)
        except StoreError as exc:
            raise RefusedError(RefusalCode.BUDGET_EXHAUSTED, str(exc)) from exc
        self.store.record_note(
            run_id,
            f"authorization {self.authorization.authorization_id}: consumed top-level submission "
            f"{used}/{self.authorization.max_top_level_submissions} for {purpose}",
        )
        return used

    def _drive(self, run_id: str, request: RunRequest) -> RunOutcome:
        spec = request.task
        project = request.project
        project_root = Path(request.project_root)
        attempt_id = new_attempt_id()
        invocation_id = new_invocation_id()
        reservation_id = new_reservation_id()

        # --- workspace preparation (M2): an isolated worktree, or the project in place
        repo: GitRepo | None = None
        worktree: Path | None = None
        user_tree_before = ""
        dirty_target = False
        if spec.workspace.mode == "worktree":
            try:
                repo = GitRepo.discover(project_root)
                user_tree_before = repo.user_change_fingerprint()
                base_commit = spec.workspace.base_commit or repo.resolve_commit("HEAD")
                if not repo.commit_exists(base_commit):
                    return self._blocked(
                        run_id,
                        RefusalCode.SCOPE_VIOLATION,
                        f"base commit {base_commit!r} does not exist in {repo.root}",
                    )
                worktree = repo.create_worktree(run_id, base_commit)
                self.store.record_worktree(run_id, worktree)
            except (GitError, RefusedError) as exc:
                return self._blocked(run_id, RefusalCode.INTERNAL_ERROR, f"git workspace failed: {exc}")
            if repo.is_dirty():
                # The user has uncommitted work. That is theirs: never stashed, reset or
                # committed. It is recorded on acceptance, where the note survives; the
                # isolated worktree already started from the base commit regardless.
                dirty_target = True
        execution_root = worktree or project_root

        self.store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY, idempotent=True)

        # A run that must change files needs the driver to allow it; a read-only probe does
        # not. This is decided from the run's own mode, recorded as a fact, and never silently
        # defaulted: an unexpected "deny" would look like an uncooperative agent.
        # Permission is decided per role, from the run's own mode plus an explicit local
        # opt-in, and recorded before dispatch. A reviewer never inherits an implementer's
        # write permission: `_review` passes writes_allowed=False unconditionally.
        allow_writes = os.environ.get(ENV_ALLOW_WRITES, "").strip().lower() in {"1", "true", "yes"}
        implementer_writes = allow_writes and spec.workspace.mode == "worktree"
        self.store.record_note(
            run_id,
            "effective permissions - implementer: "
            f"{'approve-all (all tool requests auto-approved)' if implementer_writes else 'approve-reads'}"
            " | reviewer: approve-reads",
        )
        if not implementer_writes:
            self.store.record_note(
                run_id,
                f"writes are off for this run (workspace.mode={spec.workspace.mode}, "
                f"{ENV_ALLOW_WRITES}='{os.environ.get(ENV_ALLOW_WRITES, '')}'); a task that needs "
                "to change files will block at verification instead of being granted permission "
                "implicitly",
            )

        # --- dispatch, with the authorized-submission claim and the budget gate
        if self.authorization is not None:
            self._claim_submission(run_id, "implementer invocation")

        # --- dispatch, with the budget gate and the dispatch intent in one transaction
        expires_at = (
            parse_ts(utc_now()) + timedelta(seconds=self.reservation_ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        try:
            self.store.dispatch_attempt(
                run_id=run_id,
                controller_id=self.controller_id,
                attempt_id=attempt_id,
                role="implementer",
                reservation_id=reservation_id,
                reserved_turns=1,
                reservation_expires_at=expires_at,
            )
        except StoreError as exc:
            return self._refuse(
                run_id,
                RefusalCode.BUDGET_EXHAUSTED
                if "budget exhausted" in str(exc)
                else RefusalCode.INTERNAL_ERROR,
                str(exc),
            )

        self.store.record_invocation(attempt_id, invocation_id)
        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )

        pre_fingerprint = candidate_fingerprint(execution_root, spec.scope)
        pre_manifest = manifest(execution_root)
        invocation = InvocationRequest(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            run_id=run_id,
            role="implementer",
            task_id=spec.task_id,
            task_revision=int(self.store.get_run(run_id)["task_revision"]),
            goal=spec.goal,
            acceptance=spec.acceptance,
            write_allow=list(spec.scope.write_allow),
            write_deny=list(spec.scope.write_deny),
            workspace=str(execution_root),
            deadline_seconds=request.deadline_seconds,
            spec_digest=spec.spec_digest(),
            writes_allowed=implementer_writes,
            data_dir=str(self.data_dir),
        )

        # From here on, failures must not re-dispatch: the model may already have run.
        try:
            result = self.driver.start(invocation)
        except RefusedError as exc:
            self._block_attempt(run_id, attempt_id, exc.code, str(exc))
            return self._outcome_for(run_id)
        except Exception as exc:  # noqa: BLE001 - controller must not hot-fix a driver
            self._block_attempt(run_id, attempt_id, RefusalCode.INTERNAL_ERROR, repr(exc))
            return self._outcome_for(run_id)

        if result.outcome is InvocationOutcome.OUTCOME_UNKNOWN:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.OUTCOME_UNKNOWN,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                result=result.model_dump(mode="json"),
                block_code=RefusalCode.OUTCOME_UNKNOWN,
            )
            return self._blocked(
                run_id,
                RefusalCode.OUTCOME_UNKNOWN,
                "the worker's result is unknown; no re-dispatch until an operator reconciles "
                "(plan 9.3)",
            )

        if result.agent_turns is not None:
            self.store.set_turns_observed(run_id, result.agent_turns)

        if result.outcome is not InvocationOutcome.COMPLETED:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=result.outcome,
                result=result.model_dump(mode="json"),
                block_code=RefusalCode.DRIVER_FAILED,
            )
            return self._blocked(
                run_id,
                RefusalCode.DRIVER_FAILED,
                f"driver reported {result.outcome.value}: {result.error_message or 'no detail'}",
            )

        self.store.finish_attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            state=AttemptState.SUCCEEDED,
            outcome=result.outcome,
            result=result.model_dump(mode="json"),
        )

        # --- freeze the candidate the controller actually observed -------------
        post_fingerprint = candidate_fingerprint(execution_root, spec.scope)
        outside = paths_outside_scope(
            changed_paths(pre_manifest, manifest(execution_root)), spec.scope
        )
        if outside:
            return self._blocked(
                run_id,
                RefusalCode.SCOPE_VIOLATION,
                "the worker changed files its TaskSpec did not authorize: "
                + ", ".join(outside[:5])
                + (f" (+{len(outside) - 5} more)" if len(outside) > 5 else ""),
            )

        # Freeze an explicit Git identity for the candidate before any check runs, so the
        # receipt names a commit rather than only a content fingerprint.
        freeze: CandidateFreeze | None = None
        if repo is not None and worktree is not None:
            try:
                freeze = repo.freeze_candidate(
                    worktree,
                    list(spec.scope.write_allow),
                    f"hflow: candidate for {spec.task_id}",
                    allow_ignored=IGNORED_ARTIFACT_ALLOWLIST,
                )
                # Keep the candidate reachable independently of its worktree: a bare commit
                # SHA is an identifier, not a retention policy.
                ref = repo.candidate_ref(run_id, attempt_id)
                ref_status = repo.ensure_candidate_ref(ref, freeze.candidate_commit)
                self.store.record_note(run_id, f"candidate ref {ref} ({ref_status})")
            except GitError as exc:
                return self._blocked(run_id, RefusalCode.INTERNAL_ERROR, f"candidate freeze failed: {exc}")
            except GitStatusParseError as exc:
                return self._blocked(run_id, RefusalCode.SCOPE_VIOLATION, f"candidate freeze refused: {exc}")

        self.store.advance_to_checking(run_id=run_id, attempt_id=attempt_id, phase=CheckPhase.VERIFICATION)
        verification = verify_candidate(
            store=self.store,
            spec=spec,
            project=project,
            project_root=execution_root,
            project_checks_digest=project.checks_digest(),
            candidate_fingerprint=post_fingerprint,
            attempt_id=attempt_id,
            run_id=run_id,
            runners=self.runners,
        )

        review = ReviewResult(status="not_run")
        if verification.status != "passed":
            review = ReviewResult(status="not_run", isolation=self.review_isolation)
        elif spec.needs_review(project):
            self.store.set_task_state(
                run_id,
                [TaskState.CHECKING],
                TaskState.CHECKING,
                phase=CheckPhase.REVIEW,
            )
            try:
                review = self._review(
                    run_id, spec, execution_root, post_fingerprint, project.checks_digest()
                )
            except RefusedError as exc:
                return self._blocked(run_id, exc.code, exc.message)
        else:
            review = ReviewResult(status="not_required", isolation=IsolationLevel.NONE)

        if verification.status != "passed":
            return self._blocked(
                run_id,
                RefusalCode.VERIFICATION_FAILED,
                f"the process exited successfully but acceptance is not met: {verification.detail}",
            )
        if review.status == "changes_requested":
            return self._blocked(
                run_id,
                RefusalCode.REVIEW_REJECTED,
                "independent review requested changes; automatic repair is deferred to M3",
            )
        if review.status not in {"accepted", "not_required"}:
            return self._blocked(
                run_id,
                RefusalCode.INTERNAL_ERROR,
                f"unexpected review status {review.status!r}",
            )

        return self._accept(
            run_id=run_id,
            attempt_id=attempt_id,
            spec=spec,
            project_root=execution_root,
            verification=verification,
            review=review,
            observed_turns=result.agent_turns,
            base_ref=result.candidate.base_ref if result.candidate else "",
            limitations=list(result.limitations),
            freeze=freeze,
            repo=repo,
            target_repo_root=project_root,
            user_tree_before=user_tree_before,
            dirty_target=dirty_target,
        )

    # -- steps ---------------------------------------------------------------

    def _review(
        self,
        run_id: str,
        spec: TaskSpec,
        project_root: Path,
        candidate_fp: str,
        checks_digest: str,
    ) -> ReviewResult:
        """Buy the review turn, run the reviewer, then interpret its structured verdict.

        Two things this deliberately does *not* do:

        * it does not let the reviewer's prose set the isolation level (A08) - the level
          is whatever the driver could actually enforce, recorded by the controller;
        * it does not spend a review turn when budget cannot cover it (A03): the
          reservation refuses and the run blocks with no reviewer process started.
        """
        attempt = self.store.open_attempt(run_id)
        attempt_id = attempt["attempt_id"] if attempt else ""
        if self.authorization is not None:
            # The reviewer is its own invocation and its own top-level submission.
            self._claim_submission(run_id, "reviewer invocation")
        try:
            self.store.reserve_review_turn(run_id, self.controller_id)
        except StoreError as exc:
            raise RefusedError(RefusalCode.BUDGET_EXHAUSTED, str(exc)) from exc

        invocation_id = new_invocation_id()
        self.store.record_review_invocation(attempt_id, invocation_id)
        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )
        row = self.store.get_run(run_id)
        review_request = InvocationRequest(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            run_id=run_id,
            role="reviewer",
            task_id=spec.task_id,
            task_revision=int(row["task_revision"]),
            goal=(
                "Review the frozen candidate against the acceptance criteria. "
                "Report findings against the candidate fingerprint; do not edit files."
            ),
            acceptance=spec.acceptance,
            write_allow=[],
            write_deny=list(spec.scope.write_allow),
            workspace=str(project_root),
            deadline_seconds=900,
            spec_digest=row["spec_digest"],
            # A reviewer is read-only, always: it checks what the implementer produced, and an
            # approval for the implementer to write never extends to the review invocation.
            writes_allowed=False,
            data_dir=str(self.data_dir),
        )
        try:
            review_invocation = self.driver.start(review_request)
        except Exception as exc:  # noqa: BLE001 - a broken reviewer must not become an accept
            self.store.attach_review_result(
                attempt_id, {"error": repr(exc), "invocation_id": invocation_id}
            )
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the review invocation could not be started, so no verdict exists: " f"{exc!r}",
            ) from exc

        self.store.attach_review_result(attempt_id, review_invocation.model_dump(mode="json"))
        if review_invocation.outcome is not InvocationOutcome.COMPLETED:
            # An unfinished turn is a transport failure, not the reviewer's judgment. It is
            # reported as such; a genuine rejection requires a validated `changes_requested`.
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the review invocation did not complete "
                f"({review_invocation.outcome.value}: "
                f"{review_invocation.error_message or 'no detail'}), so it produced no verdict",
            )

        review_output = review_invocation.review
        if review_output is None:
            return self._unusable_review(
                run_id=run_id,
                attempt_id=attempt_id,
                invocation=review_invocation,
                candidate_fp=candidate_fp,
                checks_digest=checks_digest,
            )

        evidence = self.store.record_evidence(
            evidence_id=self._new_evidence_id(),
            run_id=run_id,
            attempt_id=attempt_id,
            kind="review",
            status=EvidenceStatus.PASSED
            if review_output.verdict == "accepted"
            else EvidenceStatus.FAILED,
            candidate_fingerprint=candidate_fp,
            checks_digest=checks_digest,
            check_id="review",
            detail=canonical_json(review_output.model_dump(mode="json")),
        )
        return ReviewResult(
            status="accepted" if review_output.verdict == "accepted" else "changes_requested",
            isolation=self.review_isolation,
            evidence_ids=[evidence.evidence_id],
            checked_fingerprint=candidate_fp,
        )

    def _unusable_review(
        self,
        *,
        run_id: str,
        attempt_id: str,
        invocation: InvocationResult,
        candidate_fp: str,
        checks_digest: str,
    ) -> ReviewResult:
        """A completed reviewer turn whose verdict never arrived. Refuse, and say why.

        The run stays BLOCKED and still gets no receipt, exactly as before - what changes is
        the record: a missing/invalid/ambiguous verdict is a *wire* failure, so it is
        recorded as failed review evidence with a protocol reason instead of being described
        as the reviewer requesting changes. Nothing here can accept a candidate.
        """
        limitations = list(invocation.limitations)
        wire_error = review_input_error(limitations)
        if wire_error is None:
            kind, detail = (
                REVIEW_MISSING,
                "the driver reported a completed reviewer turn with no structured verdict and "
                "no explanation for its absence",
            )
        else:
            kind, detail = wire_error
        if kind == "unbound":
            # A turn whose completion is not bound to its own prompt is not a usable result:
            # it must be reconciled, never treated as an answer from this invocation.
            raise RefusedError(
                RefusalCode.OUTCOME_UNKNOWN,
                f"the reviewer's completion is not bound to its prompt ({detail}); "
                "no verdict can be trusted and nothing is re-dispatched",
            )
        self.store.record_evidence(
            evidence_id=self._new_evidence_id(),
            run_id=run_id,
            attempt_id=attempt_id,
            kind="review",
            status=EvidenceStatus.ERROR,
            candidate_fingerprint=candidate_fp,
            checks_digest=checks_digest,
            check_id="review",
            detail=(
                f"no usable structured verdict from the review invocation: {kind}: {detail}"
            ),
        )
        raise RefusedError(
            RefusalCode.REVIEW_PROTOCOL_ERROR,
            f"the reviewer produced no usable structured verdict ({kind}: {detail}); "
            "acceptance refuses, and this is not a substantive review rejection",
        )

    def _accept(
        self,
        *,
        run_id: str,
        attempt_id: str,
        spec: TaskSpec,
        project_root: Path,
        verification: VerificationResult,
        review: ReviewResult,
        observed_turns: int | None,
        base_ref: str,
        limitations: list[str],
        freeze: CandidateFreeze | None = None,
        repo: GitRepo | None = None,
        target_repo_root: Path | None = None,
        user_tree_before: str = "",
        dirty_target: bool = False,
    ) -> RunOutcome:
        """Admission gate. Every field of the receipt is re-derived from stored facts."""
        row = self.store.get_run(run_id)
        if row["current_attempt_id"] != attempt_id:
            return self._outcome_for(
                run_id,
                notes=["attempt was superseded before acceptance; no receipt was written"],
            )
        if row["cancel_intent_at"]:
            return self._blocked(
                run_id,
                RefusalCode.CANCELLED_BY_OPERATOR,
                "a cancellation intent was recorded; a late success cannot be accepted "
                f"(intent at {row['cancel_intent_at']})",
            )

        fresh_fingerprint = candidate_fingerprint(project_root, spec.scope)
        evidence_rows = [dict(r) for r in self.store.evidence_for(run_id, kind="verification")]
        current_ids = {
            r["evidence_id"]
            for r in evidence_rows
            if r["status"] == EvidenceStatus.PASSED.value
            and r["candidate_fingerprint"] == fresh_fingerprint
            and r["checks_digest"] == row["checks_digest"]
        }
        missing = [eid for eid in verification.evidence_ids if eid not in current_ids]
        if missing:
            return self._blocked(
                run_id,
                RefusalCode.EVIDENCE_STALE,
                "the candidate changed after verification, so the recorded evidence no longer "
                f"applies (stale evidence: {', '.join(missing)})",
            )

        receipt = ResultReceipt(
            run_id=run_id,
            task_id=spec.task_id,
            attempt_id=attempt_id,
            task_revision=int(row["task_revision"]),
            runtime_build=self.controller_build,
            plan_digest=row["spec_digest"],
            harness_outcome=InvocationOutcome.COMPLETED,
            candidate=CandidateSnapshot(
                base_commit=(freeze.base_commit if freeze else base_ref)
                or f"base:{row['spec_digest'][:18]}",
                git_commit=freeze.candidate_commit if freeze else "",
                git_tree=freeze.tree if freeze else "",
                worktree=str(project_root) if freeze else "",
                fingerprint=fresh_fingerprint,
            ),
            candidate_paths=list(freeze.paths) if freeze else [],
            verification=verification,
            review=review,
            task_state=TaskState.ACCEPTED,
            delivery_state=DeliveryState.LOCAL_CANDIDATE,
            usage=UsageFacts(
                controller_turns_reserved=int(row["turns_reserved"]),
                controller_turns_observed=observed_turns,
                provider_billed_tokens=None,
                provider_cost=None,
            ),
            limitations=[
                *limitations,
                *(
                    [
                        "candidate is a real Git commit in a detached worktree; nothing was "
                        "merged, pushed or published",
                        "the target repository's own uncommitted changes were not stashed or "
                        "modified (they are the user's)",
                    ]
                    if freeze
                    else [
                        "candidate snapshot is a workspace fingerprint, not a Git tree hash "
                        "(this run did not use a Git worktree)",
                    ]
                ),
                "review ran in the implementer's invocations' workspace; its isolation is not "
                "independently enforced",
            ],
        )

        self.store.finalize_acceptance(run_id, receipt, checks_digest=row["checks_digest"])

        # Post-acceptance facts go into the note *after* the terminal transition, because the
        # transition itself clears block_reason. The target repository's user-visible state is
        # checked here: a run must not have touched it.
        if repo is not None:
            if spec.workspace.mode == "worktree" and dirty_target:
                self.store.record_note(
                    run_id,
                    "target repository had uncommitted changes; they were left untouched "
                    "(not stashed, not committed) and the candidate came from an isolated worktree",
                )
            if user_tree_before and repo.user_change_fingerprint() != user_tree_before:
                self.store.record_note(
                    run_id,
                    "WARNING: the target repository's own state changed during the run; the "
                    "candidate is unaffected, but the user's tree needs a look",
                )
        return self._outcome_for(run_id)

    # -- helpers -------------------------------------------------------------

    def _new_evidence_id(self) -> str:
        from .ids import new_evidence_id

        return new_evidence_id()

    def _block_attempt(
        self, run_id: str, attempt_id: str, code: RefusalCode, reason: str
    ) -> None:
        try:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=InvocationOutcome.FAILED,
                result={"error": reason},
                block_code=code,
            )
        except StoreError:
            pass
        self.store.set_blocked(run_id, code, reason)

    def _blocked(self, run_id: str, code: RefusalCode, reason: str) -> RunOutcome:
        self.store.set_blocked(run_id, code, reason)
        return self._outcome_for(run_id)

    def _refuse(self, run_id: str, code: RefusalCode, reason: str) -> RunOutcome:
        return self._blocked(run_id, code, reason)

    def _outcome_for(self, run_id: str, notes: list[str] | None = None) -> RunOutcome:
        try:
            row = self.store.get_run(run_id)
        except RunNotFound as exc:
            raise RefusedError(RefusalCode.INTERNAL_ERROR, f"unknown run {run_id}") from exc
        implementer, reviewer = self.store.invocation_counts(run_id)
        receipt = (
            ResultReceipt.model_validate(json.loads(row["receipt_json"]))
            if row["receipt_json"]
            else None
        )
        return RunOutcome(
            run_id=run_id,
            task_state=TaskState(row["task_state"]),
            phase=CheckPhase(row["phase"]) if row["phase"] else None,
            delivery_state=DeliveryState(row["delivery_state"]),
            receipt=receipt,
            block_code=RefusalCode(row["block_code"]) if row["block_code"] else None,
            block_reason=row["block_reason"],
            turns_reserved=int(row["turns_reserved"]),
            turns_limit=int(row["turn_limit"]),
            implementer_invocations=implementer,
            reviewer_invocations=reviewer,
            workspace_matches_receipt=self.workspace_matches_receipt(run_id),
            notes=notes,
        )

    def workspace_matches_receipt(self, run_id: str) -> bool | None:
        """Alias of :func:`workspace_drift` bound to this controller's workspace."""
        if self.project_root is None:
            return None
        return workspace_drift(self.store, run_id, self.project_root)

def inspect_run(store: Store, run_id: str, *, project_root: Path | None = None) -> RunInspection:
    """Read-only projection for ``status``/``report``. Zero model calls, by design."""
    from .contracts import AttemptRecord, EvidenceRecord

    row = store.get_run(run_id)
    drift = workspace_drift(store, run_id, project_root) if project_root is not None else None
    attempts = [
        AttemptRecord(
            attempt_id=a["attempt_id"],
            run_id=a["run_id"],
            task_revision=a["task_revision"],
            role=a["role"],
            state=AttemptState(a["state"]),
            reservation_id=a["reservation_id"],
            reserved_agent_turns=a["reserved_agent_turns"],
            reserved_expires_at=a["reserved_expires_at"],
            process_id=a["process_id"],
            process_started_at=a["process_started_at"],
            process_identity=a["process_identity"],
            session_id=a["session_id"],
            invocation_id=a["invocation_id"],
            review_invocation_id=a["review_invocation_id"],
            outcome=InvocationOutcome(a["outcome"]) if a["outcome"] else None,
            result_digest=a["result_digest"],
            block_code=a["block_code"],
            created_at=a["created_at"],
            finished_at=a["finished_at"],
        )
        for a in store.attempts_for(run_id)
    ]
    evidence = [
        EvidenceRecord(
            evidence_id=e["evidence_id"],
            run_id=e["run_id"],
            attempt_id=e["attempt_id"],
            kind=e["kind"],
            status=EvidenceStatus(e["status"]),
            check_id=e["check_id"] or "",
            candidate_fingerprint=e["candidate_fingerprint"],
            checks_digest=e["checks_digest"],
            command=json.loads(e["command_json"]),
            exit_code=e["exit_code"],
            stdout_digest=e["stdout_digest"],
            stderr_digest=e["stderr_digest"],
            detail=e["detail"],
            created_at=e["created_at"],
        )
        for e in store.evidence_for(run_id)
    ]
    return RunInspection(
        run=_summary_from_row(row, workspace_matches_receipt=drift),
        task_spec=TaskSpec.model_validate(json.loads(row["task_spec_json"])),
        attempts=attempts,
        evidence=evidence,
        receipt=ResultReceipt.model_validate(json.loads(row["receipt_json"]))
        if row["receipt_json"]
        else None,
        model_calls_made=0,
    )
