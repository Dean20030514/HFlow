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
import re
from datetime import timedelta
from pathlib import Path
from typing import Callable, Protocol

from .admission import predictable_dispatch_problems, validate_task_spec
from .authorization import AuthorizationRecord
from .contracts import (
    AttemptState,
    CancellationReceipt,
    CandidateSnapshot,
    CheckPhase,
    DeliveryState,
    DispatchReservation,
    EffectiveConfig,
    EvidenceStatus,
    HarnessDriver,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    InvocationStartState,
    IsolationLevel,
    LaunchConfig,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    ReconcileOutcome,
    ResultReceipt,
    ReviewResult,
    RootBudgetBinding,
    RootBudgetLimits,
    RunInspection,
    RunRequest,
    RunSummary,
    SpawnFact,
    SpawnReporter,
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
from .packet import (
    PacketTooLargeError,
    RenderedPacket,
    render_implementer_packet,
    render_reviewer_packet,
)
from .paths import default_data_dir
from .prepare import resolve_permissions
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

#: A role-packet renderer: the signature every renderer in ``hflow.packet`` shares.
Renderer = Callable[..., RenderedPacket]


class PreparedPacket:
    """One implementer packet rendered for one run identity, plus the workspace it names.

    The workspace is kept next to the packet so ``_drive`` can tell whether the packet it was
    handed describes the workspace the run actually got: if the path changed (a lost insert
    race, a different worktree), the packet is re-rendered rather than sent under a stale path.
    """

    __slots__ = ("packet", "run_id", "workspace")

    def __init__(self, *, run_id: str, packet: RenderedPacket) -> None:
        self.run_id = run_id
        self.packet = packet
        self.workspace = _packet_workspace(packet)


def _packet_workspace(packet: RenderedPacket) -> str:
    """The workspace path a rendered implementer packet names, or "" when it cannot be read.

    Read back out of the packet rather than passed alongside it: a second copy of the path is
    one more thing that can disagree with what the agent is told.
    """
    for line in packet.text.splitlines():
        if line.startswith("- work in this directory: "):
            return line[len("- work in this directory: ") :].strip()
    return ""


#: Field markers an evidence row's detail text uses. A reference value ends where the next marker
#: begins - which is also what lets a path contain spaces: the path is not split on whitespace,
#: because "C:/temp/run 1/artifact.json" is one value, not two tokens.
_REFERENCE_MARKERS = (
    "reason=",
    "artifact=",
    "stdout:",
    "stdout=",
    "stderr:",
    "stderr=",
    "env_names=",
    "withheld_secret_like=",
)

#: Both spellings are accepted on purpose. The evidence row writes ``stdout: 3000/3000 bytes``
#: (reading like a log line) and the reviewer packet writes ``stdout=3000/3000B``; a reader that
#: matched only one of them returned "no evidence" for a row that had it. The separator and the
#: optional ``B`` are the whole difference, so the parser does not depend on which produced it.
_STREAM_REFERENCE = re.compile(
    r"^(?P<retained>\d+)\s*/\s*(?P<total>\d+)\s*B?(?:\s*bytes)?"
    r"\s+truncated=(?P<truncated>\w+)"
    r"\s+digest=(?P<digest>\S+)"
)


def _reference_field(detail: str, field: str) -> str:
    """One ``field=value`` (or ``field: value``) reference out of an evidence row's detail text.

    The value runs to the next known marker (or to the end). Splitting on spaces would cut a path
    that contains one, which is exactly how a real temp directory looks: the reviewer would be
    handed "C:/temp/run" for "C:/temp/run 1".
    """
    for marker in (f"{field}=", f"{field}:"):
        start = detail.find(marker)
        while start != -1:
            # Only a marker at a field boundary counts: "reason=" inside a word is not a field.
            if start == 0 or detail[start - 1] == " ":
                break
            start = detail.find(marker, start + 1)
        if start == -1:
            continue
        value_start = start + len(marker)
        end = len(detail)
        for other in _REFERENCE_MARKERS:
            if other == marker:
                continue
            position = detail.find(other, value_start)
            # The next field starts where the marker does, separated from this value by a space -
            # or immediately, which is how the packet writes "…/3000B truncated=False".
            if position != -1 and (position == value_start or detail[position - 1] == " "):
                end = min(end, position)
        return detail[value_start:end].strip()
    return ""


def _stream_reference(detail: str, name: str) -> dict[str, object]:
    """The retained-bytes/truncated/digest facts for one stream, out of the detail text."""
    value = _reference_field(detail, name)
    if not value:
        return {}
    match = _STREAM_REFERENCE.match(value)
    if match is None:
        return {}
    return {
        "retained_bytes": match.group("retained"),
        "total_bytes": match.group("total"),
        "truncated": match.group("truncated"),
        "digest": match.group("digest"),
    }

#: Note prefixes written by the controller. They are read back mechanically (by tests and by
#: an operator grepping a run), so they are constants rather than inline strings.
NOTE_PACKET = "role_input_packet"
NOTE_PROMPT_DIGEST = "prompt_digest"
#: Which role's invocation a stop was routed to, and what it reported. Recorded as a fact
#: because "which process was asked to stop" is the first question an operator has afterwards.
NOTE_CANCEL_TARGET = "cancel_target"
#: A result that arrived for an attempt the run had already stopped. It cannot change the
#: outcome, so the fact is recorded in the note table where a terminal transition cannot
#: erase it - instead of the controller raising on a state that is already correct.
NOTE_LATE_RESULT = "late_result"
#: One reserved dispatch (batch E1): its role, root, round, repair flag and start state as the
#: transaction left them. Written after the commit, so it describes a reservation that exists
#: rather than one that was attempted.
NOTE_DISPATCH = "dispatch"
#: One reserved dispatch (batch E1): its role, root, round, repair flag and start state as the
#: transaction left them. Written after the commit, so it describes a reservation that exists
#: rather than one that was attempted.
NOTE_DISPATCH = "dispatch"


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
        production: bool | None = None,
        reviewer_driver: HarnessDriver | None = None,
        effective_config: EffectiveConfig | None = None,
        root_binding: RootBudgetBinding | None = None,
        root_limits: RootBudgetLimits | None = None,
    ) -> None:
        self.store = store
        #: The implementer's driver. A machine profile may bind the reviewer elsewhere, so the
        #: two roles are separate objects rather than one driver used twice by assumption.
        self.driver = driver
        self.reviewer_driver = reviewer_driver or driver
        self.controller_build = controller_build
        self.runners = runners or CheckRunners.offline_default()
        self.controller_id = controller_id
        self.reservation_ttl_seconds = reservation_ttl_seconds
        #: Batch E1. The root this run's dispatches are charged against, and its immutable
        #: ceilings. Both come from the loaded authorization artifact, never from the worker:
        #: a caller that could choose its own root could choose a fresh, unspent one.
        #:
        #: They stay ``None`` for an offline run and for every run whose artifact predates roots -
        #: which is exactly the legacy path (no ``invocations`` row, no root counter), so this
        #: default cannot silently enable anything.
        if (root_binding is None) != (root_limits is None):
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                "a root budget needs both its binding and its limits: a binding without a "
                "ceiling would spend against an allowance nobody approved",
            )
        self.root_binding = root_binding
        self.root_limits = root_limits
        #: Is this a real Harness run rather than an offline one? It decides the gates that
        #: make sense only for a delivery: approved checks must be real, the workspace must be
        #: isolated, and the run must be able to write.
        #:
        #: The default is deliberately not "trust the driver object". A test that wires the
        #: production transport to prove a code path would otherwise be handed production
        #: rules it did not ask for, and the honest signal - does this run execute real
        #: approved checks? - is in the runner registry. ``kind=fake`` exists only offline, so
        #: a controller with no real command runner is an offline controller. The CLI states
        #: the answer explicitly instead of relying on this inference.
        self.production = production if production is not None else self._infer_production()
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
        #: The configuration this run was resolved with, recorded against the run so `status`
        #: and `report` can state which profile, role bindings and permissions were in effect -
        #: instead of a reader having to infer them from whatever is configured later.
        self.effective_config = effective_config
        #: Workspace the current run targets; set by ``run_task``. Only used for the
        #: read-only drift check in ``workspace_matches_receipt``.
        self.project_root: Path | None = None
        assert_driver_shape(driver)
        assert_driver_shape(self.reviewer_driver)

    def _infer_production(self) -> bool:
        """Can this controller execute a real approved check at all?

        ``CommandCheckRunner`` is the only runner that runs an approved program. Without one,
        every accepted check is a ``fake`` whose verdict came from the caller, so the run
        cannot produce program evidence a delivery could rest on.
        """
        from .verify import CommandCheckRunner

        return any(
            isinstance(runner, CommandCheckRunner) for runner in self.runners.runners.values()
        )

    @property
    def allow_fake_checks(self) -> bool:
        """May admission accept ``kind=fake`` checks for this run?

        Only for an offline run. A fake check executes nothing and returns a verdict chosen by
        the caller, so accepting one on a real delivery would let a receipt claim a program
        verification that never happened.
        """
        return not self.production

    def render(self, renderer: Renderer) -> Renderer:
        """The renderer to use for this role's packet.

        A seam, not a policy: it exists so a test can drive the controller with a renderer that
        omits a required field and observe the *consequence* - the agent refuses, because the
        prompt it received was incomplete - without weakening the prompt-digest binding that
        catches a driver corrupting a complete packet in transit. Production always renders with
        the real one.
        """
        return renderer

    # -- public entry points -------------------------------------------------

    def run_task(self, request: RunRequest) -> RunOutcome:
        spec = request.task
        project = request.project
        project_root = Path(request.project_root)

        report = validate_task_spec(
            spec, project, project_root, allow_fake_checks=self.allow_fake_checks
        )
        if not report.ok:
            first = report.issues[0]
            raise RefusedError(first.code, f"task {spec.task_id} refused: {first.detail}")

        self.project_root = project_root
        spec_digest = spec.spec_digest()

        # --- an identical TaskSpec is a *history query*, not a new dispatch ---------------
        # This is checked before the authorization is registered, before any allowance is
        # checked and before any preflight: a task that already ended must return its recorded
        # outcome even when its authorization has since been used up. (Its own run keeps the
        # budget it was admitted with; nothing here spends anything.)
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
            # Admitted but never dispatched (for example an interrupted controller): continue
            # the existing run instead of opening a second one. Only the turns this run still
            # needs are asked for, because its first dispatch may already have been paid for.
            self._assert_allowance_for(run_id, spec, project)
            if not self.store.claim_run(run_id, self.controller_id):
                raise RefusedError(
                    RefusalCode.RUN_CLAIMED_BY_OTHER,
                    f"run {run_id} is owned by another controller; one owner per project at a time",
                )
            self._record_effective_config(run_id)
            return self._drive(run_id, request)

        # --- a new dispatch: gate everything before the artifact is even registered -------
        # The run id is chosen here, not inside ``create_run``, because the implementer packet
        # embeds the worktree path and that path contains the run id. Rendering with a
        # placeholder path would let a packet pass this check and then fail after an allowance
        # had been claimed - the exact gap this ordering closes.
        run_id = new_run_id()
        repo, worktree_root = self._worktree_path(run_id, spec)
        implementer_packet = self._render_implementer_packet(
            run_id=run_id,
            spec=spec,
            workspace=str(worktree_root) if worktree_root is not None else str(project_root),
            deadline_seconds=request.deadline_seconds,
            writes_allowed=resolve_permissions(spec)[0],
        )
        # The root is registered before the run row exists: its ceilings are an admission
        # precondition, and refusing them after the run row was written would leave a run
        # pointing at a root nothing agreed to. Registration is idempotent, so this is one call
        # on purpose - two would be a reader's puzzle, not an extra guarantee.
        if self.root_binding is not None:
            self._register_root_budget()
        self._assert_dispatch_preconditions(spec, project, request.deadline_seconds)
        self._assert_allowance_for(run_id, spec, project)

        # --- real-run gate: the artifact is recorded first, then the zero-model preflight.
        # Nothing expensive happens for an unauthorized real run - no credential read, no
        # workspace, no budget reservation, no process. The artifact is registered *before*
        # the preflight so that even a refused attempt leaves an auditable record of what was
        # authorized and that nothing was spent. Its allowance is claimed later, at the moment
        # a dispatch is actually certain, so a duplicate submission (which correctly dispatches
        # nothing) cannot consume it.
        if self.authorization is not None:
            self.store.register_authorization(self.authorization.as_store_record())
            # The preflight proves that a *launch* works before a submission is spent: it starts
            # the resolved client with a metadata argument. A driver that starts no process has
            # no launch binding to prove, so demanding a probe from it would refuse the one path
            # an offline run has. A driver that *does* offer the check still gets it whatever the
            # run's production flag says: the gate is about the binding, not about a label.
            if self._launch_is_probeable():
                report = self._preflight_report()
                if not report[0]:
                    raise RefusedError(
                        RefusalCode.NOT_IMPLEMENTED,
                        f"zero-model preflight failed for this execution binding: {report[1]}. "
                        "No authorization allowance was consumed and nothing was dispatched.",
                    )

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
        if run_id != implementer_packet.run_id:
            # Lost the insert race, so this dispatch belongs to another run's identity. The
            # packet names the previous id and must not be sent under this one.
            return self._outcome_for(
                run_id,
                notes=[
                    "a concurrent identical submission created this run first; no second "
                    "dispatch was made"
                ],
            )

        self._record_effective_config(run_id)

        if not self.store.claim_run(run_id, self.controller_id):
            raise RefusedError(
                RefusalCode.RUN_CLAIMED_BY_OTHER,
                f"run {run_id} is owned by another controller; one owner per project at a time",
            )

        return self._drive(run_id, request, implementer_packet=implementer_packet)

    def _record_effective_config(self, run_id: str) -> None:
        """Record the configuration this run is executing under, once, next to the run.

        ``status`` and ``report`` read it back, so "which profile, which role bindings, which
        permissions" is a recorded fact rather than something the reader infers from whatever
        happens to be configured now. A run's first recorded configuration is never
        overwritten: if a later invocation resolves a different one (another profile, a flipped
        write opt-in), that is recorded as a divergence note and the run keeps its identity.
        """
        if self.effective_config is None:
            return
        existing = self.store.effective_config_for(run_id)
        if existing is None:
            self.store.record_effective_config(run_id, self.effective_config)
            return
        if existing.digest() != self.effective_config.digest():
            self.store.record_note(
                run_id,
                "the configuration resolved for this invocation differs from the one recorded "
                f"for this run: recorded profile={existing.profile_id or '(command line)'} "
                f"digest={existing.digest()}, current "
                f"profile={self.effective_config.profile_id or '(command line)'} "
                f"digest={self.effective_config.digest()}. The run keeps the configuration it "
                "was admitted with; nothing is re-dispatched under the new one.",
            )

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
        """Observe an interrupted attempt. Must not start new model work.

        The observation goes to the invocation the run was actually on - the reviewer's own
        driver during ``phase=review`` - because a process the other driver never started
        cannot be reported on. Nothing is re-dispatched either way.

        Batch E1 adds the ledger half, and it is deliberately the *first* thing that happens: an
        invocation that was reserved or started and never settled becomes ``unknown``, which
        keeps blocking the root. Recording the reconciliation payload is a description of what
        was observed; not closing the ledger entry would leave a run whose controller died
        between the commit and the spawn looking like a run with work still in flight, and the
        next revision of the same task would be refused for the wrong reason.
        """
        closed = self.store.mark_unsettled_invocations_unknown(
            run_id,
            "the controller did not observe a result for this invocation; reconciled by an "
            "operator. The consumption stands and this root does not re-dispatch.",
        )
        attempt = self.store.open_attempt(run_id)
        if attempt is None:
            return ReconcileOutcome.NOT_STARTED
        _role, driver, invocation_id = self._active_invocation(self.store.get_run(run_id), attempt)
        if not invocation_id:
            return ReconcileOutcome.NOT_STARTED
        result = driver.reconcile(invocation_id)
        self.store.record_reconcile(attempt["attempt_id"], result.model_dump(mode="json"))
        if closed:
            self.store.record_note(
                run_id,
                f"{NOTE_DISPATCH}: {closed} unresolved invocation(s) recorded as unknown; no "
                "re-dispatch and no refund - the allowance they consumed stands",
            )
        return result.outcome

    def cancel(self, run_id: str) -> CancellationReceipt:
        """Request a stop: record the intent first, then ask the driver, then believe facts.

        Idempotent: a recorded intent plus a recorded receipt short-circuits. No prompt is
        sent, no budget is charged, and a confirmed stop is reported as a *local process*
        fact - never as a successful protocol cancellation or a known business result.

        The stop is routed to the role that is actually running. Both roles are separate
        invocations, and a machine profile may bind them to separate driver objects, so the
        invocation id and the driver are resolved together from stored facts rather than
        assumed to be the implementer's.
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
        role, driver, active_invocation = self._active_invocation(row, attempt)
        if not active_invocation:
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

        receipt = self._driver_cancel(driver, active_invocation)
        self.store.record_cancel_receipt(run_id, receipt)
        self.store.record_note(
            run_id,
            f"{NOTE_CANCEL_TARGET}: role={role} invocation={active_invocation} "
            f"reported={receipt.status} mechanism={receipt.mechanism}",
        )
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
            # The ledger records the same fact: the allowance this dispatch consumed stays
            # consumed, and the invocation is closed so it does not keep the root open for
            # nothing. A stopped invocation is not an unknown one.
            self._settle_cancelled_invocation(run_id, active_invocation, receipt)
            self.store.set_blocked(
                run_id,
                RefusalCode.CANCELLED_BY_OPERATOR,
                f"stop confirmed ({receipt.mechanism}) for the {role} invocation "
                f"{active_invocation} ({self._driver_label(driver, role)}); local execution "
                "stopped, business result unknown",
            )
        else:
            self.store.set_blocked(
                run_id,
                RefusalCode.OUTCOME_UNKNOWN,
                f"stop could not be confirmed for the {role} invocation {active_invocation} "
                f"({self._driver_label(driver, role)}): {receipt.status}; work may still be running",
            )
        return receipt

    def _settle_cancelled_invocation(
        self, run_id: str, invocation_id: str, receipt: CancellationReceipt
    ) -> None:
        """Close the ledger entry a confirmed stop ended.

        Only for a *confirmed* stop: an unconfirmed one stays open on purpose, because "work may
        still be running" is exactly the state that must keep blocking. The consumption is never
        returned - the allowance was committed before the process existed - so this records the
        outcome, not a refund.

        Three shapes, and the difference between the last two is the whole point:

        * a launch is recorded (``started_at`` set) -> the launch happened, so the entry is
          settled as cancelled;
        * **no launch was ever requested** (``launch_requested_at`` is NULL) -> nothing was asked
          of any driver, so ``not_started`` is a fact: the allowance bought nothing at all;
        * a launch *was* requested and no report came back -> nobody may say whether a process
          exists, and a confirmed stop of a real child is direct evidence that one did. This is
          ``launch_unknown``: the root stays blocked and an operator reconciles it. Recording
          ``not_started`` here would be claiming, from an empty timestamp, that no driver was ever
          asked - which is false, and was reproduced against a real forced stop of a real pid.
        """
        recorded = self.store.invocation(invocation_id) if invocation_id else None
        if recorded is None:
            # A legacy run: its dispatch facts live on the attempt row, and there is no ledger
            # entry to close.
            return
        if recorded.started_at is not None:
            self.store.settle_invocation(
                invocation_id,
                outcome=InvocationOutcome.CANCELLED,
                detail=f"stop confirmed ({receipt.mechanism}) for run {run_id}",
            )
            return
        if not recorded.launch_requested:
            self.store.mark_invocation_not_started(
                invocation_id,
                f"a confirmed stop ({receipt.mechanism}) ended run {run_id} before any driver was "
                "asked to launch this invocation",
            )
            return
        self.store.mark_launch_unresolved(
            invocation_id,
            f"a stop was confirmed ({receipt.mechanism}) for run {run_id} after the launch was "
            "requested and before any spawn report arrived: no launch is recorded and no process "
            "is known, so the ledger does not claim either",
        )

    def _active_invocation(
        self, row: object, attempt: object
    ) -> tuple[str, HarnessDriver, str]:
        """``(role, driver, invocation_id)`` for the invocation a stop should reach.

        The run's recorded ``phase`` decides, not the order of the ids and not an assumption
        that the driver is the implementer's:

        * ``phase=review`` and a recorded review invocation -> that invocation, through
          ``reviewer_driver``. The reviewer is its own process, session and allowance, so a
          stop that named the implementer's (already finished) invocation would report a fact
          about the wrong process;
        * otherwise -> the implementer's invocation through ``driver``.

        Only durable facts are read (the run row and the attempt row), so this is the same
        decision a second controller process would make. ``""`` means no invocation was
        dispatched for this attempt and there is nothing to ask to stop.
        """
        data = dict(row)  # type: ignore[arg-type]
        attempt_data = dict(attempt) if attempt is not None else {}
        review_invocation = str(attempt_data.get("review_invocation_id") or "")
        if data.get("phase") == CheckPhase.REVIEW.value and review_invocation:
            return "reviewer", self.reviewer_driver, review_invocation
        return "implementer", self.driver, str(attempt_data.get("invocation_id") or "")

    def _driver_label(self, driver: HarnessDriver, role: str) -> str:
        """How the stop's record names the driver that was asked, without guessing."""
        driver_id = getattr(driver, "driver_id", "") or type(driver).__name__
        bound = self.effective_config.role(role) if self.effective_config is not None else None
        agent = f", agent={bound.agent}" if bound is not None else ""
        return f"driver={driver_id}{agent}"

    def _driver_cancel(self, driver: HarnessDriver, invocation_id: str) -> CancellationReceipt:
        """Prefer the handle API when the driver offers it; degrade to the plain contract.

        Takes the driver explicitly: with per-role bindings the invocation is owned by one
        role's driver, and asking the other one would report "nothing was stopped" for a
        process that is still running.
        """
        handles = getattr(driver, "_handles", None)
        cancel_handle = getattr(driver, "cancel_handle", None)
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
            return driver.cancel(invocation_id)
        except Exception as exc:  # noqa: BLE001
            return CancellationReceipt(
                invocation_id=invocation_id,
                status="unknown",
                mechanism="none",
                detail=f"driver raised while stopping: {exc!r}",
            )

    # -- the state machine ---------------------------------------------------

    def _assert_dispatch_preconditions(
        self, spec: TaskSpec, project: ProjectConfig, deadline_seconds: int
    ) -> None:
        """Refuse a run that is already known to be unable to finish, before anything is spent.

        The rules themselves live in ``admission.predictable_dispatch_problems``, which
        ``hflow prepare`` calls too: a preview that reported "admitted" for a task this gate
        would refuse would be answering a different question than the user asked. This method
        only supplies the resolved facts - the write permission and the launches - and turns
        the first problem into a refusal.
        """
        problems = predictable_dispatch_problems(
            spec,
            project,
            production=self.production,
            implementer_writes=resolve_permissions(spec)[0],
            launches=self._resolved_launches(),
        )
        if problems:
            first = problems[0]
            more = f" (+{len(problems) - 1} more admission problem(s))" if len(problems) > 1 else ""
            raise RefusedError(first.code, first.detail + more)

    def _resolved_launches(self) -> list[LaunchConfig]:
        """The launches this run would perform, as resolved before any approval.

        Empty when no configuration was resolved (a direct library caller, or an offline run):
        the gate then cannot know the answer, which is different from knowing it is fine.
        """
        if self.effective_config is None:
            return []
        return [entry.launch for entry in self.effective_config.roles if entry.launch is not None]

    # -- allowance and packet preparation ------------------------------------

    def _check_artifact_dir(self, check_id: str, evidence_id: str) -> Path:
        """Where one check's captured output is kept: under the run's data dir, never a workspace.

        Every reference an evidence row publishes is a file in here, so "read the full log" leads
        to something that exists after the check has finished.
        """
        safe_id = "".join(
            character if character.isalnum() or character in "-_." else "_" for character in check_id
        )
        directory = self.data_dir / "artifacts" / (evidence_id or "unknown") / (safe_id or "check")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _loop_turns_required(self, spec: TaskSpec, project: ProjectConfig) -> int:
        """How many top-level invocations the fixed loop needs: implementer (+ reviewer).

        This is the *whole* remaining loop for a fresh dispatch, not what happens to be left in
        the run's budget. It is deliberately not a promise about the model's behaviour, only
        about how many invocations this build will start for one accepted delivery.
        """
        return 2 if spec.needs_review(project) else 1

    def _assert_allowance_for(
        self, run_id: str, spec: TaskSpec, project: ProjectConfig
    ) -> None:
        """Refuse a dispatch the authorization cannot cover, before anything is spent.

        The per-dispatch ledger stays the real gate: this is an admission check, not a
        reservation, and it cannot make the two claims atomic. What it removes is the
        predictable case - a task whose review is required while only one submission is left -
        which previously ran the implementer and then blocked with the implementation paid for.

        History is not re-charged: for a run that already dispatched, only the turns it still
        needs are required, and a run whose authorization is spent still returns its recorded
        outcome (that path is handled before this method is reached).
        """
        if self.authorization is None:
            return
        state = self.store.authorization_state(self.authorization.authorization_id)
        if state is None:
            # Not registered yet: the recorded artifact is the only source, and it is the same
            # object the CLI verified against this run's binding.
            remaining = self.authorization.max_top_level_submissions
            limit = self.authorization.max_top_level_submissions
        else:
            limit = int(state["max_top_level_submissions"])
            remaining = limit - int(state["used_top_level_submissions"])

        already_paid = self.store.invocation_counts(run_id)[0] + self.store.invocation_counts(run_id)[1]
        needed = max(0, self._loop_turns_required(spec, project) - already_paid)
        if needed > remaining:
            raise RefusedError(
                RefusalCode.BUDGET_EXHAUSTED,
                f"authorization {self.authorization.authorization_id} has {remaining}/{limit} "
                f"top-level submission(s) left, but this task's fixed loop needs {needed} more "
                f"({self._loop_turns_required(spec, project)} for implementer + reviewer). "
                "Nothing was dispatched and no allowance was consumed; obtain a new "
                "authorization for this task or reduce it to a single invocation.",
            )

    def _worktree_path(self, run_id: str, spec: TaskSpec) -> tuple[GitRepo | None, Path | None]:
        """Where this run's isolated worktree *would* go, without creating anything.

        The implementer packet contains the workspace path, and on Windows a worktree path is
        long enough to change whether the packet fits its bound. The path is therefore derived
        here - from the run id and the repository root, the same inputs ``create_worktree`` uses
        - so the packet that is checked is the packet that will be sent.

        Refusals that a worktree run cannot survive are raised here, before the run row exists:
        a repository that cannot be discovered, and a base commit that does not exist. Both are
        knowable without side effects, and both used to be discovered after the run (and, with
        an authorization, after a claim).
        """
        if spec.workspace.mode != "worktree":
            return None, None
        assert self.project_root is not None
        try:
            repo = GitRepo.discover(self.project_root)
        except GitError as exc:
            raise RefusedError(
                RefusalCode.SCOPE_VIOLATION,
                f"this task requires an isolated worktree but the repository could not be "
                f"discovered: {exc}. Nothing was dispatched and no allowance was consumed.",
            ) from exc
        try:
            base_commit = spec.workspace.base_commit or repo.resolve_commit("HEAD")
        except GitError as exc:
            raise RefusedError(
                RefusalCode.SCOPE_VIOLATION,
                f"the base commit for this worktree run could not be resolved: {exc}",
            ) from exc
        if not repo.commit_exists(base_commit):
            raise RefusedError(
                RefusalCode.SCOPE_VIOLATION,
                f"base commit {base_commit!r} does not exist in {repo.root}. Nothing was "
                "dispatched and no allowance was consumed.",
            )
        return repo, repo.worktree_parent() / run_id

    def _render_implementer_packet(
        self,
        *,
        run_id: str,
        spec: TaskSpec,
        workspace: str,
        deadline_seconds: int,
        writes_allowed: bool,
    ) -> PreparedPacket:
        """Render the implementer packet for this run, or refuse before anything is claimed.

        ``writes_allowed`` is the run's *effective* permission, not "the scope lists a path".
        The packet tells the worker whether it may change files at all, so rendering it from
        anything other than the permission the dispatch will carry would tell the worker
        something the transport does not honour - and ``prepare`` reports the same packet, so
        the preview and the dispatch have to agree byte for byte.
        """
        try:
            packet = self.render(render_implementer_packet)(
                task_id=spec.task_id,
                task_revision=spec.revision,
                goal=spec.goal,
                acceptance=spec.acceptance,
                scope=spec.scope,
                workspace=workspace,
                spec_digest=spec.spec_digest(),
                deadline_seconds=deadline_seconds,
                writes_allowed=writes_allowed,
            )
        except PacketTooLargeError as exc:
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                f"the implementer input packet for this run does not fit, so nothing was "
                f"dispatched and no allowance was consumed: {exc}",
            ) from exc
        return PreparedPacket(run_id=run_id, packet=packet)

    def _preflight_report(self) -> tuple[bool, str]:
        if self.preflight is None:
            return False, "no zero-model preflight is wired for this execution binding"
        try:
            return self.preflight()
        except Exception as exc:  # noqa: BLE001 - a broken preflight is a refusal, not a crash
            return False, f"preflight raised {exc!r}"

    def _launch_is_probeable(self) -> bool:
        """Is there a launch binding for the zero-model preflight to prove?

        A driver that starts no process cannot fail a launch test, so requiring one from it would
        refuse an offline run for a property it cannot have - and the offline fake driver is the
        only way an E1 root run is exercised end to end without buying a model call.

        The question is asked of the *resolved configuration* first, because that is what the
        preflight actually inspects: a run with no launch config has nothing to prove. When no
        configuration was resolved (a direct library caller), the drivers themselves are asked.
        """
        if self.effective_config is not None:
            return bool(self._resolved_launches())
        return any(
            callable(getattr(driver, "readonly_client_check", None))
            for driver in (self.driver, self.reviewer_driver)
        )

    def _register_root_budget(self) -> None:
        """Record this root's binding and ceilings before any run row or dispatch exists.

        ``StoreError`` here is an admission refusal, not a crash: it means this run presents a
        different ceiling, or a second root id, for a task that already has one. Either way the
        allowance belongs to the recorded root and nothing is dispatched.
        """
        assert self.root_binding is not None and self.root_limits is not None
        try:
            self.store.register_root_budget(self.root_binding, self.root_limits)
        except StoreError as exc:
            raise RefusedError(RefusalCode.BUDGET_EXHAUSTED, str(exc)) from exc

    def _reserve_dispatch(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        invocation_id: str,
        reservation_id: str,
        role: str,
        purpose: str,
        spec: TaskSpec,
        project: ProjectConfig,
    ) -> DispatchReservation:
        """The one dispatch transaction, for both roles.

        Everything a dispatch costs and everything that proves it happened commit together: the
        authorization's submission, the run's reserved turn, the root's consumption (when this
        run has a root) and the invocation record. A refusal raises ``RefusedError`` carrying the
        store's own message, and the caller blocks the run - nothing is dispatched, no driver is
        built and no process is created.

        ``required_loop_remaining`` is what the root must still be able to afford: this dispatch
        plus every further top-level dispatch this run needs for one accepted delivery. Passing
        only "one more" is how a revision with an implementer's worth of allowance but no
        reviewer's worth got admitted and then blocked with the implementation paid for.

        A replay of the same ``invocation_id`` returns ``is_new=False``. The caller must then only
        coordinate: starting the driver again would be the second dispatch this ordering exists
        to prevent.
        """
        expires_at = (
            parse_ts(utc_now()) + timedelta(seconds=self.reservation_ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        already_paid = sum(self.store.invocation_counts(run_id))
        needed = max(1, self._loop_turns_required(spec, project) - already_paid)
        try:
            reservation = self.store.reserve_dispatch(
                run_id=run_id,
                controller_id=self.controller_id,
                invocation_id=invocation_id,
                role=role,
                reservation_id=reservation_id,
                reserved_turns=1,
                reservation_expires_at=expires_at,
                attempt_id=attempt_id,
                root_binding=self.root_binding,
                root_limits=self.root_limits,
                authorization_id=(
                    self.authorization.authorization_id if self.authorization is not None else ""
                ),
                authorization_max=(
                    self.authorization.max_top_level_submissions
                    if self.authorization is not None
                    else None
                ),
                required_loop_remaining=needed,
            )
        except StoreError as exc:
            raise RefusedError(self._dispatch_refusal_code(str(exc)), str(exc)) from exc
        if self.authorization is not None:
            self.store.record_note(
                run_id,
                f"authorization {self.authorization.authorization_id}: consumed top-level "
                f"submission {reservation.authorization_used}/"
                f"{self.authorization.max_top_level_submissions} for {purpose} "
                f"(reserved in the same transaction as the attempt and the invocation record; "
                f"this run's remaining loop needs {needed} dispatch(es))",
            )
        if reservation.invocation is not None:
            self.store.record_note(
                run_id,
                f"{NOTE_DISPATCH}: role={role} invocation={reservation.invocation.invocation_id} "
                f"root={reservation.invocation.root_id} round={reservation.invocation.round} "
                f"repair={reservation.invocation.is_repair} state="
                f"{reservation.invocation.state.value}",
            )
        return reservation

    def _dispatch_refusal_code(self, message: str) -> RefusalCode:
        """Which refusal a refused reservation is, read from the store's own wording.

        Deliberately narrow: only the cases the store states explicitly are re-labelled, so an
        unexpected failure keeps its own message and is not dressed up as a budget decision.
        """
        if "cancellation intent" in message:
            return RefusalCode.CANCELLED_BY_OPERATOR
        if (
            "unresolved invocation" in message
            or "deadline" in message
            or "exhausted" in message
            or "cannot complete this run's loop" in message
            or "is owned by run" in message
        ):
            return RefusalCode.BUDGET_EXHAUSTED
        return RefusalCode.INTERNAL_ERROR

    def _spawn_reporter(self, reservation: DispatchReservation) -> SpawnReporter:
        """The callback a driver uses to report what it observed at its spawn decision.

        Wired into every ``InvocationRequest`` this controller builds. A driver that calls it
        turns "we asked for a launch" into either "a process exists" or "no process was created";
        a driver that does not leaves the invocation ``REQUESTED``, which is reported as an
        unconfirmed launch rather than being counted as a start.

        A bookkeeping failure inside the callback is swallowed on purpose: the driver is inside
        its own spawn gate, and raising there would turn a ledger problem into "the launch
        failed". The consequence of a lost report is the conservative one - the invocation stays
        ``REQUESTED`` and keeps blocking - and the run's note table records it.
        """
        invocation = reservation.invocation
        if invocation is None:
            return lambda _fact: None
        run_id = invocation.run_id

        def report(fact: SpawnFact) -> None:
            try:
                self.store.record_invocation_spawn(fact)
            except StoreError as exc:
                self.store.record_note(
                    run_id,
                    f"{NOTE_DISPATCH}: the driver's spawn fact for {invocation.invocation_id} "
                    f"could not be recorded ({exc}); the invocation keeps the state it had",
                )

        return report

    def _mark_launch_requested(self, reservation: DispatchReservation) -> None:
        """Record that a driver is about to be asked to launch this invocation.

        Deliberately not a start. The last moment at which "no process exists" is certainly true
        is this one, so recording a request here is honest; recording a start here is what made a
        driver-suppressed launch look like a running model call. The process fact comes from the
        driver through the spawn report wired into the request.
        """
        if reservation.invocation is not None:
            self.store.mark_invocation_launch_requested(reservation.invocation.invocation_id)

    def _settle_invocation(
        self, reservation: DispatchReservation, outcome: InvocationOutcome | None, detail: str = ""
    ) -> None:
        """Close the dispatch record with the outcome the driver reported.

        Never refunds: an ``OUTCOME_UNKNOWN`` becomes ``unknown``, which keeps blocking the root
        until an operator reconciles it. A failure to record the settlement is not allowed to
        replace the result the caller already has - it is reported as a note instead.
        """
        if reservation.invocation is None:
            return
        try:
            self.store.settle_invocation(
                reservation.invocation.invocation_id, outcome=outcome, detail=detail
            )
        except StoreError as exc:
            self.store.record_note(
                reservation.invocation.run_id,
                f"{NOTE_DISPATCH}: invocation {reservation.invocation.invocation_id} could not be "
                f"settled ({exc}); its consumption stands and the root keeps it open",
            )

    def _mark_invocation_not_started(self, reservation: DispatchReservation, detail: str) -> None:
        """Record a reservation that provably never reached a launch. The spend is kept."""
        if reservation.invocation is None:
            return
        self.store.mark_invocation_not_started(reservation.invocation.invocation_id, detail)

    def _mark_driver_failure(self, reservation: DispatchReservation, exc: BaseException) -> None:
        """Record a driver that raised before it reported anything.

        Deliberately **not** "not started": nobody said whether a process exists, and a driver can
        raise *after* creating one (a failed pipe write, a broken handle). The safe reading of "no
        report and an exception" is an unconfirmed launch - the same state a crash in this window
        leaves - so the root stays blocked and an operator looks. Recording it as "never started"
        would be the mirror image of the earlier bug: an assumption in the direction that makes a
        possibly-billed invocation look free.

        Only the ledger state is left alone. The attempt is still failed and the run still blocks
        with the driver's error, because that part *is* known.
        """
        if reservation.invocation is None:
            return
        try:
            self.store.record_note(
                reservation.invocation.run_id,
                f"{NOTE_DISPATCH}: the driver raised for invocation "
                f"{reservation.invocation.invocation_id} without reporting a spawn fact "
                f"({exc!r}); the ledger keeps it as a launch that was requested and unconfirmed, "
                "which blocks the root until an operator reconciles it",
            )
        except StoreError:
            pass  # a broken note write must not change what the caller reports

    def _confirm_driver_ran(
        self, reservation: DispatchReservation, result: InvocationResult
    ) -> None:
        """Decide what a driver's return means when it did not report a spawn fact.

        Only for a driver that does not implement ``on_spawn`` (an older or third-party one), and
        only when no report arrived: a driver that reports *has* answered, whatever its report
        says, and inferring on top of that answer would let a silent-looking return overwrite a
        recorded "no process was created". The two readable shapes for a silent driver are:

        * a completed invocation that produced work - a candidate, a review verdict or observed
          agent turns - so a process existed and it is recorded as started;
        * a cancelled invocation that produced no work at all, so nothing ran and it is recorded
          as never started.

        Anything else stays ``requested``: guessing in either direction is what produced both of
        the bugs this replaces.
        """
        if reservation.invocation is None:
            return
        invocation_id = reservation.invocation.invocation_id
        current = self.store.invocation(invocation_id)
        if current is None:
            return
        if current.started_at is not None or current.launch_requested_at is not None:
            # A driver was asked and answered - or a process is already recorded. An inference is
            # not evidence, and it never overrides one.
            return
        if current.state is InvocationStartState.NOT_STARTED:
            return  # already reported as producing nothing
        produced_work = bool(
            result.candidate is not None or result.review is not None or result.agent_turns
        )
        if result.outcome is InvocationOutcome.COMPLETED and produced_work:
            self.store.mark_invocation_started(invocation_id)
        elif result.outcome is InvocationOutcome.CANCELLED and not produced_work:
            self.store.mark_invocation_not_started(
                invocation_id,
                "the driver reported a cancelled invocation that produced no work and no process",
            )

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

    def _drive(
        self,
        run_id: str,
        request: RunRequest,
        *,
        implementer_packet: PreparedPacket | None = None,
    ) -> RunOutcome:
        spec = request.task
        project = request.project
        project_root = Path(request.project_root)
        attempt_id = new_attempt_id()
        invocation_id = new_invocation_id()
        reservation_id = new_reservation_id()

        # --- workspace preparation (M2): an isolated worktree, or the project in place
        # The repository and the base commit were validated before the run row existed (see
        # ``_worktree_path``), so what remains here is the one step that cannot be predicted:
        # actually creating the worktree. If it fails, the run blocks with no dispatch - the
        # allowance was not claimed and no budget was reserved yet.
        repo: GitRepo | None = None
        worktree: Path | None = None
        user_tree_before = ""
        dirty_target = False
        if spec.workspace.mode == "worktree":
            try:
                repo = GitRepo.discover(project_root)
                user_tree_before = repo.user_change_fingerprint()
                base_commit = spec.workspace.base_commit or repo.resolve_commit("HEAD")
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
        #
        # The rule itself lives in `prepare.resolve_permissions`, which is also what the
        # effective configuration reports - so the permission an approval covers and the
        # permission the dispatch uses are one value, not two copies of one idea.
        implementer_writes, _reviewer_writes = resolve_permissions(spec)
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

        # --- the one dispatch transaction: the authorization's submission, the run's turn,
        # the root's consumption and the invocation record all commit together. Every
        # predictable refusal happens before this (admission checks, the packet, the allowance
        # gate); every *failure* after it is recorded against the invocation it belongs to,
        # instead of leaving a counter that moved with nothing to show for it.
        try:
            dispatch = self._reserve_dispatch(
                run_id=run_id,
                attempt_id=attempt_id,
                invocation_id=invocation_id,
                reservation_id=reservation_id,
                role="implementer",
                purpose="implementer invocation",
                spec=spec,
                project=project,
            )
        except RefusedError as exc:
            return self._refuse(run_id, exc.code, str(exc))
        attempt_id = dispatch.attempt_id
        invocation_id = (
            dispatch.invocation.invocation_id if dispatch.invocation else invocation_id
        )
        if dispatch.invocation is None:
            # The legacy path: no ledger row, so the dispatch facts stay on the attempt row, and
            # that write is the stop-aware one - a stop committing first must win the handoff.
            self.store.record_invocation(attempt_id, invocation_id)

        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )

        # Recorded before the worker runs: what the workspace looked like, so a change outside
        # the declared scope is detected afterwards from two manifests rather than assumed.
        pre_manifest = manifest(execution_root)
        # The role input packet is rendered from recorded facts and travels as one string. The
        # driver transports it verbatim; it never rebuilds the task text.
        #
        # A packet prepared before admission (``run_task``) is reused only when the workspace it
        # names is the workspace this run actually got. Any other path means the checked packet
        # is not the packet that would be sent, so it is rendered again here and must fit.
        if implementer_packet is not None and implementer_packet.workspace == str(execution_root):
            prepared = implementer_packet
        else:
            prepared = self._render_implementer_packet(
                run_id=run_id,
                spec=spec,
                workspace=str(execution_root),
                deadline_seconds=request.deadline_seconds,
                writes_allowed=implementer_writes,
            )
        self.store.record_note(
            run_id,
            f"{NOTE_PACKET}: role=implementer bytes={prepared.packet.byte_length} "
            f"digest={prepared.packet.digest}",
        )
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
            packet=prepared.packet.text,
            writes_allowed=implementer_writes,
            data_dir=str(self.data_dir),
            # Asked by the driver at the instant it creates the process, not answered here: a
            # snapshot taken now would be stale by the time the child is spawned.
            stop_requested=lambda: self._stop_recorded(run_id),
            # The other half of that handoff: the driver reports what it observed at its spawn
            # decision, so "a process exists" is a recorded observation. A driver that never calls
            # it leaves this invocation `requested`, which is reported as an unconfirmed launch
            # rather than counted as a start.
            on_spawn=self._spawn_reporter(dispatch),
        )

        # From here on, failures must not re-dispatch: the model may already have run. The
        # ledger records how far this dispatch got before it is handed over - a launch *requested*
        # and nothing more - so a crash here is visible as an unconfirmed launch rather than
        # rounded up to a model call.
        self._mark_launch_requested(dispatch)
        try:
            result = self.driver.start(invocation)
        except RefusedError as exc:
            self._mark_invocation_not_started(dispatch, f"refused before launch: {exc}")
            return self._block_attempt(run_id, attempt_id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - controller must not hot-fix a driver
            self._mark_driver_failure(dispatch, exc)
            return self._block_attempt(run_id, attempt_id, RefusalCode.INTERNAL_ERROR, repr(exc))
        else:
            self._confirm_driver_ran(dispatch, result)

        if result.outcome is InvocationOutcome.OUTCOME_UNKNOWN:
            self._settle_invocation(dispatch, result.outcome, "driver reported an unknown outcome")
            self._apply_result_or_stay_stopped(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.OUTCOME_UNKNOWN,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                result=result,
                block_code=RefusalCode.OUTCOME_UNKNOWN,
                reason="the worker's result is unknown; no re-dispatch until an operator reconciles "
                "(plan 9.3)",
            )
            return self._outcome_for(run_id)

        if result.agent_turns is not None:
            self.store.set_turns_observed(run_id, result.agent_turns)

        if result.prompt_digest and result.prompt_digest != prepared.packet.digest:
            # The transport reports the digest of the text it sent (see ``packet_digest``: this
            # is a local record of what was handed over, not a receipt from the remote side). A
            # mismatch means the invocation did not receive this packet, so whatever it did is
            # not an answer to this task; accepting it would attach a receipt to wrong input.
            self.store.record_note(
                run_id,
                f"{NOTE_PROMPT_DIGEST}: MISMATCH sent={result.prompt_digest} "
                f"expected={prepared.packet.digest}",
            )
            self._settle_invocation(dispatch, result.outcome, "prompt digest mismatch")
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=result.outcome,
                result=result.model_dump(mode="json"),
                block_code=RefusalCode.INTERNAL_ERROR,
            )
            return self._blocked(
                run_id,
                RefusalCode.INTERNAL_ERROR,
                "the invocation's prompt digest does not match the rendered input packet, so the "
                "result cannot be attributed to this task",
            )

        if result.outcome is not InvocationOutcome.COMPLETED:
            self._settle_invocation(
                dispatch, result.outcome, "driver reported a non-completed outcome"
            )
            self._apply_result_or_stay_stopped(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=result.outcome,
                result=result,
                block_code=RefusalCode.DRIVER_FAILED,
                reason=f"driver reported {result.outcome.value}: "
                f"{result.error_message or 'no detail'}",
            )
            # Applied or already finalized by a stop: the run's own recorded state is the answer.
            return self._outcome_for(run_id)

        self._settle_invocation(dispatch, result.outcome, "implementer invocation completed")
        if not self._apply_result_or_stay_stopped(
            run_id=run_id,
            attempt_id=attempt_id,
            state=AttemptState.SUCCEEDED,
            outcome=result.outcome,
            result=result,
        ):
            # The run was stopped while this invocation was running. Its result is recorded on
            # the attempt row and the run keeps the decision the operator made; nothing here
            # resumes the loop.
            return self._outcome_for(run_id)

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
            # A real check's captured output is kept under the run's own data directory, so the
            # log an evidence row points at is a file that still exists afterwards. It never
            # lands in the workspace under test.
            artifact_factory=self._check_artifact_dir,
        )

        review = ReviewResult(status="not_run")
        if verification.status != "passed":
            review = ReviewResult(status="not_run", isolation=self.review_isolation)
        elif spec.needs_review(project):
            # A stop recorded while the implementer or its checks were running is read here,
            # before any state transition or handoff to the reviewer. The transition itself is
            # stop-aware, because the cancel can land between reading this and asking for it.
            stopped = self._advance_to_review(run_id)
            if stopped is not None:
                return stopped
            try:
                review = self._review(
                    run_id,
                    spec,
                    execution_root,
                    post_fingerprint,
                    project.checks_digest(),
                    verification=verification,
                    freeze=freeze,
                    project=project,
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
        *,
        verification: VerificationResult | None = None,
        freeze: CandidateFreeze | None = None,
        project: ProjectConfig | None = None,
    ) -> ReviewResult:
        """Buy the review turn, run the reviewer, then interpret its structured verdict.

        The reviewer is told the *task*, the *candidate identity the controller froze* and the
        *program evidence the controller recorded* - never the implementer's own summary of its
        work. Those facts come from this run's rows, so a reviewer cannot be handed a plausible
        but invented candidate.

        Two things this deliberately does *not* do:

        * it does not let the reviewer's prose set the isolation level (A08) - the level
          is whatever the driver could actually enforce, recorded by the controller;
        * it does not spend a review turn when budget cannot cover it (A03): the
          reservation refuses and the run blocks with no reviewer process started.

        It also does not *start* a reviewer for a run whose stop was already requested: the
        intent is recorded before anything is asked to stop, and buying a turn after that
        would spend allowance and dispatch a process for a decision a human already ended.
        """
        attempt = self.store.open_attempt(run_id)
        attempt_id = attempt["attempt_id"] if attempt else ""
        row = self.store.get_run(run_id)
        invocation_id = new_invocation_id()

        # Read as late as possible - after the checks, after the worktree - so a stop recorded
        # while the implementer or its checks were running is seen here.
        if self.store.get_run(run_id)["cancel_intent_at"]:
            raise RefusedError(
                RefusalCode.CANCELLED_BY_OPERATOR,
                "a cancellation intent is recorded, so no review turn was bought and no reviewer "
                "was started; the run stays stopped instead of re-dispatching",
            )

        # The packet is rendered *before* the submission is claimed: an input that cannot be
        # built must not consume an allowance or a budget turn.
        check_summaries = [
            {
                "check_id": r["check_id"],
                "status": r["status"],
                "exit_code": r["exit_code"],
                "command": " ".join(json.loads(r["command_json"])) if r["command_json"] else "",
                "detail": r["detail"],
                # References, extracted from the row rather than left inside the detail text: the
                # packet renders them unshortened, so a long artifact path still reaches the
                # reviewer in full.
                "reason": _reference_field(r["detail"], "reason"),
                "artifact": _reference_field(r["detail"], "artifact"),
                "stdout": _stream_reference(r["detail"], "stdout"),
                "stderr": _stream_reference(r["detail"], "stderr"),
            }
            for r in self.store.evidence_for(run_id, kind="verification")
            if r["candidate_fingerprint"] == candidate_fp and r["checks_digest"] == checks_digest
        ]
        evidence_rows = [
            {
                "evidence_id": r["evidence_id"],
                "check_id": r["check_id"],
                "status": r["status"],
                "exit_code": r["exit_code"],
                "artifact": _reference_field(r["detail"], "artifact"),
                "stdout_digest": r["stdout_digest"],
                "candidate_fingerprint": r["candidate_fingerprint"],
            }
            for r in self.store.evidence_for(run_id, kind="verification")
            if r["candidate_fingerprint"] == candidate_fp
        ]
        candidate_identity: dict[str, object] = {
            "fingerprint": candidate_fp,
            "worktree": str(project_root),
            "paths": list(freeze.paths) if freeze else [],
        }
        if freeze is not None:
            candidate_identity |= {
                "base_commit": freeze.base_commit,
                "git_commit": freeze.candidate_commit,
                "git_tree": freeze.tree,
            }
        try:
            reviewer_packet = self.render(render_reviewer_packet)(
                task_id=spec.task_id,
                task_revision=int(row["task_revision"]),
                goal=spec.goal,
                acceptance=spec.acceptance,
                scope=spec.scope,
                workspace=str(project_root),
                spec_digest=row["spec_digest"],
                candidate_fingerprint=candidate_fp,
                deadline_seconds=900,
                candidate=candidate_identity,
                verification_status=verification.status if verification else "not_run",
                verification_detail=verification.detail if verification else "",
                check_summaries=check_summaries,
                evidence_rows=evidence_rows,
            )
        except PacketTooLargeError as exc:
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                f"the reviewer input packet could not be built, so no review was bought: {exc}",
            ) from exc

        self.store.record_note(
            run_id,
            f"{NOTE_PACKET}: role=reviewer bytes={reviewer_packet.byte_length} "
            f"digest={reviewer_packet.digest}",
        )

        # A stop can be recorded while the packet is being built or the checks are running. It is
        # re-read here, after the rendering and before anything is bought: buying an allowance or
        # a review turn for a run a human already stopped is exactly the spend a stop prevents.
        self._refuse_if_stopped(run_id, where="the review turn was bought")

        # The reviewer is its own invocation and its own top-level submission, bought through
        # the same single transaction as the implementer's: the submission, the review turn, the
        # root's consumption and the invocation record commit together or not at all. A stop that
        # commits first is refused there rather than after one of them moved.
        dispatch = self._reserve_dispatch(
            run_id=run_id,
            attempt_id=attempt_id,
            invocation_id=invocation_id,
            reservation_id=new_reservation_id(),
            role="reviewer",
            purpose="reviewer invocation",
            spec=spec,
            # ``_drive`` always passes the project through; the default only keeps a direct
            # caller from crashing on this path, and it fails closed rather than open.
            project=project if project is not None else ProjectConfig(project_id="", checks=[]),
        )
        attempt_id = dispatch.attempt_id
        invocation_id = (
            dispatch.invocation.invocation_id if dispatch.invocation else invocation_id
        )
        if dispatch.invocation is None:
            # The legacy path: registering the reviewer's invocation and the stop's decision are
            # a single conditional write, so exactly one of them wins. If the stop won, nothing
            # is registered and the reviewer is never started.
            #
            # Registration is *not* enough on its own: a stop can still commit between this write
            # and the driver creating a child. The request therefore carries the stop question,
            # and the driver decides it inside the gate it also uses to publish that invocation's
            # handle - so either the stop is seen and no process is created, or the process
            # exists and the stop finds a published handle to act on.
            self._register_reviewer(run_id, attempt_id, invocation_id)
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
            packet=reviewer_packet.text,
            # A reviewer is read-only, always: it checks what the implementer produced, and an
            # approval for the implementer to write never extends to the review invocation.
            writes_allowed=False,
            data_dir=str(self.data_dir),
            stop_requested=lambda: self._stop_recorded(run_id),
            # The reviewer's launch is reported the same way the implementer's is: the ledger
            # learns whether a process was created from the driver, not from the fact that a
            # review turn was bought.
            on_spawn=self._spawn_reporter(dispatch),
        )
        self._mark_launch_requested(dispatch)
        try:
            review_invocation = self.reviewer_driver.start(review_request)
        except Exception as exc:  # noqa: BLE001 - a broken reviewer must not become an accept
            self._mark_driver_failure(dispatch, exc)
            self.store.attach_review_result(
                attempt_id, {"error": repr(exc), "invocation_id": invocation_id}
            )
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the review invocation could not be started, so no verdict exists: " f"{exc!r}",
            ) from exc
        self._confirm_driver_ran(dispatch, review_invocation)

        if review_invocation.outcome is InvocationOutcome.CANCELLED and self._stop_recorded(run_id):
            # The stop won the handoff inside the driver, so no reviewer process was created.
            # Nothing is attached to the attempt - there was no invocation - and the run keeps
            # the stop decision it already made. The reservation is recorded as never started:
            # its allowance stays consumed (it was committed before the handoff) and the ledger
            # says so instead of counting a process that never existed.
            self._mark_invocation_not_started(dispatch, "the stop won the reviewer handoff")
            raise RefusedError(
                RefusalCode.CANCELLED_BY_OPERATOR,
                "the stop was seen before the reviewer process was created, so none was started "
                "and no reviewer invocation is recorded",
            )

        self.store.attach_review_result(attempt_id, review_invocation.model_dump(mode="json"))
        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )
        if review_invocation.outcome is not InvocationOutcome.COMPLETED:
            # An unfinished turn is a transport failure, not the reviewer's judgment. It is
            # reported as such; a genuine rejection requires a validated `changes_requested`.
            self._settle_invocation(
                dispatch, review_invocation.outcome, "review invocation did not complete"
            )
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the review invocation did not complete "
                f"({review_invocation.outcome.value}: "
                f"{review_invocation.error_message or 'no detail'}), so it produced no verdict",
            )
        if (
            review_invocation.prompt_digest
            and review_invocation.prompt_digest != reviewer_packet.digest
        ):
            # A verdict produced from a different prompt is not a verdict on this candidate.
            self.store.record_note(
                run_id,
                f"{NOTE_PROMPT_DIGEST}: MISMATCH role=reviewer sent="
                f"{review_invocation.prompt_digest} expected={reviewer_packet.digest}",
            )
            self._settle_invocation(dispatch, review_invocation.outcome, "review prompt mismatch")
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the reviewer invocation received a different prompt than the rendered review "
                "packet, so its verdict does not belong to this candidate",
            )

        try:
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
        finally:
            # One close for every way out of this block, including a vanished verdict and an
            # unexpected error in the evidence write. Without it a reviewer that ran but whose
            # verdict was unusable would leave its invocation open forever, and an open
            # invocation blocks the whole root - a wedge, not a safety property.
            self._settle_invocation(
                dispatch, review_invocation.outcome, "review invocation completed"
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
            # A late success is not applied. The block code is not downgraded either: if the run
            # is already blocked as ``OUTCOME_UNKNOWN`` because the stop was never confirmed
            # ("work may still be running"), that stronger fact is what a reader needs, and
            # overwriting it with a confirmed-looking stop would be the same lie in the other
            # direction. The same rule holds for every other block via ``_blocked``; it is
            # repeated here only to attach the reason the *acceptance* path refused.
            if row["block_code"] == RefusalCode.OUTCOME_UNKNOWN.value:
                return self._outcome_for(
                    run_id,
                    notes=[
                        "a cancellation intent is recorded and the stop was never confirmed; the "
                        "late success was not applied and the run stays outcome_unknown"
                    ],
                )
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
    ) -> RunOutcome:
        """Record a blocked attempt, then end the loop on the stopped-aware block.

        The attempt row is always finished (it is the record of what happened to *that*
        dispatch); the run's own block code goes through :meth:`_blocked`, so a stop recorded
        first keeps deciding the run's state.
        """
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
        return self._blocked(run_id, code, reason)

    def _apply_result_or_stay_stopped(
        self,
        *,
        run_id: str,
        attempt_id: str,
        state: AttemptState,
        outcome: InvocationOutcome,
        result: InvocationResult,
        block_code: RefusalCode | None = None,
        reason: str = "",
    ) -> bool:
        """Apply one invocation result, unless the run was stopped while it was in flight.

        ``True`` when the result was applied to the live attempt; ``False`` when the attempt had
        already been finalized - a cancellation recorded first, and possibly confirmed, is
        exactly that case. That result is then a fact about an invocation which no longer
        decides anything: it is recorded in the run's note table and the run keeps the decision
        already taken. A late success must not overwrite a stop, and a late answer must not
        resurrect a run somebody stopped.

        Without this, the store's compare-and-set raises ``StoreError`` on a state that is
        already correct, and the exception escapes the controller's own loop instead of the run
        simply staying stopped.
        """
        try:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=state,
                outcome=outcome,
                result=result.model_dump(mode="json"),
                block_code=block_code,
            )
        except StoreError as exc:
            self.store.record_note(
                run_id,
                f"{NOTE_LATE_RESULT}: the {outcome.value} result of invocation "
                f"{result.invocation_id} for attempt {attempt_id} arrived after the attempt was "
                f"finalized ({exc}); the run keeps its recorded decision and nothing is "
                "re-dispatched",
            )
            return False
        if block_code is not None:
            self._blocked(run_id, block_code, reason)
        return True
    def _blocked(self, run_id: str, code: RefusalCode, reason: str) -> RunOutcome:
        """End the loop at this block, unless a stop already decided the run's state.

        A stop is a decision about the *whole* run, recorded durably before anything was asked
        to stop. Everything that fails afterwards - a driver error, a protocol failure, a
        rejected review, a stale check - is a later fact about an invocation that the stop
        already ended, and it must not relabel the run: a reader has to see
        ``outcome_unknown`` for a stop that was never confirmed, and ``cancelled_by_operator``
        for one that was.

        The comparison happens **in the write**: ``block_unless_stopped`` carries
        ``WHERE cancel_intent_at IS NULL``, so a stop committed at any point up to this
        statement wins. Reading the row here and then writing unconditionally is the race this
        replaces - a cancel landing between the two would have been overwritten by a late
        ``review_protocol_error``.
        """
        if self.store.block_unless_stopped(run_id, code, reason):
            return self._outcome_for(run_id)
        return self._outcome_for(
            run_id,
            notes=[
                f"the {code.value} block was not recorded: a stop had already decided this run's "
                "state, which no later failure relabels"
            ],
        )

    def _stop_recorded(self, run_id: str) -> bool:
        """Has this run's stop been requested? The question a driver asks before it spawns.

        Deliberately a plain read of one durable column: it is called from a driver's spawn path,
        must not block, and must not start or send anything. It is handed to the driver as a
        callable rather than answered by the controller, so the answer is taken at the instant
        the process would be created instead of when the request was built.
        """
        return bool(self.store.get_run(run_id)["cancel_intent_at"])

    def _refuse_if_stopped(self, run_id: str, *, where: str) -> None:
        """Refuse to start model work for a run whose stop was already requested.

        One read of a durable fact, at the moment of a handoff. It is *not* the coordination
        mechanism by itself - a stop can commit immediately after it - so every handoff that
        registers or starts a role also writes conditionally on the same fact (see
        :meth:`_register_reviewer`), and the run's own block path is conditional too.
        """
        row = self.store.get_run(run_id)
        if not row["cancel_intent_at"]:
            return
        raise RefusedError(
            RefusalCode.CANCELLED_BY_OPERATOR,
            f"a cancellation intent was recorded before {where}; no invocation was started and "
            "none is re-dispatched",
        )

    def _register_reviewer(self, run_id: str, attempt_id: str, invocation_id: str) -> None:
        """Register the reviewer invocation, or refuse because a stop won the handoff.

        This is the atomic half of the handoff, and the reason the earlier read is not enough:
        the registration and the stop decision are one statement, so exactly one of them wins.
        If the stop won, no invocation id is registered and the reviewer is never started - the
        run keeps its stop state and the caller sees a refusal, not a running process.

        A stop that commits *after* this registration sees the reviewer as the live invocation
        and is reported against it, which is the honest target even while the driver is still
        publishing that invocation's process handle.
        """
        if self.store.register_attempt_invocation_unless_stopped(
            run_id, attempt_id, column="review_invocation_id", invocation_id=invocation_id
        ):
            return
        raise RefusedError(
            RefusalCode.CANCELLED_BY_OPERATOR,
            "a cancellation intent was recorded while the reviewer invocation was being "
            "registered, so the registration was refused and no reviewer was started",
        )

    def _advance_to_review(self, run_id: str) -> RunOutcome | None:
        """Enter the review phase, or report why the loop must stop here.

        ``StoreError`` used to escape the run thread when a stop landed while the checks were
        running: the transition asks for ``CHECKING``, and a stopped run is already ``BLOCKED``.
        A transition that cannot happen because the run was stopped is not an error - it is the
        stop, so it reports the run's own recorded state. When it cannot happen for any other
        reason, the run is blocked through the same conditional path as every other failure.
        """
        try:
            self.store.set_task_state(
                run_id,
                [TaskState.CHECKING],
                TaskState.CHECKING,
                phase=CheckPhase.REVIEW,
            )
        except StoreError as exc:
            # ``block_unless_stopped`` is conditional on the same fact the stop writes, so this
            # reports the stop's state when a stop won and this run's own block otherwise.
            return self._blocked(
                run_id,
                RefusalCode.INTERNAL_ERROR,
                f"the run could not enter the review phase: {exc}",
            )
        return None

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
    recorded_config = store.effective_config_for(run_id)
    # Batch E1 ledger facts. A run with no root reports ``None`` and no invocations, which the
    # text projection renders as "not recorded" - rather than as a root with zero usage, which a
    # reader could mistake for a real ledger that has not been spent yet.
    root_row = store.root_budget_for_run(run_id)
    root_usage = store.root_budget_view(str(root_row["root_id"])) if root_row is not None else None
    return RunInspection(
        run=_summary_from_row(row, workspace_matches_receipt=drift),
        task_spec=TaskSpec.model_validate(json.loads(row["task_spec_json"])),
        attempts=attempts,
        evidence=evidence,
        receipt=ResultReceipt.model_validate(json.loads(row["receipt_json"]))
        if row["receipt_json"]
        else None,
        effective_config=recorded_config,
        model_calls_made=0,
        root_budget=root_usage,
        invocations=store.invocations_for(run_id),
        invocation_counts=store.invocation_state_counts(run_id),
    )
