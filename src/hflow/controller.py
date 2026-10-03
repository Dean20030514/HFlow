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

import glob
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from .admission import predictable_dispatch_problems, validate_task_spec
from .authorization import AuthorizationRecord
from .contracts import (
    AttemptState,
    CancellationReceipt,
    CandidateIdentity,
    CandidateSnapshot,
    CheckPhase,
    DeliveryState,
    DispatchReservation,
    DshContextRecord,
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
    RepairContext,
    RepairDecision,
    RepairPolicy,
    RepairRecord,
    RepairTrigger,
    ResultReceipt,
    ReviewResult,
    RootBudgetBinding,
    RootBudgetLimits,
    RunInspection,
    RunRequest,
    RunSummary,
    Scope,
    SpawnFact,
    SpawnKind,
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
from .prepare import resolve_permissions, start_workspace_client_config
from .review import REVIEW_MISSING, review_input_error
from .drivers.base import assert_driver_shape
from .drivers.acpx_dsh import ENV_ALLOW_WRITES
from .drivers.fake import ProcessGuard
from .gitworkspace import (
    GIT_METADATA_CHANGED,
    IGNORED_ARTIFACT_ALLOWLIST,
    CandidateFreeze,
    GitError,
    GitMetadataSnapshot,
    GitRepo,
    GitStatusParseError,
)
from .store import INVOCATION_OPEN_STATES, RunNotFound, Store, StoreError
from .verify import CLEAN_EXIT_REASONS, CheckRunners, failed_check_facts, verify_candidate
from .workspace import (
    DSH_CONTEXT_LIST_SOURCE,
    candidate_fingerprint,
    changed_paths,
    dsh_context_paths,
    expand_scope,
    manifest,
    paths_outside_scope,
)

#: How long a reservation may stay open before it is considered abandoned.
RESERVATION_TTL_SECONDS = 1800

#: A role-packet renderer: the signature every renderer in ``hflow.packet`` shares.
Renderer = Callable[..., RenderedPacket]


class PreparedPacket:
    """One implementer packet rendered for one run identity, plus the workspace it names.

    The workspace is kept next to the packet so ``_drive`` can tell whether the packet it was
    handed describes the workspace the run actually got: if the path changed (a lost insert
    race, a different worktree), the packet is re-rendered rather than sent under a stale path.
    ``deadline_seconds`` is the deadline the packet states, kept for the same reason: a packet
    whose stated deadline is not the one the invocation is given is re-rendered, never sent.
    """

    __slots__ = ("deadline_seconds", "packet", "run_id", "workspace")

    def __init__(self, *, run_id: str, packet: RenderedPacket, deadline_seconds: int) -> None:
        self.run_id = run_id
        self.packet = packet
        self.workspace = _packet_workspace(packet)
        self.deadline_seconds = int(deadline_seconds)


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


def _change_list(items: list[str]) -> str:
    """At most five changed labels, then a count: what a block reason or a note names."""
    return ", ".join(items[:5]) + (f" (+{len(items) - 5} more)" if len(items) > 5 else "")


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
#: One repair decision (batch E2) - allowed or refused, with its reason. Recorded as a structured
#: record *and* as a note, so an operator grepping a run finds the decision where they look while
#: `status`/`report` can still print it as a decision rather than a log line.
NOTE_REPAIR = "repair"
#: The shared Git metadata HFlow's own git reads (``GitRepo.metadata_snapshot``). The digest is
#: taken before the first dispatch; each later comparison that found a change records the digests
#: and the changed keys and files, never values.
NOTE_GIT_METADATA = "git_metadata"


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
    try:
        return candidate_fingerprint(root, spec.scope) == receipt.candidate.fingerprint
    except RefusedError:
        # A scoped entry now resolves outside the workspace: the accepted content can no longer
        # be read where it was frozen, which is drift, not a reason for ``status`` to fail.
        return False


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
        #:
        #: Without an explicit one it is the directory of this run's own ledger, so check
        #: artifacts stay next to the database that references them. The platform default is
        #: only for a ledger that has no directory (an in-memory store).
        self.data_dir = Path(data_dir) if data_dir else self._ledger_data_dir(store)
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

    @staticmethod
    def _ledger_data_dir(store: Store) -> Path:
        """The data dir a ledger file implies (``<data_dir>/hflow.sqlite``), else the default."""
        path = getattr(store, "path", None)
        if path is None or str(path) == ":memory:":
            return default_data_dir()
        return Path(path).parent

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
            self._assert_root_repair_allowance(run_id, spec)
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
        repo, worktree_root = self._worktree_path(run_id, spec, request.base_commit)
        implementer_packet = self._render_implementer_packet(
            run_id=run_id,
            spec=spec,
            workspace=str(worktree_root) if worktree_root is not None else str(project_root),
            # The deadline the first invocation will actually be given (capped by the root), so
            # the worker is never told it has more time than is enforced.
            deadline_seconds=self._capped_deadline(request.deadline_seconds),
            writes_allowed=resolve_permissions(spec)[0],
        )
        # Every refusal that can be decided by reading runs before anything is written, so a
        # refused run records nothing: no root row, no authorization, no run row. A root row
        # written first would lock the root at the ceilings the run was refused for (there is no
        # top-up path), and a run row would turn an identical resubmission into a history query
        # returning the blocked run - either way the refusal's own advice could not work.
        self._assert_dispatch_preconditions(
            spec, project, request.deadline_seconds, base_commit=request.base_commit
        )
        self._assert_allowance_for(run_id, spec, project)
        if self.root_binding is not None:
            # A root recorded with other ceilings (or another root for this task) is reported as
            # that, before the repair gate reads the recorded ceilings.
            self._check_root_registration()
        else:
            self._assert_no_root_for_task(spec, project)
        self._assert_root_repair_allowance(run_id, spec)
        # The root is registered before the run row exists: its ceilings are an admission
        # precondition, and refusing them after the run row was written would leave a run
        # pointing at a root nothing agreed to. Registration repeats the read-only check inside
        # its own transaction, so a concurrent registration is still refused here.
        if self.root_binding is not None:
            self._register_root_budget()

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
            "no result was applied for this invocation - none was observed, or one arrived after "
            "the run's stop and is recorded as a late_result note; reconciled by an operator. "
            "The consumption stands and this root does not re-dispatch.",
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

        Idempotent: a recorded receipt short-circuits once the run has ended. No prompt is sent,
        no budget is charged, and a confirmed stop is reported as a *local process* fact - never
        as a successful protocol cancellation or a known business result.

        The stop is routed to the role that is actually running. Both roles are separate
        invocations, and a machine profile may bind them to separate driver objects, so the
        invocation id and the driver are resolved together from stored facts rather than
        assumed to be the implementer's.

        What the stop may change depends on the run's state once the intent is durable:

        * a **live** run ends here. Its receipt and its terminal block - ``cancelled_by_operator``
          for a confirmed stop, ``outcome_unknown`` for one that was not - are written in *one*
          statement (``record_cancel_outcome``), *before* the attempt and ledger bookkeeping.
          The receipt is what makes a repeated ``cancel`` return early, so it never exists
          without the block, and a failure in the bookkeeping after it becomes a run note. If
          that one write fails, only the intent is left, and the next ``cancel`` asks again and
          ends the run;
        * a receipt found on a run that is **still live** (a database written while the receipt
          and the block were separate writes) is not returned as if the stop were finished: the
          block it implies is re-applied - with the attempt and ledger bookkeeping of a confirmed
          stop, when it was one - and the driver is not asked again;
        * a run that **already ended** (``BLOCKED`` or ``CANCELLED``) keeps the outcome it ended
          with. The invocation it was on is still asked to stop - a child can outlive an unknown
          or a failed result - and the receipt is recorded with ``run_already_ended`` set, but
          the block is not relabelled and no ledger entry is touched: an ``unknown`` entry stays
          for an operator's reconcile, so ``resume`` still reconciles an ``outcome_unknown`` run,
          and a settled entry stays settled.
        """
        intent_at, existing_receipt = self.store.cancel_state(run_id)
        if existing_receipt is not None:
            recorded_row = self.store.get_run(run_id)
            if TaskState(recorded_row["task_state"]) in {
                TaskState.ACCEPTED,
                TaskState.BLOCKED,
                TaskState.CANCELLED,
            }:
                return existing_receipt
            return self._reapply_recorded_stop(run_id, recorded_row, existing_receipt)
        row = self.store.get_run(run_id)
        if TaskState(row["task_state"]) is TaskState.ACCEPTED:
            return self._stop_after_acceptance(run_id)
        intent_at = self.store.record_cancel_intent(run_id)
        # Read again now that the intent is durable. From here on the run's own block and its
        # acceptance can no longer be written - both are conditional on this intent - so this is
        # the state the stop decides from, not one a run thread replaced a moment ago.
        row = self.store.get_run(run_id)
        state = TaskState(row["task_state"])
        if state is TaskState.ACCEPTED:
            return self._stop_after_acceptance(run_id)
        ended = state in {TaskState.BLOCKED, TaskState.CANCELLED}

        attempt = self.store.open_attempt(run_id)
        role, driver, active_invocation = self._active_invocation(row, attempt)
        if not active_invocation:
            if ended:
                detail = (
                    f"the run was already {state.value} and no invocation was dispatched; "
                    "nothing was stopped"
                )
            elif attempt is not None:
                # A rootless dispatch reserves the attempt first and registers its invocation
                # afterwards, conditionally on this stop: the stop landed in between.
                detail = (
                    f"the stop was recorded after attempt {attempt['attempt_id']} was reserved and "
                    "before its invocation was registered; no process was created (the "
                    "registration and the driver's spawn gate are both conditional on the stop); "
                    "the reserved turn and authorization submission stay consumed"
                )
            else:
                detail = "run cancelled before any invocation was dispatched"
            receipt = CancellationReceipt(
                invocation_id="",
                status="confirmed_stopped",
                mechanism="none",
                local_process_stopped=True,
                detail=detail,
                run_already_ended=ended,
            )
            if ended:
                self.store.record_cancel_receipt(run_id, receipt)
                self._note_stop_of_ended_run(run_id, row)
                return receipt
            self.store.record_cancel_outcome(
                run_id,
                receipt,
                RefusalCode.CANCELLED_BY_OPERATOR,
                f"cancelled before dispatch at {intent_at}",
            )
            self._finish_live_attempt(run_id, attempt, receipt)
            return receipt

        receipt = self._driver_cancel(driver, active_invocation)
        target_note = (
            f"{NOTE_CANCEL_TARGET}: role={role} invocation={active_invocation} "
            f"reported={receipt.status} mechanism={receipt.mechanism}"
        )
        if ended:
            receipt = receipt.model_copy(update={"run_already_ended": True})
            self.store.record_cancel_receipt(run_id, receipt)
            self.store.record_note(run_id, target_note)
            self._note_stop_of_ended_run(run_id, row)
            return receipt
        if receipt.status != "confirmed_stopped":
            self.store.record_cancel_outcome(
                run_id,
                receipt,
                RefusalCode.OUTCOME_UNKNOWN,
                f"stop could not be confirmed for the {role} invocation {active_invocation} "
                f"({self._driver_label(driver, role)}): {receipt.status}; work may still be running",
            )
            self.store.record_note(run_id, target_note)
            return receipt
        self.store.record_cancel_outcome(
            run_id,
            receipt,
            RefusalCode.CANCELLED_BY_OPERATOR,
            f"stop confirmed ({receipt.mechanism}) for the {role} invocation "
            f"{active_invocation} ({self._driver_label(driver, role)}); local execution "
            "stopped, business result unknown",
        )
        self.store.record_note(run_id, target_note)
        self._finish_confirmed_stop(run_id, attempt, active_invocation, receipt)
        return receipt

    def _reapply_recorded_stop(
        self, run_id: str, row: object, receipt: CancellationReceipt
    ) -> CancellationReceipt:
        """Finish a stop whose receipt was recorded and whose block was not.

        The receipt is the answer the driver already gave, so the driver is not asked again: a
        confirmed stop blocks ``cancelled_by_operator`` and gets the attempt and ledger
        bookkeeping of one; anything else blocks ``outcome_unknown`` and leaves both alone -
        work may still be running, which is the state ``resume`` reconciles.
        """
        attempt = self.store.open_attempt(run_id)
        role, driver, active_invocation = self._active_invocation(row, attempt)
        invocation_id = receipt.invocation_id or active_invocation
        confirmed = receipt.status == "confirmed_stopped"
        if confirmed:
            self.store.set_blocked(
                run_id,
                RefusalCode.CANCELLED_BY_OPERATOR,
                f"stop confirmed ({receipt.mechanism}) for the {role} invocation "
                f"{invocation_id or '(none registered)'} ({self._driver_label(driver, role)}); "
                "local execution stopped, business result unknown",
            )
        else:
            self.store.set_blocked(
                run_id,
                RefusalCode.OUTCOME_UNKNOWN,
                f"stop could not be confirmed for the {role} invocation {invocation_id} "
                f"({self._driver_label(driver, role)}): {receipt.status}; work may still be "
                "running",
            )
        self.store.record_note(
            run_id,
            f"{NOTE_CANCEL_TARGET}: the block was re-applied from the recorded receipt "
            f"(reported={receipt.status} mechanism={receipt.mechanism}), which had been recorded "
            "without it; the driver was not asked again",
        )
        if confirmed:
            if invocation_id:
                self._finish_confirmed_stop(run_id, attempt, invocation_id, receipt)
            else:
                self._finish_live_attempt(run_id, attempt, receipt)
        return receipt

    def _finish_live_attempt(
        self, run_id: str, attempt: object, receipt: CancellationReceipt
    ) -> None:
        """Finish an attempt a confirmed stop ended, if it is still ``CREATED`` or ``ACTIVE``.

        The plain form of ``finish_attempt``: this *is* the stop's bookkeeping. A run thread
        that finished the attempt first wins, and the receipt stands either way.
        """
        if attempt is None:
            return
        data = dict(attempt)  # type: ignore[arg-type]
        if data.get("state") not in {AttemptState.CREATED.value, AttemptState.ACTIVE.value}:
            return
        try:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=str(data["attempt_id"]),
                state=AttemptState.CANCELLED,
                outcome=InvocationOutcome.CANCELLED,
                result=receipt.model_dump(mode="json"),
                block_code=RefusalCode.CANCELLED_BY_OPERATOR,
            )
        except StoreError:
            pass  # the run thread finished it first; the receipt is still recorded

    def _finish_confirmed_stop(
        self,
        run_id: str,
        attempt: object,
        invocation_id: str,
        receipt: CancellationReceipt,
    ) -> None:
        """The attempt and ledger bookkeeping of a confirmed stop, after the run is blocked."""
        try:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt["attempt_id"],  # type: ignore[index]
                state=AttemptState.CANCELLED,
                outcome=InvocationOutcome.CANCELLED,
                result=receipt.model_dump(mode="json"),
                block_code=RefusalCode.CANCELLED_BY_OPERATOR,
            )
        except StoreError:
            pass  # the attempt may already be terminal; the receipt is still recorded
        # The ledger records the same fact: the allowance this dispatch consumed stays consumed,
        # and the entry is closed if it is still open, so it does not keep the root blocked for
        # nothing. A stopped invocation is not an unknown one.
        try:
            self._settle_cancelled_invocation(run_id, invocation_id, receipt)
        except StoreError as exc:
            self.store.record_note(
                run_id,
                f"{NOTE_DISPATCH}: the confirmed stop could not record the ledger entry of "
                f"invocation {invocation_id} ({exc}); the run is stopped and blocked "
                f"{RefusalCode.CANCELLED_BY_OPERATOR.value}, and the entry keeps the state it "
                "had. An open entry keeps blocking the root, and this build has no command that "
                "closes it for such a run: `resume` reconciles only an outcome_unknown run",
            )

    def _stop_after_acceptance(self, run_id: str) -> CancellationReceipt:
        """Record a stop requested for an ``ACCEPTED`` run. History is not rewritten.

        The request is a fact worth keeping, but it cannot un-accept a delivered candidate, and
        nothing is asked to stop.
        """
        receipt = CancellationReceipt(
            invocation_id="",
            status="confirmed_stopped",
            mechanism="none",
            local_process_stopped=True,
            detail="the run was already ACCEPTED and its candidate frozen; nothing was stopped "
            "and the delivery stands",
            run_already_ended=True,
        )
        self.store.record_cancel_receipt(run_id, receipt)
        self.store.record_note(
            run_id, "a stop was requested after acceptance; the accepted candidate is unchanged"
        )
        return receipt

    def _note_stop_of_ended_run(self, run_id: str, row: object) -> None:
        """Record that a stop reached a run that had already ended, and changed nothing."""
        data = dict(row)  # type: ignore[arg-type]
        self.store.record_note(
            run_id,
            f"a stop was requested for a run already {data.get('task_state')} "
            f"({data.get('block_code') or 'no block code'}); the run keeps that outcome and its "
            "ledger entries stand - a stop neither relabels nor settles a run that already ended",
        )

    def _settle_cancelled_invocation(
        self, run_id: str, invocation_id: str, receipt: CancellationReceipt
    ) -> None:
        """Close the ledger entry a confirmed stop ended, if that entry is still open.

        Only for a *confirmed* stop: an unconfirmed one stays open on purpose, because "work may
        still be running" is exactly the state that must keep blocking. The consumption is never
        returned - the allowance was committed before the process existed - so this records the
        outcome, not a refund.

        Only an **open** entry (``reserved``, ``requested``, ``started``) is closed. Every other
        state already records what is known, and a local stop is not evidence against it:
        ``not_started`` (the driver's gate reported that no launch happened - the stop won the
        handoff), ``settled`` (a result was applied before the stop arrived), ``unknown`` and
        ``launch_unknown`` (closed only by an operator's reconcile). Rewriting one of those
        either invents a fact or unblocks a root that must stay blocked, so the stop leaves it and
        records a note instead.

        Three shapes for an open entry, and the difference between the last two is the point:

        * a launch is recorded (``started_at`` set) -> the launch happened, so the entry is
          settled as cancelled;
        * **no launch was ever requested** (``launch_requested_at`` is NULL) -> nothing was asked
          of any driver, so ``not_started`` is a fact: the allowance bought nothing at all;
        * a launch *was* requested and no report came back -> nobody may say whether a process
          exists, and a confirmed stop of a real child is direct evidence that one did. This is
          ``launch_unknown``: the root stays blocked (a late spawn report can still record what
          the driver saw, see ``Store.record_invocation_spawn``; no command closes it). Recording
          ``not_started`` here would be claiming, from an empty timestamp, that no driver was ever
          asked - which is false, and was reproduced against a real forced stop of a real pid.
        """
        recorded = self.store.invocation(invocation_id) if invocation_id else None
        if recorded is None:
            # A legacy run: its dispatch facts live on the attempt row, and there is no ledger
            # entry to close.
            return
        if recorded.state.value not in INVOCATION_OPEN_STATES:
            self.store.record_note(
                run_id,
                f"{NOTE_DISPATCH}: the confirmed stop ({receipt.mechanism}) left invocation "
                f"{invocation_id} {recorded.state.value}; a stop closes only an open entry, and "
                "this one already records what is known",
            )
            return
        if recorded.started_at is not None:
            if not self.store.settle_invocation(
                invocation_id,
                outcome=InvocationOutcome.CANCELLED,
                detail=f"stop confirmed ({receipt.mechanism}) for run {run_id}",
            ):
                self._note_not_settled(run_id, invocation_id, InvocationOutcome.CANCELLED)
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
        self,
        spec: TaskSpec,
        project: ProjectConfig,
        deadline_seconds: int,
        *,
        base_commit: str = "",
    ) -> None:
        """Refuse a run that is already known to be unable to finish, before anything is spent.

        The rules themselves live in ``admission.predictable_dispatch_problems``, which
        ``hflow prepare`` calls too: a preview that reported "admitted" for a task this gate
        would refuse would be answering a different question than the user asked. This method
        only supplies the resolved facts - the write permission, the launches, and (for a real
        transport) whether the starting workspace at ``base_commit`` already holds the client's
        project config - and turns the first problem into a refusal.
        """
        real_transport = self._real_transport()
        problems = predictable_dispatch_problems(
            spec,
            project,
            production=self.production,
            implementer_writes=resolve_permissions(spec)[0],
            launches=self._resolved_launches(),
            real_transport=real_transport,
            root_bound=self.root_binding is not None,
            workspace_client_config=(
                start_workspace_client_config(spec, self.project_root, base_commit)
                if real_transport and self.project_root is not None
                else ""
            ),
        )
        if problems:
            first = problems[0]
            more = f" (+{len(problems) - 1} more admission problem(s))" if len(problems) > 1 else ""
            raise RefusedError(first.code, first.detail + more)

    def _real_transport(self) -> bool:
        """Is any role dispatched to something other than the offline fake driver?

        Read from the driver objects this run holds and from the resolved configuration, so a
        driver that does not name itself, or a configuration naming a real transport, counts as
        real. Deliberately not ``production``: that flag says whether real checks run, while the
        repair rule that uses this answer is about where a model call can go.
        """
        from .drivers.selected import FAKE_ALIASES

        if any(
            getattr(driver, "driver_id", "") not in FAKE_ALIASES
            for driver in (self.driver, self.reviewer_driver)
        ):
            return True
        if self.effective_config is None:
            return False
        return any(entry.driver_id not in FAKE_ALIASES for entry in self.effective_config.roles)

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
        """How many top-level invocations this run may need, worst case.

        Without a repair policy it is the fixed loop: implementer (+ reviewer). With one, the
        worst case is the plan's four - I1 + R1 + I2 + R2 - because a repair can follow a reviewer
        rejection, and that is the number the root, the run and the authorization all have to
        cover before the first dispatch. An attempt that is never needed simply is not bought:
        this is a ceiling, not a quota.
        """
        review_turns = 1 if spec.needs_review(project) else 0
        if spec.repair_policy is None:
            return 1 + review_turns
        # I1 + (R1) + I2 + (R2): the repair attempt and the review that must follow it are the
        # ones the first attempt cannot know about, so they are priced up front.
        return 2 * (1 + review_turns)

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

    def _assert_root_repair_allowance(self, run_id: str, spec: TaskSpec) -> None:
        """Refuse an armed repair policy the root's repair counter cannot pay for, before I1.

        The worst-case gate prices top-level submissions only; the repair counter is enforced by
        the repair's own reservation. Without this check a root with no repair left (``max_repairs``
        defaults to 0) would buy I1 - and R1 on the reviewer path - and only then discover that
        the repair it armed for cannot be bought.

        What this run may need, read from the ledger's own rule: the repair itself, plus one more
        when another run already dispatched an implementer on this root, because the ledger
        charges a later revision's first implementer as a repair too. A run whose own implementer
        is already recorded is past I1, so the repair decision checks the counter from there.
        The dispatch transaction stays the real gate; this removes the predictable case.
        """
        if self.root_binding is None or self.root_limits is None or spec.repair_policy is None:
            return
        implementers = [
            entry
            for entry in self.store.invocations_for_root(self.root_binding.root_id)
            if entry.role == "implementer"
        ]
        if any(entry.run_id == run_id for entry in implementers):
            return
        view = self.store.root_budget_view(self.root_binding.root_id)
        used = view.used_repairs if view is not None else 0
        limit = view.limits.max_repairs if view is not None else self.root_limits.max_repairs
        needed = 1 + (1 if implementers else 0)
        if used + needed > limit:
            first = (
                "; this revision's first implementer is itself charged as a repair, because an "
                "earlier run already dispatched one on this root"
                if implementers
                else ""
            )
            raise RefusedError(
                RefusalCode.BUDGET_EXHAUSTED,
                f"root {self.root_binding.root_id} has used {used} of its {limit} repair "
                f"attempt(s), and this task's repair policy needs {needed} more{first}. Nothing "
                "was dispatched and no allowance was consumed; obtain a root budget whose "
                "max_repairs covers the repair, or drop repair_policy.",
            )

    def _worktree_path(
        self, run_id: str, spec: TaskSpec, resolved_base: str = ""
    ) -> tuple[GitRepo | None, Path | None]:
        """Where this run's isolated worktree *would* go, without creating anything.

        The implementer packet contains the workspace path, and on Windows a worktree path is
        long enough to change whether the packet fits its bound. The path is therefore derived
        here - from the run id and the repository root, the same inputs ``create_worktree`` uses
        - so the packet that is checked is the packet that will be sent.

        Refusals that a worktree run cannot survive are raised here, before the run row exists:
        a repository that cannot be discovered, and a base commit that does not exist. Both are
        knowable without side effects, and both used to be discovered after the run (and, with
        an authorization, after a claim). ``resolved_base`` is the commit the caller already
        resolved the task's base to (``RunRequest.base_commit``); it is checked the same way.
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
            base_commit = (
                resolved_base or spec.workspace.base_commit or repo.resolve_commit("HEAD")
            )
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
        repair: RepairContext | None = None,
    ) -> PreparedPacket:
        """Render the implementer packet for this run, or refuse before anything is claimed.

        ``writes_allowed`` is the run's *effective* permission, not "the scope lists a path".
        The packet tells the worker whether it may change files at all, so rendering it from
        anything other than the permission the dispatch will carry would tell the worker
        something the transport does not honour - and ``prepare`` reports the same packet, so
        the preview and the dispatch have to agree byte for byte.

        ``repair`` is present only for the second attempt of a run. Its section is rendered from
        the context the repair decision recorded, so the facts an agent is handed and the facts an
        operator reads are the same facts. An oversized packet is refused here, before the
        dispatch transaction, so a repair that cannot be described costs nothing.
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
                repair=repair,
            )
        except PacketTooLargeError as exc:
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                f"the {'repair ' if repair is not None else ''}implementer input packet for this "
                f"run does not fit, so nothing was dispatched and no allowance was consumed: {exc}",
            ) from exc
        return PreparedPacket(run_id=run_id, packet=packet, deadline_seconds=deadline_seconds)

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

    def _check_root_registration(self) -> None:
        """Refuse, without writing, a root ``_register_root_budget`` would refuse.

        Asked before the repair gate, which reads the recorded ceilings: a run presenting other
        ceilings than the recorded ones is told that, not that the recorded root has no repair.
        """
        assert self.root_binding is not None and self.root_limits is not None
        try:
            self.store.check_root_registration(self.root_binding, self.root_limits)
        except StoreError as exc:
            raise RefusedError(RefusalCode.BUDGET_EXHAUSTED, str(exc)) from exc

    def _assert_no_root_for_task(self, spec: TaskSpec, project: ProjectConfig) -> None:
        """Refuse a rootless run of a task that already has a root, before its run row exists.

        The rootless dispatch transaction refuses the same thing, but only after ``create_run``:
        the refusal then becomes a blocked run under this spec digest, and resubmitting the same
        TaskSpec with ``--root-budget-file`` - the refusal's own advice - would only return it.
        Read here, the refusal records nothing; the transaction's check stays the race backstop.
        """
        roots = self.store.root_ids_for_task(
            project_id=project.project_id,
            repo_path=str(self.project_root) if self.project_root is not None else "",
            task_id=spec.task_id,
        )
        if roots:
            raise RefusedError(
                RefusalCode.BUDGET_EXHAUSTED,
                f"the task {spec.task_id} of {project.project_id} has root {roots[0]} in this "
                "ledger, and this run has no root binding: a dispatch outside the root would "
                "step around its unresolved invocations, its live run and its ceilings. Nothing "
                "was recorded or dispatched; pass --root-budget-file (with an authorization "
                "covering that root) to run this task against its root.",
            )

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
        is_repair: bool,
    ) -> DispatchReservation:
        """The one dispatch transaction, for both roles.

        Everything a dispatch costs and everything that proves it happened commit together: the
        authorization's submission, the run's reserved turn, the root's consumption (when this
        run has a root) and the invocation record. A refusal raises ``RefusedError`` carrying the
        store's own message, and the caller blocks the run - nothing is dispatched, no driver is
        built and no process is created.

        ``required_loop_remaining`` is what the root must still be able to afford: this dispatch
        plus every further top-level dispatch this run may need. With a repair policy enabled the
        worst case is four (I1 + R1 + I2 + R2), priced from the first attempt, so a run that could
        only ever afford half a loop is refused before anything is bought - which is the gate the
        plan asks for, not a step discovered after the implementer has been paid.

        ``is_repair`` travels with the reservation so the ledger charges the root's repair counter
        for this dispatch rather than inferring it from a count taken elsewhere.
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
                is_repair=is_repair,
                # Lets the rootless path see a root this task already has in the ledger.
                repo_path=str(self.project_root) if self.project_root is not None else "",
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
            or "repair attempt(s)" in message
            or "pass --root-budget-file" in message
        ):
            return RefusalCode.BUDGET_EXHAUSTED
        return RefusalCode.INTERNAL_ERROR

    def _spawn_reporter(self, reservation: DispatchReservation) -> SpawnReporter:
        """The callback a driver uses to report what it observed at its spawn decision.

        Wired into every ``InvocationRequest`` this controller builds. A driver that calls it
        turns "we asked for a launch" into either "a process exists" or "no process was created".
        For a driver that does not, ``_confirm_driver_ran`` reads its result: completed work is
        recorded as a launch with spawn kind unknown and no process, a cancelled result with no
        work as ``not_started``, and anything else stays ``REQUESTED`` - an unconfirmed launch,
        not a start.

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
        """Close the dispatch record with the outcome the caller classified the turn as, if it is
        still open.

        That is the driver's reported outcome unless the controller concluded otherwise - a
        reviewer turn it refuses as unknown is settled as ``OUTCOME_UNKNOWN``.

        Never refunds: an ``OUTCOME_UNKNOWN`` becomes ``unknown``, which keeps blocking the root
        until an operator reconciles it. The store settles only an open entry, so a result that
        arrives after a confirmed stop already closed the entry - as ``launch_unknown`` after a
        forced stop of an unrecorded launch, as ``settled`` after an ordinary one - leaves that
        state as it is and is recorded as a note. (Both roles call this only after their result
        was applied, see :meth:`_apply_result_or_stay_stopped` and :meth:`_late_review_result`; a
        stop recorded first leaves the entry untouched.) A failure to record the settlement is not
        allowed to replace the result the caller already has - it is reported as a note instead.
        """
        if reservation.invocation is None:
            return
        invocation_id = reservation.invocation.invocation_id
        try:
            settled = self.store.settle_invocation(invocation_id, outcome=outcome, detail=detail)
        except StoreError as exc:
            self.store.record_note(
                reservation.invocation.run_id,
                f"{NOTE_DISPATCH}: invocation {invocation_id} could not be "
                f"settled ({exc}); its consumption stands and the root keeps it open",
            )
            return
        if not settled:
            self._note_not_settled(reservation.invocation.run_id, invocation_id, outcome)

    def _note_not_settled(
        self, run_id: str, invocation_id: str, outcome: InvocationOutcome | None
    ) -> None:
        """Record a settlement the ledger refused because the entry was no longer open."""
        recorded = self.store.invocation(invocation_id)
        state = recorded.state.value if recorded is not None else "unrecorded"
        reported = outcome.value if outcome is not None else "no outcome"
        self.store.record_note(
            run_id,
            f"{NOTE_DISPATCH}: invocation {invocation_id} was not settled as {reported}: the "
            f"ledger already records it as {state}, and that state stands",
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
                "which keeps blocking the root. This build has no command that closes it for a "
                "run blocked by a driver failure: `resume` reconciles only an outcome_unknown run",
            )
        except StoreError:
            pass  # a broken note write must not change what the caller reports

    def _record_interruption(self, run_id: str, attempt_id: str, role: str) -> None:
        """Leave a reconcilable run when ``KeyboardInterrupt``/``SystemExit`` escapes a ``start``.

        This controller will never observe the invocation's result, which is exactly the state
        ``resume`` reconciles: the run blocks ``outcome_unknown`` (unless a stop already decided
        it), every ledger entry still open becomes ``unknown`` or ``launch_unknown`` - so a root
        stays blocked and nothing is refunded - and the attempt is finished as an unknown outcome.
        The block is written first, so a failure in the bookkeeping after it still leaves a run
        ``resume`` acts on. Every write is best effort: the interrupt is what the caller must see,
        and a store error here must not replace it.

        A hard crash (a kill, a power loss) runs none of this; such a run stays ``RUNNING``.
        """
        reason = (
            f"controller interrupted during {role} invocation; its result was never observed. "
            "No re-dispatch: `hflow resume` reconciles it"
        )
        try:
            self.store.block_unless_stopped(run_id, RefusalCode.OUTCOME_UNKNOWN, reason)
        except Exception:  # noqa: BLE001 - never mask the interrupt
            pass
        try:
            self.store.mark_unsettled_invocations_unknown(run_id, reason)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.store.finish_attempt(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.OUTCOME_UNKNOWN,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                result={"error": reason},
                block_code=RefusalCode.OUTCOME_UNKNOWN,
                unless_stopped=True,
            )
        except Exception:  # noqa: BLE001 - not live any more, or a stop decided it
            pass

    def _release_invocation(self, driver: HarnessDriver, run_id: str, invocation_id: str) -> None:
        """Tell the driver an invocation's result is on record, so it can close what it opened.

        Called after the result is applied, never before. ``release`` is not part of
        :class:`HarnessDriver`, so a driver without it is simply not asked. A release that fails
        is cleanup that failed, not a result: it is noted and changes nothing already decided.
        """
        if not hasattr(driver, "release"):
            return
        try:
            driver.release(invocation_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - cleanup must not rewrite a recorded decision
            try:
                self.store.record_note(
                    run_id,
                    f"{NOTE_DISPATCH}: the driver could not release invocation {invocation_id} "
                    f"({exc!r}); its recorded result stands",
                )
            except StoreError:
                pass

    def _confirm_driver_ran(
        self,
        reservation: DispatchReservation,
        result: InvocationResult,
        *,
        stop_before_launch: bool = False,
    ) -> None:
        """Decide what a driver's return means when it did not report a spawn fact.

        Only for a driver that does not implement ``on_spawn`` (an older or third-party one), and
        only when no report arrived: a driver that reports *has* answered, whatever its report
        says, and inferring on top of that answer would let a silent-looking return overwrite a
        recorded "no process was created". "No report arrived" is read from the row itself - still
        ``requested``, no start time and no spawn kind - and not from ``launch_requested_at``,
        which the controller always sets before asking a driver. The two readable shapes for a
        silent driver are:

        * a completed invocation that produced work - a candidate, a review verdict or observed
          agent turns - so the launch happened and it is recorded as started. Whether that launch
          created an operating-system child is not known, so no process and no spawn kind are
          claimed for it;
        * a cancelled invocation that produced no work at all **and** a stop that was recorded
          before the driver was asked to launch (``stop_before_launch``, read by the caller between
          the launch request and ``start``). Under the ``stop_requested`` contract the driver's
          gate then refused, so nothing ran and it is recorded as never started.

        Anything else stays ``requested``: guessing in either direction is what produced both of
        the bugs this replaces. In particular a cancelled, workless return after a stop recorded
        *while the role ran* is not evidence that nothing launched - a silent driver may have
        worked and then honoured the stop. That entry is left to the stop's own bookkeeping: a
        confirmed stop records ``launch_unknown``, an unconfirmed one leaves it open (blocking the
        root) for ``resume``. This method runs before the stop-conditional apply, so it must not
        close an entry the stop decides.
        """
        if reservation.invocation is None:
            return
        invocation_id = reservation.invocation.invocation_id
        current = self.store.invocation(invocation_id)
        if current is None:
            return
        if (
            current.state is not InvocationStartState.REQUESTED
            or current.started_at is not None
            or current.spawn_kind is not SpawnKind.UNKNOWN
        ):
            # A driver answered - a launch, a "nothing was created", or a row that has already
            # gone further. An inference is not evidence, and it never overrides one.
            return
        produced_work = bool(
            result.candidate is not None or result.review is not None or result.agent_turns
        )
        if result.outcome is InvocationOutcome.COMPLETED and produced_work:
            self.store.record_invocation_spawn(
                SpawnFact(
                    invocation_id=invocation_id,
                    created=True,
                    pid=None,
                    spawn_kind=SpawnKind.UNKNOWN,
                    detail=(
                        "inferred from completed work: the driver reported no spawn fact, so "
                        "whether its launch created a process is not known"
                    ),
                )
            )
        elif (
            result.outcome is InvocationOutcome.CANCELLED
            and not produced_work
            and stop_before_launch
        ):
            self.store.mark_invocation_not_started(
                invocation_id,
                "the stop was recorded before the driver was asked to launch, and the driver "
                "returned a cancelled invocation that produced no work and no process",
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
        """Drive this run: at most two implementer attempts, then a verdict.

        Setup happens once - the workspace, the permissions, the budget gate that covers the worst
        case - and the per-attempt work lives in :meth:`_attempt_cycle`. The loop is bounded by the
        plan's rule that a run buys **one** repair, and every way it can refuse to spend that
        second attempt is recorded as a decision rather than disappearing into a log line.

        Setup and cycle are split deliberately: a repair must not re-create the worktree, re-claim
        the run or re-run the admission gates, and a second cycle must never be reachable from an
        exception path.
        """
        spec = request.task
        project = request.project
        project_root = Path(request.project_root)
        policy = self._repair_policy(spec, project)

        # --- workspace preparation (M2): an isolated worktree, or the project in place
        # Created once for the run, before the first dispatch: a repair reuses this worktree
        # rather than making a second one, and nothing here is repeated per attempt.
        repo: GitRepo | None = None
        worktree: Path | None = None
        user_tree_before = ""
        dirty_target = False
        # The original base, resolved to a commit exactly once, here, before the worktree exists.
        # A ref is a name, not a base: inside the worktree 'HEAD' is the candidate itself, and a
        # branch such as 'main' can move while the run works. Every later use - the worktree, each
        # round's CandidateIdentity, the repair context, the delivery diff - takes this commit.
        # ``request.base_commit`` is the commit the caller already resolved (and checked its
        # approval against); it is used as given rather than resolved from the name again.
        resolved_base = ""
        git_metadata_before: GitMetadataSnapshot | None = None
        if spec.workspace.mode == "worktree":
            base_name = spec.workspace.base_commit or "HEAD"
            try:
                repo = GitRepo.discover(project_root)
                user_tree_before = repo.user_change_fingerprint()
                resolved_base = repo.resolve_commit(request.base_commit or base_name)
                worktree = repo.create_worktree(run_id, resolved_base)
                self.store.record_worktree(run_id, worktree)
                # Taken after HFlow's own ``worktree add`` - which can itself write the shared config
                # (``extensions.relativeWorktrees`` under ``worktree.useRelativePaths``) and the
                # run's ``config.worktree`` - and before anything HFlow does not control runs.
                git_metadata_before = repo.metadata_snapshot(worktree)
            except (GitError, RefusedError) as exc:
                return self._blocked(run_id, RefusalCode.INTERNAL_ERROR, f"git workspace failed: {exc}")
            self.store.record_note(
                run_id,
                f"{NOTE_GIT_METADATA}: snapshot before dispatch {git_metadata_before.digest} "
                f"({len(git_metadata_before.entries)} entries)",
            )
            if base_name != resolved_base:
                self.store.record_note(run_id, f"base {base_name!r} -> {resolved_base}")
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

        # The original Base is fixed before the first dispatch and never changes: it is what the
        # final delivery diff is taken against, and what tells the repair round where it started.
        # A worktree run uses the commit resolved above; an in-place run has no worktree base.
        original_base_commit = resolved_base if repo is not None else spec.workspace.base_commit
        original_base_ref = f"base:{spec.task_id}:{spec.revision}"

        previous: CandidateIdentity | None = None
        trigger: RepairTrigger | None = None
        repair_context: RepairContext | None = None
        round_number = 1
        while True:
            cycle = self._attempt_cycle(
                run_id,
                request,
                repo=repo,
                worktree=worktree,
                execution_root=execution_root,
                user_tree_before=user_tree_before,
                dirty_target=dirty_target,
                implementer_writes=implementer_writes,
                implementer_packet=implementer_packet,
                previous=previous,
                trigger=trigger,
                round_number=round_number,
                original_base_commit=original_base_commit,
                original_base_ref=original_base_ref,
                policy=policy,
                repair_context=repair_context,
                git_metadata_before=git_metadata_before,
            )
            if cycle.repair is None:
                if cycle.outcome is None:  # pragma: no cover - the cycle always returns one
                    raise RefusedError(
                        RefusalCode.INTERNAL_ERROR,
                        "the attempt cycle returned neither an outcome nor a repair context",
                    )
                if (
                    repo is not None
                    and worktree is not None
                    and cycle.outcome.task_state is not TaskState.ACCEPTED
                    and not (cycle.outcome.block_reason or "").startswith(GIT_METADATA_CHANGED)
                ):
                    # A run that ends without a receipt for any other reason - a failed check or a
                    # rejection that buys no repair, a driver failure, an unknown outcome, a stop
                    # while the worker ran - never reached a comparison after its last worker,
                    # check or reviewer. ``hflow clean`` and the next worktree run read the
                    # metadata next, so it is compared once more and the answer is a note. It never
                    # relabels the block: an ``outcome_unknown`` stays one (AGENTS rules 5 and 8).
                    self._note_git_metadata_at_exit(run_id, repo, worktree, git_metadata_before)
                return cycle.outcome
            if round_number > 1:
                # A second repair is not a thing this build does. The cycle only asks for a repair
                # when it is allowed one, so this is a guard against the loop, not a policy.
                return self._blocked(
                    run_id,
                    RefusalCode.BUDGET_EXHAUSTED,
                    "the run already used its single repair; there is no second one",
                )
            previous = cycle.identity
            trigger = cycle.repair.trigger
            repair_context = cycle.repair
            round_number += 1
            self.store.record_note(
                run_id,
                f"{NOTE_REPAIR}: round {round_number} begins after "
                f"{trigger.value if trigger else 'unknown'} (policy digest "
                f"{policy.digest() if policy is not None else '(none)'})",
            )


    def _repair_policy(self, spec: TaskSpec, project: ProjectConfig) -> RepairPolicy | None:
        """This task's explicit repair policy, or ``None`` for the ordinary single loop.

        Deliberately reads only the spec. ``budget.max_repair_cycles`` is *not* consulted: it kept
        its historical default of 1 for every task that predates E2, and treating that number as
        consent would repair on behalf of users who never asked for one.

        ``project`` is unused today and stays in the signature because the support rules that will
        use it (worktree mode, required review) live in ``admission.predictable_dispatch_problems``
        and are checked before a run row exists; this method answers the narrower question "did
        this task ask for a repair at all?".
        """
        return spec.repair_policy

    def _repairable_failure(
        self,
        *,
        run_id: str,
        policy: RepairPolicy | None,
        round_number: int,
        original_base_commit: str,
        original_base_ref: str,
        previous: CandidateIdentity | None,
        identity: CandidateIdentity,
        trigger_candidate: RepairTrigger,
        failure_facts: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        detail: str,
        configured_deadline_seconds: int,
    ) -> _CycleResult:
        """Decide whether this failure may buy the run's single repair, and record the answer.

        The decision is a **record**, not a conclusion: an operator has to be able to see that the
        run did not repair because the failing check was an environment error rather than a
        business assertion. Every refusal path below writes one row and then blocks the run; only
        the allowed path returns a repair context.

        What may trigger a repair, in full:

        * a check that is in the policy, whose exit code is one the policy declares, and whose
          structured ``exit_reason`` says the check ran to completion with a trustworthy capture;
        * a ``changes_requested`` verdict on *this* candidate with at least one usable finding.

        Everything else - an ERROR anywhere in the mix, a timeout, leftover descendants, a
        capture failure, an undeclared exit code, a legacy evidence row with no reason, an empty
        rejection, a cancellation, an unknown outcome - stops here without spending anything. So
        does a failure that would qualify when the bound root has no repair left: that is recorded
        as ``BUDGET_EXHAUSTED``, never as an allowed repair the reservation then refuses.

        ``configured_deadline_seconds`` is the implementer's configured deadline; the repair is
        told what it actually has - that, capped by what is left of the root's clock.
        """

        def refuse(decision: RepairDecision, reason: str) -> _CycleResult:
            record = RepairRecord(
                decision=decision,
                trigger=trigger_candidate,
                reason=reason,
                policy_digest=policy.digest() if policy is not None else "",
                failed_checks=[str(fact.get("check_id", "")) for fact in failure_facts],
                exit_codes={
                    str(fact.get("check_id", "")): fact.get("exit_code") for fact in failure_facts
                },
                round=round_number,
                decided_at=utc_now(),
            )
            self._record_repair_decision(run_id, record)
            # The block reason leads with what the failure *was*, then says why no repair was
            # bought. An operator reading it needs both: the check's own message ("cannot
            # execute", "acceptance is not met") is the diagnosis, and the decision is the policy
            # outcome. Reporting only the second would hide the first.
            if trigger_candidate is RepairTrigger.REVIEW_CHANGES_REQUESTED:
                code = RefusalCode.REVIEW_REJECTED
                headline = f"independent review requested changes: {detail}"
            else:
                code = RefusalCode.VERIFICATION_FAILED
                headline = f"the process exited successfully but acceptance is not met: {detail}"
            return _CycleResult(outcome=self._blocked(run_id, code, f"{headline}. {reason}"))

        if policy is None:
            return refuse(
                RepairDecision.NOT_ENABLED,
                "this task carries no repair policy, so the failure ends the run: a second "
                "implementer attempt is something a task has to ask for explicitly",
            )
        if self._stop_recorded(run_id):
            return refuse(
                RepairDecision.STOP_REQUESTED,
                "a cancellation was recorded, so no repair was bought and nothing was "
                "re-dispatched",
            )
        if round_number > 1:
            return refuse(
                RepairDecision.ALREADY_REPAIRED,
                "this run already used its single repair; a second one is not something this "
                "build dispatches",
            )
        # The deadline is checked here as well as in the dispatch transaction: a repair that
        # cannot finish inside the root's clock would buy an attempt only to abandon it.
        deadline_state = self._root_deadline_state()
        if deadline_state is not None:
            return refuse(RepairDecision.DEADLINE_REACHED, deadline_state)

        if trigger_candidate is RepairTrigger.BUSINESS_CHECK_FAILED:
            allowed, reason = self._business_failure_allows(policy, failure_facts)
            if not allowed:
                return refuse(RepairDecision.NOT_A_BUSINESS_FAILURE, reason)
            context_findings: list[dict[str, Any]] = []
        else:
            if not policy.allow_reviewer_changes:
                return refuse(
                    RepairDecision.NOT_A_BUSINESS_FAILURE,
                    "the reviewer requested changes, but this task's repair policy does not allow "
                    "a reviewer rejection to buy a second attempt",
                )
            if not findings:
                return refuse(
                    RepairDecision.NO_FINDINGS,
                    "the reviewer requested changes without a usable finding, so there is nothing "
                    "to act on: an empty rejection cannot say what to change, and guessing would "
                    "spend an attempt on a target nobody named",
                )
            context_findings = findings

        # The root's repair counter is read here, from the ledger, rather than assumed from
        # admission: the repair's own reservation would refuse it anyway, but only after an
        # "allowed" decision had been recorded for a repair that could never be bought.
        exhausted = self._root_repairs_exhausted()
        if exhausted is not None:
            return refuse(RepairDecision.BUDGET_EXHAUSTED, exhausted)

        record = RepairRecord(
            decision=RepairDecision.ALLOWED,
            trigger=trigger_candidate,
            reason=f"one repair allowed: {detail}",
            policy_digest=policy.digest(),
            failed_checks=[str(fact.get("check_id", "")) for fact in failure_facts],
            exit_codes={
                str(fact.get("check_id", "")): fact.get("exit_code") for fact in failure_facts
            },
            round=round_number,
            decided_at=utc_now(),
        )
        self._record_repair_decision(run_id, record)
        context = RepairContext(
            original_base_commit=original_base_commit,
            # The task's own base reference, threaded through so the reference a repair round is
            # told about is the one the delivery is actually taken against. A second, differently
            # shaped reference here would be a string that merely looks authoritative.
            original_base_ref=original_base_ref,
            previous=identity,
            trigger=trigger_candidate,
            failed_checks=failure_facts,
            findings=context_findings,
            remaining_turns=self._remaining_loop_turns(run_id),
            # The deadline the repair attempt will actually be given: on a root, its configured
            # value capped by what the root's clock has left (the clock started at I1's
            # reservation); for a fully offline rootless run, the configured value. Never a
            # fallback 0 for a clock that does not exist.
            deadline_seconds=self._capped_deadline(configured_deadline_seconds),
            detail=detail,
        )
        return _CycleResult(repair=context, identity=identity)

    def _business_failure_allows(
        self, policy: RepairPolicy, failure_facts: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Is *every* failure under this policy a clean, declared business assertion failure?

        ``every`` is the whole point: one ERROR among three failed checks means the run does not
        know what it is looking at, and repairing on the strength of the other two would spend an
        attempt on a diagnosis that is not established.
        """
        if not failure_facts:
            return False, (
                "the checks did not pass but no failed check was recorded for this candidate, so "
                "there is nothing to repair against"
            )
        for fact in failure_facts:
            check_id = str(fact.get("check_id", ""))
            status = str(fact.get("status", ""))
            exit_code = fact.get("exit_code")
            reason = str(fact.get("exit_reason", ""))
            if status != EvidenceStatus.FAILED.value:
                return False, (
                    f"check {check_id!r} ended as {status!r}, not as a failure: an error, a "
                    "timeout or an unsettled process is not a business assertion failing, and "
                    "this run does not repair an environment problem"
                )
            if reason not in CLEAN_EXIT_REASONS:
                return False, (
                    f"check {check_id!r} has execution reason {reason or '(none recorded)'}, which "
                    "does not establish that the check ran to completion with a trustworthy "
                    "capture; without that, no repair is bought"
                )
            if not policy.business_failure_for(check_id, exit_code if isinstance(exit_code, int) else None):
                declared = policy.check_exit_codes.get(check_id)
                return False, (
                    f"check {check_id!r} exited {exit_code!r}, which this task's repair policy "
                    f"does not declare as a business failure (declared: {declared or 'nothing'}). "
                    "An undeclared exit code is not a diagnosis"
                )
        summary = ", ".join(
            f"{fact.get('check_id')} exit={fact.get('exit_code')}" for fact in failure_facts
        )
        return True, f"every failing check is a declared business failure under the policy ({summary})"

    def _root_repairs_exhausted(self) -> str | None:
        """A human-readable reason when the bound root has no repair left, else ``None``.

        ``None`` too for a rootless run: only a fully offline run gets here without a root, and
        it has no root counter to spend (admission refuses a real transport's rootless repair).
        """
        if self.root_binding is None:
            return None
        view = self.store.root_budget_view(self.root_binding.root_id)
        if view is None or view.used_repairs < view.limits.max_repairs:
            return None
        return (
            f"root {self.root_binding.root_id} has used {view.used_repairs}/"
            f"{view.limits.max_repairs} repair attempt(s), so no repair is bought and nothing is "
            "re-dispatched; changing the revision does not reset the count"
        )

    def _root_deadline_state(self) -> str | None:
        """A human-readable reason when the root's clock has run out, else ``None``.

        Read from the ledger rather than from a local timer: the deadline belongs to the root and
        survives a restart, so a resumed controller reaches the same answer.
        """
        if self.root_binding is None:
            return None
        view = self.store.root_budget_view(self.root_binding.root_id)
        if view is None or not view.deadline_at:
            return None
        if parse_ts(view.deadline_at) <= parse_ts(utc_now()):
            return (
                f"the root's deadline ({view.deadline_at}) has passed, so no repair is bought and "
                "no further work is dispatched for it"
            )
        return None

    def _capped_deadline(self, configured_seconds: int) -> int:
        """A role's deadline: its configured value, or what is left of the root, whichever is less.

        A role asked to work for longer than the root has left is being asked to work past a
        deadline the run already recorded. Capping here is what makes the root's clock a bound on
        the work rather than a note beside it - and it is why the reviewer's default of 900 seconds
        is a ceiling, not a promise.

        Before the root's clock has started, the root's own ``deadline_seconds`` is the cap: the
        clock starts at the first reservation with exactly that much time, so no role on this
        root can be given more.
        """
        if self.root_binding is None:
            return int(configured_seconds)
        remaining = self._remaining_deadline_seconds()
        if remaining is None:
            # No clock has started yet; it will start, at this root's first reservation, with the
            # root's full deadline. A root not registered yet is read from this controller's limits.
            view = self.store.root_budget_view(self.root_binding.root_id)
            limits = view.limits if view is not None else self.root_limits
            if limits is None:
                return int(configured_seconds)
            return min(int(configured_seconds), int(limits.deadline_seconds))
        return max(0, min(int(configured_seconds), remaining))

    def _remaining_deadline_seconds(self) -> int | None:
        """Seconds left on the root's clock, or ``None`` when no clock has started.

        ``None`` and ``0`` are different facts and must not be collapsed: the deadline is recorded
        when the root's first dispatch is reserved, so before that there is no clock to measure
        against - the run has whatever its limits allow. Reporting that as ``0`` would tell every
        check "no time left" and refuse work that has not started, which is the opposite of what a
        not-yet-measured deadline means.
        """
        if self.root_binding is None:
            return None
        view = self.store.root_budget_view(self.root_binding.root_id)
        if view is None or not view.deadline_at:
            return None
        remaining = (parse_ts(view.deadline_at) - parse_ts(utc_now())).total_seconds()
        return max(0, int(remaining))

    def _remaining_loop_turns(self, run_id: str) -> int:
        """How many top-level dispatches this run may still make, from its own ceiling."""
        row = self.store.get_run(run_id)
        return max(0, int(row["turn_limit"]) - int(row["turns_reserved"]))

    def _record_repair_decision(self, run_id: str, record: RepairRecord) -> None:
        """Persist one repair decision and echo it into the run's notes.

        Both, on purpose: the structured row is what `status`/`report` renders and what a later
        reader can act on, and the note is what an operator grepping a run finds in place. A
        failure to store the structured row is reported rather than swallowed, because a decision
        nobody recorded is the state this method exists to prevent.
        """
        try:
            self.store.record_repair_record(run_id, record)
        except StoreError as exc:  # pragma: no cover - a store failure must not hide the decision
            self.store.record_note(
                run_id,
                f"{NOTE_REPAIR}: the decision could not be stored structurally ({exc}); it is "
                "recorded only in this note",
            )
        self.store.record_note(
            run_id,
            f"{NOTE_REPAIR}: decision={record.decision.value} "
            f"trigger={record.trigger.value if record.trigger else '(none)'} "
            f"round={record.round} checks={','.join(record.failed_checks) or '-'} "
            f"reason={record.reason[:400]}",
        )

    def _review_findings(
        self, run_id: str, attempt_id: str, candidate_fp: str
    ) -> list[dict[str, Any]]:
        """The reviewer's findings for *this* candidate, if it left any usable ones.

        Read from the recorded review evidence rather than from the verdict object, so the facts a
        repair acts on are the facts on disk. Two filters, both load-bearing:

        * the **candidate and attempt** must match, which is what stops a previous round's
          rejection from being replayed as a reason to repair the current one;
        * the verdict recorded in that row must be ``changes_requested``, which is read from the
          row's own payload rather than inferred from its status - a review evidence row is
          recorded as failed for a rejection and for a wire failure alike, and only the former is
          a finding a repair may act on.

        Only *usable* findings are returned (see :meth:`_usable_finding`): a ``[{}]`` or a
        whitespace statement is a non-empty list that still names nothing to change. An empty or
        unusable payload returns nothing, so the caller refuses with ``NO_FINDINGS`` rather than
        guessing what the reviewer meant.
        """
        for row in self.store.evidence_for(run_id, kind="review"):
            if row["attempt_id"] != attempt_id or row["candidate_fingerprint"] != candidate_fp:
                continue
            try:
                payload = json.loads(row["detail"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("verdict") != "changes_requested":
                # A wire failure is recorded as failed review evidence too, and it is not a
                # rejection with findings: only a real verdict may buy a repair.
                continue
            findings = payload.get("findings")
            if isinstance(findings, list) and findings:
                return [
                    item
                    for item in findings
                    if isinstance(item, dict) and self._usable_finding(item)
                ]
        return []

    #: Keys that label or place a finding but do not say what is wrong: a finding made only of
    #: these (or of blank text) gives a repair nothing to act on.
    _FINDING_BOOKKEEPING_KEYS = frozenset({"id", "severity", "status", "location", "target"})

    @classmethod
    def _usable_finding(cls, finding: dict[str, Any]) -> bool:
        """Does this finding carry at least one non-blank text value outside the bookkeeping keys?

        The reviewer contract names no finding keys, so usability is decided by content rather
        than by a key name: any string under any other key counts, including one nested in a
        list or an object (``"evidence": ["..."]``). Numbers and booleans alone say nothing a
        repair could act on.
        """

        def has_text(value: Any) -> bool:
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, dict):
                return any(has_text(item) for item in value.values())
            if isinstance(value, list):
                return any(has_text(item) for item in value)
            return False

        return any(
            has_text(value)
            for key, value in finding.items()
            if key not in cls._FINDING_BOOKKEEPING_KEYS
        )

    def _reconcile_repair_workspace(
        self,
        *,
        repo: GitRepo,
        worktree: Path,
        previous: CandidateIdentity | None,
        scope: Scope,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Confirm the worktree is the one the repair is supposed to start from.

        The repair runs in the *same* isolated worktree as the first attempt, which is only safe
        if that worktree is still exactly at the previous candidate. A drifted or dirty tree means
        the bytes a repair would edit are not the bytes that were checked, so it is refused
        instead: nothing is reset, nothing is overwritten, and the user's checkout is never
        touched. A file git does not ignore that appeared after the freeze (a check's report, for
        example) makes the tree dirty: the repair's freeze would stage it or refuse it.

        An index entry flagged assume-unchanged or skip-worktree hides changes from the status
        read, so it is refused too.

        Returns ``(refusal, carried_ignored)``: a refusal reason, or ``None`` when the tree is the
        expected one, and the ignored paths present in the worktree now. Those can only have
        appeared after the previous freeze (which refused any ignored path off the allowlist), so
        they are the previous round's check and review byproducts - HFlow's own output, not the
        worker's. The repair's freeze accepts exactly these paths, literally, and never stages
        one. A carried path is only safe outside what the scoped fingerprint hashes: there the
        repair round's manifest comparison refuses any worker change to it as outside the scope.
        An ignored file the fingerprint *does* cover (one under a ``write_allow`` directory) would
        make the fingerprint describe bytes no commit holds - it changes the fingerprint while
        the commit stays the same, so the no-content-change guard and the evidence would no
        longer speak about the frozen commit - and an in-scope worker edit to it would pass the
        manifest comparison. Such a path refuses the repair here, before anything is bought.
        Nothing is deleted to make the tree clean.
        """
        if previous is None:
            return None, ()
        if not previous.git_commit:
            return (
                "the previous candidate has no recorded commit, so a repair cannot establish the "
                "tree it would be starting from"
            ), ()
        try:
            head = repo.worktree_commit(worktree)
            tree = repo.worktree_tree(worktree)
        except GitError as exc:
            return f"the worktree's identity could not be read: {exc}", ()
        if head != previous.git_commit:
            return (
                f"the worktree is at {head}, not at the previous candidate "
                f"{previous.git_commit}: a repair would start from bytes that were never checked"
            ), ()
        if previous.git_tree and tree != previous.git_tree:
            return (
                f"the worktree's tree is {tree}, not the checked candidate tree "
                f"{previous.git_tree}"
            ), ()
        try:
            flagged = repo.index_flagged_paths(worktree)
            report = repo.status_report(worktree)
        except GitError as exc:
            return f"the worktree's status could not be read: {exc}", ()
        if flagged:
            return (
                "index entries are flagged assume-unchanged or skip-worktree ("
                + ", ".join(flagged[:5])
                + (f" (+{len(flagged) - 5} more)" if len(flagged) > 5 else "")
                + "), so git's status cannot show whether the worktree is still the checked "
                "candidate; a repair is refused rather than clearing them, and nothing was bought"
            ), ()
        blocking = report.blocking_changes
        if blocking:
            return (
                "the worktree changed after the candidate was frozen ("
                + ", ".join(blocking[:5])
                + (f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else "")
                + "), for example a file an approved check wrote that git does not ignore; a "
                "repair is refused rather than resetting or overwriting it, and nothing was bought"
            ), ()
        try:
            root = Path(os.path.realpath(worktree))
            fingerprinted = {
                path.relative_to(root).as_posix() for path in expand_scope(root, scope)
            }
        except RefusedError as exc:
            return f"the worktree's scoped files could not be listed: {exc.message}", ()
        covered = [path for path in report.ignored if path in fingerprinted]
        if covered:
            return (
                "a previous check left ignored file(s) inside write_allow ("
                + ", ".join(covered[:5])
                + (f" (+{len(covered) - 5} more)" if len(covered) > 5 else "")
                + "); the scoped fingerprint would cover bytes no candidate commit holds, so a "
                "repair is refused rather than deleting them, and nothing was bought"
            ), ()
        return None, tuple(report.ignored)

    def _git_metadata_refusal(
        self,
        run_id: str,
        repo: GitRepo,
        worktree: Path,
        before: GitMetadataSnapshot | None,
        *,
        where: str,
        consequence: str,
    ) -> tuple[RefusalCode, str] | None:
        """Why HFlow's own git must not run here, or ``None`` when the shared metadata is unchanged.

        Asked right before HFlow's git reads that metadata again after something it does not
        control ran - the implementer, an approved check, the reviewer: before the freeze stages
        anything, before a repair round reads the worktree's status, and at acceptance, before the
        status read of the user's checkout (a status read runs a clean filter on a file whose stat
        data changed). Any difference refuses, whoever made it - a worker, a check, or the user's
        own concurrent ``git config``, which is a false positive this accepts to fail closed.
        Nothing is restored: that would write the user's repository. The refusal names what
        changed by key and file, never by value.
        """
        if before is None:
            return RefusalCode.INTERNAL_ERROR, (
                f"no pre-dispatch snapshot of the shared Git metadata exists to compare with "
                f"{where}, so {consequence}"
            )
        tail = (
            "This is configuration or attribute data outside the worktree that HFlow's own git "
            "reads - it can name a program git would run - so HFlow ran no git status, add or "
            "commit on it and restored nothing: inspect and restore it (git config --list "
            "--show-origin --show-scope) before hflow clean or the next worktree run, which read "
            "it again"
        )
        try:
            after = repo.metadata_snapshot(worktree)
        except GitError as exc:
            # It was readable before dispatch, so a read that fails now is treated as a change.
            self.store.record_note(
                run_id,
                f"{NOTE_GIT_METADATA}: unreadable {where}: {before.digest} -> ({exc}); restore it "
                "before hflow clean or the next worktree run",
            )
            return RefusalCode.SCOPE_VIOLATION, (
                f"{GIT_METADATA_CHANGED} or became unreadable since the pre-dispatch snapshot "
                f"({exc}); found {where}, so {consequence}. {tail}"
            )
        changes = after.changes_since(before)
        if not changes:
            return None
        # The note carries the change list too: when a stop already decided the run, ``_blocked``
        # keeps the stop's reason and this note is the only place the change is named.
        self.store.record_note(
            run_id,
            f"{NOTE_GIT_METADATA}: changed {where}: {before.digest} -> {after.digest} "
            f"({_change_list(changes)}); restore it before hflow clean or the next worktree run",
        )
        return RefusalCode.SCOPE_VIOLATION, (
            f"{GIT_METADATA_CHANGED} since the pre-dispatch snapshot ({_change_list(changes)}); "
            f"found {where}, so {consequence}. {tail}"
        )

    def _note_git_metadata_at_exit(
        self, run_id: str, repo: GitRepo, worktree: Path, before: GitMetadataSnapshot | None
    ) -> None:
        """Compare the shared Git metadata once more as a run ends without a receipt: record, never decide.

        Never calls ``_blocked`` or any store transition, so the run's block stays what it was.
        """
        if before is None:
            return
        try:
            after = repo.metadata_snapshot(worktree)
        except GitError as exc:
            self.store.record_note(
                run_id,
                f"{NOTE_GIT_METADATA}: unreadable when the run ended: {before.digest} -> ({exc}); "
                "inspect and restore it before hflow clean or the next worktree run",
            )
            return
        changes = after.changes_since(before)
        if not changes:
            return
        self.store.record_note(
            run_id,
            f"{NOTE_GIT_METADATA}: changed when the run ended: {before.digest} -> {after.digest} "
            f"({_change_list(changes)}); the run's block is unchanged - inspect and restore it "
            "(git config --list --show-origin --show-scope) before hflow clean or the next "
            "worktree run, which read it again",
        )

    # -- steps ---------------------------------------------------------------




    def _attempt_cycle(
        self,
        run_id: str,
        request: RunRequest,
        *,
        repo: GitRepo | None,
        worktree: Path | None,
        execution_root: Path,
        user_tree_before: str,
        dirty_target: bool,
        implementer_writes: bool,
        implementer_packet: PreparedPacket | None,
        previous: CandidateIdentity | None,
        trigger: RepairTrigger | None,
        round_number: int,
        original_base_commit: str,
        original_base_ref: str,
        policy: RepairPolicy | None,
        repair_context: RepairContext | None = None,
        git_metadata_before: GitMetadataSnapshot | None = None,
    ) -> _CycleResult:
        """One implementer attempt: dispatch, launch, freeze, check, review, decide.

        The parameter list is long on purpose: everything this cycle must *not* re-derive from
        ambient state (the worktree, the original base, which round it is) is passed in, so a
        repair round cannot quietly behave like a first round.
        """

        spec = request.task
        project = request.project
        project_root = Path(request.project_root)
        reservation_id = new_reservation_id()
        attempt_id = new_attempt_id()
        invocation_id = new_invocation_id()

        # The workspace, the run's state and the permissions were established once in
        # ``_drive`` before this cycle was entered. Repeating any of it here would create
        # a second worktree for a repair and re-record facts that are already the run's.

        # --- a repair round reopens the run for its single new attempt --------------------
        # Before anything is bought, the worktree must still be exactly the candidate that was
        # checked. A drifted or dirty tree means the bytes a repair would edit are not the bytes
        # that were verified, so it is refused rather than reset or overwritten - and this is
        # checked *before* the dispatch, so a refusal costs nothing.
        # Ignored paths the previous round's checks left in the worktree, accepted (literally) by
        # this round's freeze; empty for a first round, which starts from a fresh worktree.
        if repo is not None and worktree is not None and git_metadata_before is None:
            # Without the pre-dispatch snapshot no later comparison could show that HFlow's own
            # git reads the metadata it read before the worker ran; refused before anything is
            # bought.
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    "a worktree attempt was reached without the pre-dispatch snapshot of the "
                    "shared Git metadata, so it was refused before any dispatch",
                )
            )
        carried_ignored: tuple[str, ...] = ()
        if previous is not None:
            if repo is not None and worktree is not None:
                # Before the reconcile's status read: an approved check or the reviewer ran since
                # the last comparison, and a filter it configured would run inside that read.
                metadata = self._git_metadata_refusal(
                    run_id,
                    repo,
                    worktree,
                    git_metadata_before,
                    where="before the repair round",
                    consequence="no repair was bought and nothing was dispatched",
                )
                if metadata is not None:
                    self._record_repair_decision(
                        run_id,
                        RepairRecord(
                            decision=RepairDecision.WORKSPACE_DRIFT,
                            trigger=trigger,
                            reason=metadata[1],
                            policy_digest=policy.digest() if policy is not None else "",
                            round=round_number,
                            decided_at=utc_now(),
                        ),
                    )
                    return _CycleResult(outcome=self._blocked(run_id, *metadata))
                refusal, carried_ignored = self._reconcile_repair_workspace(
                    repo=repo, worktree=worktree, previous=previous, scope=spec.scope
                )
                if refusal is not None:
                    self._record_repair_decision(
                        run_id,
                        RepairRecord(
                            decision=RepairDecision.WORKSPACE_DRIFT,
                            trigger=trigger,
                            reason=refusal,
                            policy_digest=policy.digest() if policy is not None else "",
                            round=round_number,
                            decided_at=utc_now(),
                        ),
                    )
                    return _CycleResult(
                        outcome=self._blocked(run_id, RefusalCode.SCOPE_VIOLATION, refusal)
                    )
            # The run's phase still says "verification", which is correct for the round that just
            # ended: an implementer must not be reserved behind a finished candidate. Reopening is
            # its own recorded transition, in one transaction, and it refuses to revive a run that
            # a stop or a terminal decision already ended - a repair is a decision taken *inside* a
            # live run, never a way back into a finished one.
            try:
                self.store.reopen_for_repair(run_id, self.controller_id)
            except StoreError as exc:
                self._record_repair_decision(
                    run_id,
                    RepairRecord(
                        decision=RepairDecision.STOP_REQUESTED,
                        trigger=trigger,
                        reason=str(exc),
                        policy_digest=policy.digest() if policy is not None else "",
                        round=round_number,
                        decided_at=utc_now(),
                    ),
                )
                return _CycleResult(outcome=self._blocked(run_id, RefusalCode.BUDGET_EXHAUSTED, str(exc)))

        # The structured input of a repair attempt: the context the decision that allowed it
        # recorded. ``None`` for a first attempt, which is why the first attempt's packet is
        # byte-identical to what it was before E2 - and why an offline agent can tell the two
        # rounds apart from the packet it actually received.
        repair = repair_context
        if previous is not None and repair is None:
            # A repair round without the context it was allowed on is not a repair: it is an agent
            # being asked to change a candidate it cannot see, on evidence nobody handed it. The
            # refusal is before the dispatch transaction, so it costs nothing.
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    "a repair round was reached without the recorded repair context, so the "
                    "attempt was refused before any dispatch instead of being sent an input that "
                    "does not say what to repair",
                )
            )

        # --- the packet is rendered and bounded *before* anything is bought ----------------
        # The real input is rendered here, not after the reservation, because the packet's size is
        # a fact about this attempt: a repair packet carries the failure facts and the previous
        # candidate, so it can be the first packet that does not fit. Discovering that after the
        # dispatch transaction would charge the root's submission for an attempt that never
        # started - and the resulting refusal would have to claim nothing had been consumed while
        # the ledger said otherwise. Rendering first makes an oversized packet an ordinary
        # pre-dispatch refusal: no allowance moves, and the run blocks instead of raising.
        #
        # A packet prepared before admission (``run_task``) is reused only when the workspace it
        # names is the workspace this run actually got *and* this is a first attempt. A repair
        # must never reuse it: the packet has to carry the repair context, and the checked packet
        # was rendered before any failure existed.
        #
        # The packet states the deadline this invocation will be given, and is reused or
        # re-rendered on that basis too: a packet rendered earlier (in ``run_task``, or a repair
        # context decided before the checks finished) may name a longer one than is enforced now.
        stated_deadline = self._capped_deadline(request.deadline_seconds)
        if repair is not None and repair.deadline_seconds != stated_deadline:
            repair = repair.model_copy(update={"deadline_seconds": stated_deadline})
        if (
            implementer_packet is not None
            and implementer_packet.workspace == str(execution_root)
            and implementer_packet.deadline_seconds == stated_deadline
            and repair is None
        ):
            prepared = implementer_packet
        else:
            try:
                prepared = self._render_implementer_packet(
                    run_id=run_id,
                    spec=spec,
                    workspace=str(execution_root),
                    deadline_seconds=stated_deadline,
                    writes_allowed=implementer_writes,
                    repair=repair,
                )
            except RefusedError as exc:
                return _CycleResult(outcome=self._refuse(run_id, exc.code, str(exc)))

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
                purpose="repair invocation" if previous is not None else "implementer invocation",
                spec=spec,
                project=project,
                is_repair=previous is not None,
            )
        except RefusedError as exc:
            return _CycleResult(outcome=self._refuse(run_id, exc.code, str(exc)))
        attempt_id = dispatch.attempt_id
        invocation_id = (
            dispatch.invocation.invocation_id if dispatch.invocation else invocation_id
        )
        if dispatch.invocation is None:
            # The legacy path: no ledger row, so the dispatch facts stay on the attempt row. The
            # registration is conditional on the stop, so a stop that committed after the
            # reservation wins the handoff: nothing is registered and no driver is asked.
            if not self.store.register_attempt_invocation_unless_stopped(
                run_id, attempt_id, column="invocation_id", invocation_id=invocation_id
            ):
                reason = (
                    "a cancellation intent was recorded between the reservation and the "
                    "implementer's registration; no invocation was started and the reserved "
                    "turn and authorization submission stay consumed"
                )
                try:
                    self.store.finish_attempt(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        state=AttemptState.CANCELLED,
                        outcome=InvocationOutcome.CANCELLED,
                        result={"error": reason},
                        block_code=RefusalCode.CANCELLED_BY_OPERATOR,
                    )
                except StoreError:
                    pass  # the stop's own bookkeeping finished it first
                return _CycleResult(
                    outcome=self._blocked(run_id, RefusalCode.CANCELLED_BY_OPERATOR, reason)
                )

        # The reservation may have started the root's clock, or the clock moved on since the
        # packet was rendered; the invocation gets what is left now, and the packet is brought
        # into agreement with it. The value can only shrink, so the re-rendered packet is never
        # larger than the one already checked against the bound; a refusal anyway is recorded
        # against this reservation, which provably never reached a launch.
        deadline_seconds = min(
            prepared.deadline_seconds, self._capped_deadline(request.deadline_seconds)
        )
        if deadline_seconds != prepared.deadline_seconds:
            try:
                prepared = self._render_implementer_packet(
                    run_id=run_id,
                    spec=spec,
                    workspace=str(execution_root),
                    deadline_seconds=deadline_seconds,
                    writes_allowed=implementer_writes,
                    repair=(
                        repair.model_copy(update={"deadline_seconds": deadline_seconds})
                        if repair is not None
                        else None
                    ),
                )
            except RefusedError as exc:
                self._mark_invocation_not_started(dispatch, f"refused before launch: {exc}")
                return _CycleResult(
                    outcome=self._block_attempt(run_id, attempt_id, exc.code, str(exc))
                )

        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )

        # Recorded before the worker runs: what the workspace looked like, so a change outside
        # the declared scope is detected afterwards from two manifests rather than assumed.
        pre_manifest = manifest(execution_root)
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
            deadline_seconds=prepared.deadline_seconds,
            spec_digest=spec.spec_digest(),
            packet=prepared.packet.text,
            writes_allowed=implementer_writes,
            data_dir=str(self.data_dir),
            # Asked by the driver at the instant it creates the process, not answered here: a
            # snapshot taken now would be stale by the time the child is spawned.
            stop_requested=lambda: self._stop_recorded(run_id),
            # The other half of that handoff: the driver reports what it observed at its spawn
            # decision, so "a process exists" is a recorded observation. A driver that never calls
            # it is read by `_confirm_driver_ran` instead: completed work counts as a launch (spawn
            # kind unknown, no process), cancelled with no work as `not_started`, and anything
            # else stays `requested` - an unconfirmed launch, not a start.
            on_spawn=self._spawn_reporter(dispatch),
        )

        # From here on, failures must not re-dispatch: the model may already have run. The
        # ledger records how far this dispatch got before it is handed over - a launch *requested*
        # and nothing more - so a crash here is visible as an unconfirmed launch rather than
        # rounded up to a model call.
        self._mark_launch_requested(dispatch)
        # Read between the launch request and the handoff: only a stop recorded by now proves the
        # driver's gate refused, which is what lets a silent driver's empty return mean "never
        # launched" (see ``_confirm_driver_ran``).
        stop_before_launch = self._stop_recorded(run_id)
        try:
            result = self.driver.start(invocation)
        except RefusedError as exc:
            self._mark_invocation_not_started(dispatch, f"refused before launch: {exc}")
            return _CycleResult(
                outcome=self._block_attempt(run_id, attempt_id, exc.code, str(exc))
            )
        except Exception as exc:  # noqa: BLE001 - controller must not hot-fix a driver
            self._mark_driver_failure(dispatch, exc)
            return _CycleResult(
                outcome=self._block_attempt(
                    run_id, attempt_id, RefusalCode.INTERNAL_ERROR, repr(exc)
                )
            )
        except BaseException:
            # Ctrl+C or SystemExit: this process ends without the result. Record that before
            # re-raising, so the run is reconcilable instead of RUNNING forever.
            self._record_interruption(run_id, attempt_id, "implementer")
            raise
        else:
            self._confirm_driver_ran(dispatch, result, stop_before_launch=stop_before_launch)

        if result.outcome is InvocationOutcome.OUTCOME_UNKNOWN:
            self._apply_result_or_stay_stopped(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.OUTCOME_UNKNOWN,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                result=result,
                dispatch=dispatch,
                settle_detail="driver reported an unknown outcome",
                block_code=RefusalCode.OUTCOME_UNKNOWN,
                reason=(
                    f"the worker's result is unknown ({result.error_code or 'no code'}: "
                    f"{result.error_message or 'no detail'}); no re-dispatch until an operator "
                    "reconciles (plan 9.3)"
                ),
            )
            self._release_invocation(self.driver, run_id, invocation_id)
            return _CycleResult(outcome=self._outcome_for(run_id))

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
            self._apply_result_or_stay_stopped(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=result.outcome,
                result=result,
                dispatch=dispatch,
                settle_detail="prompt digest mismatch",
                block_code=RefusalCode.INTERNAL_ERROR,
                reason="the invocation's prompt digest does not match the rendered input packet, "
                "so the result cannot be attributed to this task",
            )
            self._release_invocation(self.driver, run_id, invocation_id)
            return _CycleResult(outcome=self._outcome_for(run_id))

        if result.outcome is not InvocationOutcome.COMPLETED:
            # Settled only when applied, like every implementer result: after a stop, an entry
            # the stop already closed - ``launch_unknown`` after a forced stop of an unrecorded
            # launch, ``not_started`` after a stop that won the spawn gate - keeps its state, an
            # entry an unconfirmed stop left open stays open, and this result is a note.
            self._apply_result_or_stay_stopped(
                run_id=run_id,
                attempt_id=attempt_id,
                state=AttemptState.FAILED,
                outcome=result.outcome,
                result=result,
                dispatch=dispatch,
                settle_detail="driver reported a non-completed outcome",
                block_code=RefusalCode.DRIVER_FAILED,
                reason=f"driver reported {result.outcome.value}: "
                f"{result.error_message or 'no detail'}",
            )
            self._release_invocation(self.driver, run_id, invocation_id)
            # Applied or already finalized by a stop: the run's own recorded state is the answer.
            return _CycleResult(outcome=self._outcome_for(run_id))

        applied = self._apply_result_or_stay_stopped(
            run_id=run_id,
            attempt_id=attempt_id,
            state=AttemptState.SUCCEEDED,
            outcome=result.outcome,
            result=result,
            dispatch=dispatch,
            settle_detail="implementer invocation completed",
        )
        self._release_invocation(self.driver, run_id, invocation_id)
        if not applied:
            # The run was stopped while this invocation was running. Its result is a late-result
            # note, the attempt and the ledger entry keep the state the stop left, and the run
            # keeps the decision the operator made; nothing here freezes or resumes the loop.
            return _CycleResult(outcome=self._outcome_for(run_id))

        # The freeze's ``git status`` / ``git add`` read the shared Git metadata. A filter the
        # worker configured there would run inside them, in this process, after the worker's own
        # process boundary is gone - so any change since dispatch refuses the run here, first:
        # ahead of the fingerprint and manifest scope checks, so the change that outlives the run
        # is the one reported, and ahead of the stop check, so a stop recorded after the
        # implementer's result was applied still gets its note (``_blocked`` keeps the stop's
        # state). A stop recorded while the implementer ran returned just above, before this
        # point; the exit note in ``_drive`` covers that case.
        if repo is not None and worktree is not None:
            metadata = self._git_metadata_refusal(
                run_id,
                repo,
                worktree,
                git_metadata_before,
                where="before the candidate freeze",
                consequence="nothing was staged, committed or checked",
            )
            if metadata is not None:
                return _CycleResult(outcome=self._blocked(run_id, *metadata))

        # --- freeze the candidate the controller actually observed -------------
        try:
            post_fingerprint = candidate_fingerprint(execution_root, spec.scope)
        except RefusedError as exc:
            # A write_allow entry that resolved inside the checkout at admission now leaves the
            # worktree (the worker replaced it with a junction or a symlink, for example). Nothing
            # is frozen, and the run ends blocked - releasing its root - instead of raising out of
            # ``run_task`` with the run left RUNNING.
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    exc.code,
                    "the worker's candidate cannot be fingerprinted inside the worktree, so "
                    f"nothing was frozen, checked or reviewed: {exc.message}",
                )
            )
        # Outside ``write_allow`` or under any deny rule (the task's, the project's, the built-in
        # list): a denied file inside an allowed directory is refused here, before the freeze.
        outside = paths_outside_scope(
            changed_paths(pre_manifest, manifest(execution_root)), spec.scope, project.write_deny
        )
        if outside:
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.SCOPE_VIOLATION,
                    "the worker changed files its TaskSpec did not authorize (outside write_allow "
                    "or under a write_deny rule): "
                    + ", ".join(outside[:5])
                    + (f" (+{len(outside) - 5} more)" if len(outside) > 5 else ""),
                )
            )

        # A stop that landed after the result was applied still decides the run: nothing is
        # frozen for a stopped run - no candidate commit, no retained ref - and the run keeps
        # the state the stop recorded. Git work cannot share the stop's transaction, so this is
        # asked at each Git step instead.
        if self._stop_recorded(run_id):
            return _CycleResult(outcome=self._stopped_before_freeze(run_id, attempt_id))

        # Freeze an explicit Git identity for the candidate before any check runs, so the
        # receipt names a commit rather than only a content fingerprint.
        freeze: CandidateFreeze | None = None
        if repo is not None and worktree is not None:
            try:
                freeze = repo.freeze_candidate(
                    worktree,
                    list(spec.scope.write_allow),
                    f"hflow: candidate for {spec.task_id}",
                    deny=[*spec.scope.write_deny, *project.write_deny],
                    # A repair round also accepts the ignored byproducts the previous round's
                    # checks left behind, each as a literal path (see
                    # ``_reconcile_repair_workspace``); anything new and ignored still refuses.
                    allow_ignored=[
                        *IGNORED_ARTIFACT_ALLOWLIST,
                        *(glob.escape(path) for path in carried_ignored),
                    ],
                    # The commit this round started from. A worker that committed, amended or
                    # reset inside the worktree moved HEAD away from it and is refused there.
                    expected_head=(
                        previous.git_commit if previous is not None else original_base_commit
                    ),
                )
                if self._stop_recorded(run_id):
                    return _CycleResult(
                        outcome=self._stopped_before_freeze(
                            run_id, attempt_id, frozen_commit=freeze.candidate_commit
                        )
                    )
                # Second guard, on Git's own answer rather than on the status the freeze read:
                # the whole change from the task's original base to this candidate is held to
                # the scope and every deny rule before a ref keeps it or a check runs on it.
                cumulative_paths = repo.diff_paths(
                    original_base_commit, freeze.candidate_commit, cwd=project_root
                )
                cumulative_outside = paths_outside_scope(
                    cumulative_paths, spec.scope, project.write_deny
                )
                if cumulative_outside:
                    return _CycleResult(
                        outcome=self._blocked(
                            run_id,
                            RefusalCode.SCOPE_VIOLATION,
                            "the candidate commit changes paths its TaskSpec did not authorize "
                            "(outside write_allow or under a write_deny rule): "
                            + ", ".join(cumulative_outside[:5])
                            + (
                                f" (+{len(cumulative_outside) - 5} more)"
                                if len(cumulative_outside) > 5
                                else ""
                            ),
                        )
                    )
                # Keep the candidate reachable independently of its worktree: a bare commit
                # SHA is an identifier, not a retention policy.
                ref = repo.candidate_ref(run_id, attempt_id)
                ref_status = repo.ensure_candidate_ref(ref, freeze.candidate_commit)
                self.store.record_note(run_id, f"candidate ref {ref} ({ref_status})")
                # The next role (the reviewer, or a repair round) starts a DSH agent in this
                # worktree, and upstream DSH source says it loads these files as instructions,
                # skills or environment next to HFlow's packet. They are recorded from Git's own
                # list, never refused here (refusing needs a ruling), and an empty list is
                # recorded too.
                self.store.record_dsh_context(
                    run_id,
                    DshContextRecord(
                        attempt_id=attempt_id,
                        round=round_number,
                        base_commit=original_base_commit,
                        candidate_commit=freeze.candidate_commit,
                        paths=dsh_context_paths(cumulative_paths),
                        list_source=DSH_CONTEXT_LIST_SOURCE,
                        recorded_at=utc_now(),
                    ),
                )
            except GitError as exc:
                return _CycleResult(
                    outcome=self._blocked(
                        run_id, RefusalCode.INTERNAL_ERROR, f"candidate freeze failed: {exc}"
                    )
                )
            except GitStatusParseError as exc:
                return _CycleResult(
                    outcome=self._blocked(
                        run_id, RefusalCode.SCOPE_VIOLATION, f"candidate freeze refused: {exc}"
                    )
                )

        # The three identities a repair has to keep apart: the task's original base, the
        # candidate this round started from, and the candidate this round produced. The scoped
        # *fingerprint* decides whether the checked content changed - a fresh commit SHA is not
        # progress, and neither is a tree change the fingerprint cannot see.
        identity = CandidateIdentity(
            round=round_number,
            attempt_id=attempt_id,
            base_commit=original_base_commit,
            parent_commit=previous.git_commit if previous is not None else "",
            git_commit=freeze.candidate_commit if freeze is not None else "",
            git_tree=freeze.tree if freeze is not None else "",
            fingerprint=post_fingerprint,
            paths=list(freeze.paths) if freeze is not None else [],
            is_repair=previous is not None,
            unchanged_from_parent=bool(
                previous is not None and previous.fingerprint == post_fingerprint
            ),
        )
        if identity.unchanged_from_parent:
            # No content change: the scoped fingerprint is the one of the round we are repairing.
            # Buying a reviewer for that would be paying to be told nothing happened, so the run
            # stops here and records why. A tree that moved anyway changed something the scope's
            # fingerprint does not cover (denied paths are refused before the freeze), which is a
            # violation rather than progress.
            parent_tree = previous.git_tree if previous is not None else ""
            if parent_tree != identity.git_tree:
                moved = ", ".join(identity.paths[:5]) or "(no staged paths)"
                reason = (
                    f"the repair attempt changed the candidate tree ({parent_tree or '(none)'} -> "
                    f"{identity.git_tree}; {moved}) but not the scoped fingerprint "
                    f"{identity.fingerprint}: no checked content changed, and a tree change the "
                    "fingerprint cannot see is refused as a scope violation, so no reviewer was "
                    "bought"
                )
                block_code = RefusalCode.SCOPE_VIOLATION
                block_reason = (
                    f"the repair attempt changed the candidate tree ({moved}) but not its scoped "
                    "fingerprint; that is not progress, and nothing was reviewed or accepted"
                )
            else:
                reason = (
                    "the repair attempt produced no content change: candidate tree "
                    f"{identity.git_tree or '(none)'} and fingerprint {identity.fingerprint} are "
                    "unchanged from the round it was repairing, so no reviewer was bought"
                )
                block_code = RefusalCode.VERIFICATION_FAILED
                block_reason = (
                    "the repair attempt left the candidate unchanged; nothing was reviewed and "
                    "nothing was accepted"
                )
            decision = RepairRecord(
                decision=RepairDecision.NO_CONTENT_CHANGE,
                trigger=trigger,
                reason=reason,
                policy_digest=policy.digest() if policy is not None else "",
                round=round_number,
                decided_at=utc_now(),
            )
            self._record_repair_decision(run_id, decision)
            return _CycleResult(outcome=self._blocked(run_id, block_code, block_reason))

        try:
            self.store.advance_to_checking(
                run_id=run_id, attempt_id=attempt_id, phase=CheckPhase.VERIFICATION
            )
        except StoreError as exc:
            # As in ``_advance_to_review``: a transition refused because a stop landed is the
            # stop, reported as the run's recorded state; any other refusal blocks this run.
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    f"the run could not enter the checking phase: {exc}",
                )
            )
        # The root's remaining time, read here and not at the start of the cycle: the implementer
        # attempt consumed the clock, so what is left to check and review with is a different
        # number from the one the attempt was given. Read from the ledger, so a restarted
        # controller reaches the same answer instead of restarting the clock.
        remaining_root_seconds = self._remaining_deadline_seconds()
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
            # A repaired candidate is checked from scratch: the first round's passing rows belong
            # to a different candidate, and reusing them would call a new delivery verified
            # without running anything.
            force_refresh=previous is not None,
            # What is left of the root's clock caps every check. A check allowed to run its full
            # configured timeout while the root has less time than that would let the run work past
            # its own deadline and then accept the result.
            time_budget_seconds=remaining_root_seconds,
        )
        if remaining_root_seconds is not None and remaining_root_seconds <= 0:
            # The clock ran out while the implementer was working. Checks were recorded as
            # not-started errors rather than executed, so the run stops here instead of buying a
            # reviewer for work it can no longer accept.
            self._record_repair_decision(
                run_id,
                RepairRecord(
                    decision=RepairDecision.DEADLINE_REACHED,
                    trigger=trigger,
                    reason=(
                        "the root's deadline passed before this candidate could be checked, so no "
                        "check was started, no reviewer was bought and nothing was accepted"
                    ),
                    policy_digest=policy.digest() if policy is not None else "",
                    round=round_number,
                    decided_at=utc_now(),
                ),
            )
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.BUDGET_EXHAUSTED,
                    "the root's deadline passed while the implementer was working; the candidate "
                    "was not checked and nothing was accepted",
                )
            )
        failure_facts = [
            fact
            for fact in failed_check_facts(
                self.store,
                run_id=run_id,
                attempt_id=attempt_id,
                candidate_fingerprint=post_fingerprint,
                checks_digest=project.checks_digest(),
            )
            if fact.get("status") != EvidenceStatus.PASSED.value
        ]

        review = ReviewResult(status="not_run")
        if verification.status != "passed":
            review = ReviewResult(status="not_run", isolation=self.review_isolation)
        elif spec.needs_review(project):
            # A stop recorded while the implementer or its checks were running is read here,
            # before any state transition or handoff to the reviewer. The transition itself is
            # stop-aware, because the cancel can land between reading this and asking for it.
            stopped = self._advance_to_review(run_id)
            if stopped is not None:
                return _CycleResult(outcome=stopped)
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
                    repo=repo,
                    original_base_commit=original_base_commit,
                    round_number=round_number,
                )
            except RefusedError as exc:
                return _CycleResult(outcome=self._blocked(run_id, exc.code, exc.message))
        else:
            review = ReviewResult(status="not_required", isolation=IsolationLevel.NONE)

        if verification.status != "passed":
            # The only two shapes that may buy a repair are decided here, from recorded facts:
            # a business assertion failing under the policy, or a substantive reviewer rejection.
            return self._repairable_failure(
                run_id=run_id,
                policy=policy,
                round_number=round_number,
                original_base_commit=original_base_commit,
                original_base_ref=original_base_ref,
                previous=previous,
                identity=identity,
                trigger_candidate=RepairTrigger.BUSINESS_CHECK_FAILED,
                failure_facts=failure_facts,
                findings=[],
                detail=verification.detail,
                configured_deadline_seconds=request.deadline_seconds,
            )

        if review.status == "changes_requested":
            findings = self._review_findings(run_id, attempt_id, post_fingerprint)
            return self._repairable_failure(
                run_id=run_id,
                policy=policy,
                round_number=round_number,
                original_base_commit=original_base_commit,
                original_base_ref=original_base_ref,
                previous=previous,
                identity=identity,
                trigger_candidate=RepairTrigger.REVIEW_CHANGES_REQUESTED,
                failure_facts=[],
                findings=findings,
                detail="independent review requested changes",
                configured_deadline_seconds=request.deadline_seconds,
            )
        if review.status not in {"accepted", "not_required"}:
            return _CycleResult(
                outcome=self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    f"unexpected review status {review.status!r}",
                )
            )

        accepted = self._accept(
            run_id=run_id,
            attempt_id=attempt_id,
            spec=spec,
            project_root=execution_root,
            verification=verification,
            review=review,
            observed_turns=result.agent_turns,
            # The delivery diff covers the *whole* change from the task's original base to this
            # final candidate, so a repaired delivery cannot be read as a delivery of the last
            # patch alone. The agent's own claim is not used for this.
            base_ref=original_base_ref,
            limitations=list(result.limitations),
            freeze=freeze,
            repo=repo,
            target_repo_root=project_root,
            user_tree_before=user_tree_before,
            dirty_target=dirty_target,
            identity=identity,
            original_base_commit=original_base_commit,
            project_write_deny=list(project.write_deny),
            git_metadata_before=git_metadata_before,
        )
        return _CycleResult(outcome=accepted, identity=identity, review=review, accepted=True)

    # -- repair (batch E2) ---------------------------------------------------





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
        repo: GitRepo | None = None,
        original_base_commit: str = "",
        round_number: int = 1,
    ) -> ReviewResult:
        """Buy the review turn, run the reviewer, then interpret its structured verdict.

        The reviewer is told the *task*, the *candidate identity the controller froze* and the
        *program evidence the controller recorded* - never the implementer's own summary of its
        work. Those facts come from this run's rows, so a reviewer cannot be handed a plausible
        but invented candidate.

        The candidate is described the way it is delivered: from the task's original base, with
        every path changed since then and the cumulative diff. In a repair round the freeze's own
        base is the previous round's candidate, and a reviewer shown only that patch would vote
        on a delivery whose first round it never saw (when round one failed a check, no reviewer
        was bought for it). That round's own patch is added, labelled, next to the whole change.

        Two things this deliberately does *not* do:

        * it does not let the reviewer's prose set the isolation level (A08) - the level
          is whatever the driver could actually enforce, recorded by the controller;
        * it does not spend a review turn when budget cannot cover it (A03): the
          reservation refuses and the run blocks with no reviewer process started.

        It also does not *start* a reviewer for a run whose stop was already requested: the
        intent is recorded before anything is asked to stop, and buying a turn after that
        would spend allowance and dispatch a process for a decision a human already ended. And a
        reviewer result that arrives after a stop is not applied: the stop is decided in the
        result's first write, and a late result is only a note (see :meth:`_late_review_result`).
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
        #
        # Both the attempt and the candidate are filtered on, and the attempt is the belt to the
        # candidate's braces: a repair that produced an identical fingerprint is stopped as a
        # no-content-change before any reviewer is bought, so today the fingerprint alone already
        # separates the rounds. Keeping the attempt filter makes this reader agree with `_accept`,
        # and means a future fingerprint covering fewer files cannot start mixing rounds.
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
            if r["attempt_id"] == attempt_id
            and r["candidate_fingerprint"] == candidate_fp
            and r["checks_digest"] == checks_digest
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
            if r["attempt_id"] == attempt_id and r["candidate_fingerprint"] == candidate_fp
        ]
        candidate_identity: dict[str, object] = {
            "fingerprint": candidate_fp,
            "worktree": str(project_root),
            "paths": list(freeze.paths) if freeze else [],
        }
        if freeze is not None:
            base = original_base_commit or freeze.base_commit
            candidate_identity |= {
                "base_commit": base,
                "git_commit": freeze.candidate_commit,
                "git_tree": freeze.tree,
            }
            if base != freeze.base_commit:
                # This round started from an earlier round's candidate, not from the base: the
                # paths are the cumulative ones, computed by Git exactly as ``_accept`` does.
                if repo is None:  # pragma: no cover - a freeze only exists in a worktree run
                    raise RefusedError(
                        RefusalCode.INTERNAL_ERROR,
                        "a repair round's candidate has no repository to diff it against its base",
                    )
                try:
                    cumulative = repo.diff_paths(base, freeze.candidate_commit, cwd=project_root)
                except GitError as exc:
                    raise RefusedError(
                        RefusalCode.INTERNAL_ERROR,
                        "the reviewer's view of the whole change could not be computed, so no "
                        f"review was bought: {exc}",
                    ) from exc
                candidate_identity["paths"] = cumulative
            if round_number > 1:
                candidate_identity |= {
                    "round": round_number,
                    "round_parent_commit": freeze.base_commit,
                    "round_paths": list(freeze.paths),
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
                deadline_seconds=self._capped_deadline(900),
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
            # A review is never the repair: the repair is an implementer attempt, and the root's
            # repair counter is charged by that dispatch, not by the reviewer that follows it.
            is_repair=False,
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
            deadline_seconds=self._capped_deadline(900),
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
        # The same ordering fact as the implementer's: only a stop recorded before the handoff
        # proves the driver's gate refused.
        stop_before_launch = self._stop_recorded(run_id)
        try:
            review_invocation = self.reviewer_driver.start(review_request)
        except RefusedError as exc:
            # A deterministic refusal before launch (for example a workspace client config): no
            # review was attempted, so it is reported under its own code rather than as a review
            # protocol error, and the reservation is recorded as never started.
            self._mark_invocation_not_started(dispatch, f"refused before launch: {exc}")
            self.store.attach_review_result(
                attempt_id, {"error": str(exc), "invocation_id": invocation_id}
            )
            raise RefusedError(exc.code, exc.message) from exc
        except Exception as exc:  # noqa: BLE001 - a broken reviewer must not become an accept
            self._mark_driver_failure(dispatch, exc)
            self.store.attach_review_result(
                attempt_id, {"error": repr(exc), "invocation_id": invocation_id}
            )
            raise RefusedError(
                RefusalCode.REVIEW_PROTOCOL_ERROR,
                "the review invocation could not be started, so no verdict exists: " f"{exc!r}",
            ) from exc
        except BaseException:
            self._record_interruption(run_id, attempt_id, "reviewer")
            raise
        self._confirm_driver_ran(
            dispatch, review_invocation, stop_before_launch=stop_before_launch
        )

        if review_invocation.outcome is InvocationOutcome.CANCELLED and self._stop_recorded(run_id):
            recorded = self.store.invocation(invocation_id) if dispatch.invocation else None
            if recorded is None or recorded.state is InvocationStartState.NOT_STARTED:
                # The stop won the handoff inside the driver: the driver reported that no process
                # was created, or (a silent driver) the stop was recorded before it was asked.
                # Nothing is attached to the attempt - there was no invocation - and the run keeps
                # the stop decision it already made. The ledger already says "never started",
                # with the allowance still consumed (it was committed before the handoff).
                raise RefusedError(
                    RefusalCode.CANCELLED_BY_OPERATOR,
                    "the stop was seen before the reviewer process was created, so none was "
                    "started and no reviewer invocation is recorded",
                )
            # Otherwise the stop reached a reviewer that was already running. Its cancelled
            # result is a late one, like any other result after a stop: handled below.

        # The stop is decided in the first write of the result, as for the implementer
        # (``_apply_result_or_stay_stopped``). A stop recorded first - confirmed or not - makes
        # this result a late one: it is a note, the ledger entry keeps the state the stop left
        # (an unconfirmed stop leaves it open, so the root stays blocked) and no verdict or
        # evidence is recorded. A stop that commits after this write keeps the applied result.
        if not self.store.attach_review_result_unless_stopped(
            run_id, attempt_id, review_invocation.model_dump(mode="json")
        ):
            raise self._late_review_result(
                run_id=run_id,
                attempt_id=attempt_id,
                invocation_id=invocation_id,
                outcome=review_invocation.outcome,
            )
        self._release_invocation(self.reviewer_driver, run_id, invocation_id)
        pid, started_at, identity = ProcessGuard(self.controller_id).identity()
        self.store.record_process_identity(
            attempt_id, pid=pid, started_at=started_at, identity=identity, session_id=attempt_id
        )
        if review_invocation.outcome is InvocationOutcome.OUTCOME_UNKNOWN:
            # Nobody knows how this turn ended - it may not even have ended. That is an unknown
            # outcome, not a wire failure: the ledger and the run both say so, the root stays
            # blocked, and ``resume`` reconciles it without re-dispatching.
            self._settle_invocation(
                dispatch, review_invocation.outcome, "review invocation outcome unknown"
            )
            raise RefusedError(
                RefusalCode.OUTCOME_UNKNOWN,
                "the review invocation's outcome is unknown "
                f"({review_invocation.error_code or 'no code'}: "
                f"{review_invocation.error_message or 'no detail'}); it produced no verdict and "
                "nothing is re-dispatched",
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

        # The ledger records what the controller concluded about this turn, not the driver's raw
        # outcome: a turn the controller refuses as unknown (an unbound completion) is settled as
        # unknown, so the root stays blocked until it is reconciled.
        settled_as = review_invocation.outcome
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
        except RefusedError as exc:
            if exc.code is RefusalCode.OUTCOME_UNKNOWN:
                settled_as = InvocationOutcome.OUTCOME_UNKNOWN
            raise
        finally:
            # One close for every way out of this block, including a vanished verdict and an
            # unexpected error in the evidence write. Without it a reviewer that ran but whose
            # verdict was unusable would leave its invocation open forever, and an open
            # invocation blocks the whole root - a wedge, not a safety property.
            self._settle_invocation(
                dispatch,
                settled_as,
                "review invocation outcome unknown"
                if settled_as is InvocationOutcome.OUTCOME_UNKNOWN
                else "review invocation completed",
            )

    def _late_review_result(
        self,
        *,
        run_id: str,
        attempt_id: str,
        invocation_id: str,
        outcome: InvocationOutcome,
    ) -> RefusedError:
        """Record a reviewer result that arrived after the run's stop, and return the refusal.

        The reviewer's half of :meth:`_apply_result_or_stay_stopped`: the result decides nothing
        any more, so it is a ``late_result`` note. No verdict is attached, no evidence and no
        process identity are recorded, and the ledger entry is **not settled** - after an
        unconfirmed stop it stays open and keeps blocking the root until ``resume`` reconciles
        it; an entry the stop already closed keeps that state, and the refused settlement is
        noted. The driver is still released: the result is on record, as a note.

        The caller raises the returned error; ``_blocked`` then keeps the block the stop recorded.
        """
        self.store.record_note(
            run_id,
            f"{NOTE_LATE_RESULT}: the {outcome.value} result of reviewer invocation "
            f"{invocation_id} for attempt {attempt_id} arrived after the run's stop was "
            "recorded; no verdict or evidence was recorded, the ledger entry keeps the state the "
            "stop left, the run keeps its recorded decision and nothing is re-dispatched",
        )
        self._release_invocation(self.reviewer_driver, run_id, invocation_id)
        recorded = self.store.invocation(invocation_id)
        if recorded is not None and recorded.state.value not in INVOCATION_OPEN_STATES:
            self._note_not_settled(run_id, invocation_id, outcome)
        return RefusedError(
            RefusalCode.CANCELLED_BY_OPERATOR,
            f"the reviewer's {outcome.value} result arrived after the stop was recorded; it is "
            "recorded as a late_result note and decides nothing",
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
        identity: CandidateIdentity | None = None,
        original_base_commit: str = "",
        project_write_deny: list[str] | None = None,
        git_metadata_before: GitMetadataSnapshot | None = None,
    ) -> RunOutcome:
        """Admission gate. Every field of the receipt is re-derived from stored facts.

        The delivery paths are held to the task's scope once more before a receipt names them:
        ``project_write_deny`` carries the project's deny rules, and the task's own and the
        built-in deny list always apply. A delivery that names a path the scope does not allow is
        refused as a scope violation instead of being written into a receipt.

        Accepts the frozen *identity of this round* so the receipt and the run's notes name the
        candidate that was actually verified, and the task's *original base* so the delivery it
        describes is the cumulative change a repaired run actually produced - base to final
        candidate - rather than the last round's patch.

        The DSH context limitation is re-derived from that same Git delivery diff, never read
        back from the stored ``dsh_context`` records, so a bad note cannot fail an acceptance.

        Always returns the run's outcome; a refusal of the acceptance write never escapes. A stop
        can commit after the intent is read here and before ``finalize_acceptance``, which then
        refuses the late success in its own transaction: the outcome is the state the stop
        recorded, with no receipt. Any other refusal of that write blocks the run through the
        stop-conditional :meth:`_blocked`.
        """
        row = self.store.get_run(run_id)
        # The deadline is checked at acceptance as well as before each dispatch. A run can spend
        # its last seconds inside a review that was bought in time; accepting the result would mean
        # the recorded deadline bounded nothing at all.
        expired = self._root_deadline_state()
        if expired is not None:
            return self._blocked(run_id, RefusalCode.BUDGET_EXHAUSTED, expired)
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

        if repo is not None:
            # Checks and the reviewer ran after the freeze's comparison, and the status read of the
            # user's checkout runs after the receipt, so the metadata is compared first. Here
            # ``project_root`` is the run's worktree.
            metadata = self._git_metadata_refusal(
                run_id,
                repo,
                project_root,
                git_metadata_before,
                where="at acceptance",
                consequence="no receipt was written",
            )
            if metadata is not None:
                return self._blocked(run_id, *metadata)

        try:
            fresh_fingerprint = candidate_fingerprint(project_root, spec.scope)
        except RefusedError as exc:
            # The scope no longer resolves inside the workspace, so the verified content cannot
            # be re-read: that is a recorded block, never an exception out of the run.
            return self._blocked(
                run_id,
                exc.code,
                f"the candidate cannot be fingerprinted again at acceptance: {exc.message}",
            )
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

        # --- the delivery is cumulative, from the task's original base ----------------
        # A repaired run's candidate contains every round's change, but the *second* round's
        # freeze only knows the second round's parent and the paths it touched. Reporting those
        # would hand an integration step a delivery that appears to change only the last patch,
        # and the first round's change would be silently missing. So the delivery base is the
        # task's original base, and the paths are the diff between that base and this candidate,
        # computed by Git rather than accumulated in memory.
        delivery_base = original_base_commit or (freeze.base_commit if freeze else "")
        if freeze is not None and repo is not None and delivery_base:
            if not repo.commit_exists(delivery_base):  # pragma: no cover - defensive
                return self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    f"the task's original base {delivery_base} is not a commit in this "
                    "repository, so the cumulative delivery diff cannot be computed",
                )
            try:
                delivery_paths = repo.diff_paths(
                    delivery_base, freeze.candidate_commit, cwd=project_root
                )
            except GitError as exc:
                return self._blocked(
                    run_id,
                    RefusalCode.INTERNAL_ERROR,
                    f"the cumulative delivery diff could not be computed: {exc}",
                )
        else:
            # No frozen candidate or no recorded base (an in-place run, or a driver that produced
            # no Git identity): the local paths are all that is known, and inventing a range would
            # be worse than saying less.
            delivery_paths = list(freeze.paths) if freeze else []
        undelivered = paths_outside_scope(delivery_paths, spec.scope, project_write_deny or [])
        if undelivered:
            return self._blocked(
                run_id,
                RefusalCode.SCOPE_VIOLATION,
                "the delivery changes paths its TaskSpec did not authorize (outside write_allow "
                "or under a write_deny rule); no receipt was written: "
                + ", ".join(undelivered[:5])
                + (f" (+{len(undelivered) - 5} more)" if len(undelivered) > 5 else ""),
            )
        # In-tree attribute files travel with the candidate and show in the delivery, so they are
        # recorded rather than refused; ``freeze`` is set only when HFlow's own git staged them.
        attribute_files = (
            [path for path in delivery_paths if path.rsplit("/", 1)[-1].lower() == ".gitattributes"]
            if freeze is not None
            else []
        )
        dsh_changed = dsh_context_paths(delivery_paths) if freeze is not None else []

        receipt = ResultReceipt(
            run_id=run_id,
            task_id=spec.task_id,
            attempt_id=attempt_id,
            task_revision=int(row["task_revision"]),
            runtime_build=self.controller_build,
            plan_digest=row["spec_digest"],
            harness_outcome=InvocationOutcome.COMPLETED,
            candidate=CandidateSnapshot(
                base_commit=delivery_base or f"base:{row['spec_digest'][:18]}",
                git_commit=freeze.candidate_commit if freeze else "",
                git_tree=freeze.tree if freeze else "",
                worktree=str(project_root) if freeze else "",
                fingerprint=fresh_fingerprint,
            ),
            candidate_paths=delivery_paths,
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
                *(
                    [
                        "the delivery changes "
                        + ", ".join(attribute_files[:5])
                        + ": HFlow's own git add applied those attributes when it froze the "
                        "candidate, under Git configuration verified unchanged since dispatch, so a "
                        "filter or line-ending rule they name can make the committed bytes differ "
                        "from the bytes the checks and the fingerprint read"
                    ]
                    if attribute_files
                    else []
                ),
                "review ran in the implementer's invocations' workspace; its isolation is not "
                "independently enforced",
                *(
                    [
                        "the candidate changes files that upstream DSH source (dsh-v0.2.0-rc.2) "
                        "says a DSH agent started in this worktree loads as instructions, skills "
                        f"or environment ({_first_paths(dsh_changed)}); every role started there "
                        "after the freeze - the reviewer when one ran, a repair implementer when "
                        "there was one - would have them in its context. Recorded from the Git "
                        "diff, not observed in an agent and not refused"
                    ]
                    if dsh_changed
                    else []
                ),
            ],
        )

        try:
            self.store.finalize_acceptance(run_id, receipt, checks_digest=row["checks_digest"])
        except RunNotFound:
            raise
        except StoreError as exc:
            if self._stop_recorded(run_id):
                # A stop committed after the intent check above and before this write. The store
                # refused the late success in the same transaction; the stop decides the state.
                return self._outcome_for(
                    run_id,
                    notes=[
                        "a stop was recorded while the acceptance was being prepared; no receipt "
                        "was written and the run keeps the state the stop records"
                    ],
                )
            return self._blocked(
                run_id, RefusalCode.INTERNAL_ERROR, f"the acceptance could not be recorded: {exc}"
            )

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
        dispatch: DispatchReservation,
        settle_detail: str,
        block_code: RefusalCode | None = None,
        reason: str = "",
    ) -> bool:
        """Apply one implementer result, unless the run was stopped while it was in flight.

        ``True`` when the result was applied to the live attempt, and only then is the ledger
        entry settled with it. ``False`` when the run's stop is recorded - confirmed or not - or
        the attempt had already been finalized. That result is then a fact about an invocation
        which no longer decides anything: it is recorded as a ``late_result`` note and the run
        keeps the decision already taken. A late success must not overwrite a stop, and a late
        answer must not resurrect a run somebody stopped.

        The stop is decided **in the attempt write** (``finish_attempt(unless_stopped=True)``),
        before the ledger is touched. After an unconfirmed stop the attempt stays live and the
        ledger entry stays open - "work may still be running" must keep blocking the root, and
        ``resume`` reconciles both. An entry a confirmed stop already closed keeps that state.

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
                unless_stopped=True,
            )
        except StoreError as exc:
            self.store.record_note(
                run_id,
                f"{NOTE_LATE_RESULT}: the {outcome.value} result of invocation "
                f"{result.invocation_id} for attempt {attempt_id} arrived after the run's stop "
                f"or the attempt's end was recorded ({exc}); the attempt and its ledger entry "
                "keep the state the stop left, the run keeps its recorded decision and nothing "
                "is re-dispatched",
            )
            recorded = (
                self.store.invocation(dispatch.invocation.invocation_id)
                if dispatch.invocation is not None
                else None
            )
            if recorded is not None and recorded.state.value not in INVOCATION_OPEN_STATES:
                self._note_not_settled(run_id, recorded.invocation_id, outcome)
            return False
        self._settle_invocation(dispatch, outcome, settle_detail)
        if block_code is not None:
            self._blocked(run_id, block_code, reason)
        return True
    def _stopped_before_freeze(
        self, run_id: str, attempt_id: str, *, frozen_commit: str = ""
    ) -> RunOutcome:
        """Report a run whose stop was recorded after its implementer result was applied.

        The result stands on the attempt (it arrived before the stop), but no candidate is
        retained for a stopped run. ``frozen_commit`` is a candidate commit made in the run's
        worktree just before the stop was seen: it is named here so it is not mistaken for a
        delivery, and no ``refs/hflow/candidates`` ref is created for it.
        """
        detail = (
            f"candidate commit {frozen_commit} was made in the worktree before the stop was seen "
            "and is not retained by a candidate ref"
            if frozen_commit
            else "no candidate was frozen"
        )
        self.store.record_note(
            run_id,
            f"a stop was recorded after the result of attempt {attempt_id} was applied; {detail}, "
            "and the run keeps the state the stop recorded",
        )
        return self._outcome_for(run_id)

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



class _CycleResult:
    """What one implementer attempt ended as, in terms the driver loop can act on.

    ``outcome`` is set when the run is finished - accepted, blocked, stopped, unknown. Otherwise
    ``repair`` says the attempt may be followed by exactly one repair, and carries the trigger and
    the structured context that repair needs. Exactly one of the two is set, which is what keeps
    "the run is over" and "the run may continue" from being inferred from a null somewhere.
    """

    __slots__ = ("accepted", "identity", "outcome", "repair", "review")

    def __init__(
        self,
        *,
        outcome: RunOutcome | None = None,
        repair: RepairContext | None = None,
        identity: CandidateIdentity | None = None,
        review: ReviewResult | None = None,
        accepted: bool = False,
    ) -> None:
        self.outcome = outcome
        self.repair = repair
        self.identity = identity
        self.review = review
        self.accepted = accepted


def _first_paths(paths: list[str], limit: int = 20) -> str:
    """The first ``limit`` paths joined for a receipt line, with a count of the rest."""
    shown = ", ".join(paths[:limit])
    return shown + (f" (+{len(paths) - limit} more)" if len(paths) > limit else "")


def _stored_model_facts(row: Any, column: str, prefix: str) -> dict[str, Any]:
    """The model facts an attempt's stored invocation result carries, read as recorded.

    A result without them - written before they existed, by the offline driver, or not a driver
    result at all - yields nothing, which ``status`` reports as "not recorded" rather than as an
    absent model.
    """
    from .contracts import ModelApplied, ModelObservation

    raw = row[column] if column in row.keys() else None
    try:
        payload = json.loads(raw) if raw else {}
        observation = payload.get("model_observation")
        applied = payload.get("model_applied")
        return {
            f"{prefix}model_observation": ModelObservation.model_validate(observation)
            if isinstance(observation, dict)
            else None,
            f"{prefix}model_applied": ModelApplied(applied) if applied else None,
        }
    except (ValueError, TypeError, AttributeError):
        return {}


def _stored_stream_order(row: Any, column: str, prefix: str) -> dict[str, Any]:
    """The stream order an attempt's stored invocation result carries, read as recorded.

    Built like ``_stored_model_facts``: a result without it (an unbound turn, a stream not read to
    its end, the offline driver, an older run) yields ``None``, which ``status`` reports as
    unknown, never as "0 updates".
    """
    from .contracts import StreamOrder

    raw = row[column] if column in row.keys() else None
    try:
        payload = json.loads(raw) if raw else {}
        order = payload.get("stream_order")
        return {
            f"{prefix}stream_order": StreamOrder.model_validate(order)
            if isinstance(order, dict)
            else None
        }
    except (ValueError, TypeError, AttributeError):
        return {}


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
            # Batch E2: read from the attempt's own row, so a reader can tell the first attempt
            # from the repair attempt without counting rows or inferring it from a later round.
            is_repair=bool(a["is_repair"]) if "is_repair" in a.keys() else False,
            created_at=a["created_at"],
            finished_at=a["finished_at"],
            **_stored_model_facts(a, "result_json", ""),
            **_stored_model_facts(a, "review_json", "review_"),
            **_stored_stream_order(a, "result_json", ""),
            **_stored_stream_order(a, "review_json", "review_"),
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
            exit_reason=str(e["exit_reason"]) if "exit_reason" in e.keys() else "",
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
    # Batch E2 repair decisions: every one the run recorded, refusals included. A run that never
    # decided anything reports an empty list, which the text projection renders as "no decision
    # recorded" rather than as an empty table.
    try:
        repair_records = store.repair_records_for(run_id)
    except AttributeError:  # pragma: no cover - the store method lands with the same batch
        repair_records = []
    return RunInspection(
        run=_summary_from_row(row, workspace_matches_receipt=drift),
        task_spec=TaskSpec.model_validate(json.loads(row["task_spec_json"])),
        attempts=attempts,
        evidence=evidence,
        receipt=ResultReceipt.model_validate(json.loads(row["receipt_json"]))
        if row["receipt_json"]
        else None,
        repair_records=repair_records,
        dsh_context=store.dsh_context_for(run_id),
        effective_config=recorded_config,
        model_calls_made=0,
        root_budget=root_usage,
        invocations=store.invocations_for(run_id),
        invocation_counts=store.invocation_state_counts(run_id),
    )
