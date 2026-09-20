"""Single source of truth for every HFlow data contract.

Rules for this module (they are the reason it exists):

* Every persistent/transportable structure is declared here exactly once.
  JSON Schema is *generated* from these models (see :func:`json_schema`),
  never hand-written a second time.
* The controller derives every field of ``ResultReceipt`` from recorded facts.
  A worker/agent can never submit a receipt, a task state, or a verification
  result: those types are not part of the driver protocol at all.
* ``unknown`` is a first-class value. Fields that cannot be observed are
  ``None`` (serialized as JSON ``null``), never a fabricated 0.

Field names mirror the plan (sections 16.1-16.5) so a reader of the plan can
find the implementation of each contract.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

# ``Strict`` forbids unknown fields everywhere: an agent that invents a field
# gets a hard validation error instead of a silently ignored key.
Strict = ConfigDict(extra="forbid")


class HFlowContractError(ValueError):
    """Raised when persisted data no longer matches the declared contract."""


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: Any) -> str:
    """Stable content hash of any JSON-serializable payload."""
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


T = TypeVar("T", bound=BaseModel)


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generated JSON Schema for a contract model (no parallel schema file)."""
    return model.model_json_schema()


# --------------------------------------------------------------------------
# Task lifecycle vocabulary (plan 9.1)
# --------------------------------------------------------------------------


class TaskState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    CHECKING = "CHECKING"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    SUPERSEDED = "SUPERSEDED"


class DeliveryState(StrEnum):
    NONE = "NONE"
    LOCAL_CANDIDATE = "LOCAL_CANDIDATE"
    INTEGRATED = "INTEGRATED"
    PUBLISHED = "PUBLISHED"


class CheckPhase(StrEnum):
    VERIFICATION = "verification"
    REVIEW = "review"
    INTEGRATION_CHECK = "integration-check"


class EvidenceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class ReuseStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    EXEMPT = "exempt"
    EXISTING_DECISION = "existing_decision"
    DECIDED = "decided"


class IsolationLevel(StrEnum):
    """How strongly a reviewer was *actually* constrained - never a self-claim."""

    NONE = "none"
    PROMPT_ONLY = "prompt_only"
    TOOL_POLICY = "tool_policy"
    WORKSPACE_FROZEN = "workspace_frozen"
    READONLY_ENFORCED = "readonly_enforced"
    UNKNOWN = "unknown"


class ReconcileOutcome(StrEnum):
    """Result of looking at an interrupted attempt (plan 9.3)."""

    NOT_STARTED = "not_started"
    STILL_RUNNING = "still_running"
    FINISHED_RESULT_UNPROCESSED = "finished_result_unprocessed"
    UNKNOWN = "unknown"


RiskLevel = Literal["low", "standard", "strict"]


# --------------------------------------------------------------------------
# Project contract: <target repo>/.hflow/project.json
# --------------------------------------------------------------------------


class CheckDef(BaseModel):
    """An approved check. TaskSpec refers to it by ID; it cannot pass a command."""

    model_config = Strict

    id: str
    kind: Literal["fake", "command"]
    argv: list[str] = Field(default_factory=list)
    timeout_seconds: int = 600
    cacheable: bool = Field(
        default=True,
        description="False for non-deterministic checks (plan A12): never reused.",
    )

    @model_validator(mode="after")
    def _validate_kind(self) -> CheckDef:
        if not self.id.strip():
            raise ValueError("check id must be non-empty")
        if self.kind == "command" and not self.argv:
            raise ValueError(f"check {self.id!r}: command checks need a non-empty argv")
        if self.kind == "fake" and self.argv:
            raise ValueError(f"check {self.id!r}: fake checks must not carry argv")
        if self.timeout_seconds <= 0:
            raise ValueError(f"check {self.id!r}: timeout_seconds must be > 0")
        return self


class ProjectLimits(BaseModel):
    """Pre-authorized ceilings. A TaskSpec may ask for less, never for more."""

    model_config = Strict

    max_agent_turns: int = 4
    max_repair_cycles: int = 1
    max_parallel_workers: int = 1
    max_native_children: int = 0

    @model_validator(mode="after")
    def _validate_limits(self) -> ProjectLimits:
        if self.max_agent_turns < 1:
            raise ValueError("max_agent_turns must be >= 1")
        if self.max_repair_cycles < 0:
            raise ValueError("max_repair_cycles must be >= 0")
        return self


class ProjectConfig(BaseModel):
    """Declarative only: no embedded expressions, no network-loaded rules."""

    model_config = Strict

    schema_version: int = SCHEMA_VERSION
    project_id: str
    checks: list[CheckDef]
    write_deny: list[str] = Field(default_factory=lambda: [".hflow/**", ".github/**"])
    limits: ProjectLimits = Field(default_factory=ProjectLimits)
    min_risk_for_review: RiskLevel = "standard"
    review_required: bool = True

    def check_map(self) -> dict[str, CheckDef]:
        return {check.id: check for check in self.checks}

    def checks_digest(self) -> str:
        """Fingerprint of the approved checks; evidence from another digest is stale."""
        return digest_of([check.model_dump(mode="json") for check in self.checks])


# --------------------------------------------------------------------------
# Task contract: TaskSpec (plan 16.1)
# --------------------------------------------------------------------------


class AcceptanceCriterion(BaseModel):
    model_config = Strict

    id: str
    statement: str
    check_ids: list[str]


class Scope(BaseModel):
    model_config = Strict

    write_allow: list[str]
    write_deny: list[str] = Field(default_factory=list)


class ReuseDecision(BaseModel):
    """Shortest useful reuse record (plan 11.2). Still just a gate, not a report."""

    model_config = Strict

    status: ReuseStatus
    need: str = ""
    local_search: str = ""
    external_candidates: list[dict[str, Any]] = Field(default_factory=list)
    choice: Literal["reuse", "adapt", "build", "defer", "none"] = "none"
    reason: str = ""
    reference: str = ""
    required_fit_test: str = ""
    fit_test_status: Literal["passed", "failed", "pending", "not_required"] = "not_required"
    revisit_on: list[str] = Field(default_factory=list)


class ReviewRequirement(BaseModel):
    model_config = Strict

    required: bool = True


class DeliveryRequirement(BaseModel):
    model_config = Strict

    mode: Literal["local_candidate", "integrated", "published"] = "local_candidate"


class BudgetRequest(BaseModel):
    model_config = Strict

    max_agent_turns: int = 4
    max_repair_cycles: int = 1


class TaskSpec(BaseModel):
    model_config = Strict

    schema_version: int = SCHEMA_VERSION
    task_id: str
    revision: int = 1
    goal: str
    acceptance: list[AcceptanceCriterion]
    scope: Scope
    risk: RiskLevel = "standard"
    dependencies: list[str] = Field(default_factory=list)
    reuse: ReuseDecision
    review: ReviewRequirement = Field(default_factory=ReviewRequirement)
    delivery: DeliveryRequirement = Field(default_factory=DeliveryRequirement)
    budget: BudgetRequest = Field(default_factory=BudgetRequest)

    @model_validator(mode="after")
    def _validate_shape(self) -> TaskSpec:
        if not self.goal.strip():
            raise ValueError("goal must be non-empty")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        if not self.acceptance:
            raise ValueError("acceptance must contain at least one criterion")
        ids = [criterion.id for criterion in self.acceptance]
        if len(set(ids)) != len(ids):
            raise ValueError("acceptance ids must be unique")
        for criterion in self.acceptance:
            if not criterion.check_ids:
                raise ValueError(f"acceptance {criterion.id!r} must reference at least one check")
        if self.budget.max_agent_turns < 1:
            raise ValueError("budget.max_agent_turns must be >= 1")
        return self

    def spec_digest(self) -> str:
        """Idempotency key input: identical spec text => identical digest."""
        return digest_of(self.model_dump(mode="json"))

    def required_check_ids(self) -> list[str]:
        seen: list[str] = []
        for criterion in self.acceptance:
            for check_id in criterion.check_ids:
                if check_id not in seen:
                    seen.append(check_id)
        return seen

    def needs_review(self, project: ProjectConfig) -> bool:
        if not project.review_required:
            return False
        if not self.review.required:
            # A task may not lower the project's risk requirement; validation
            # rejects that combination before we ever get here.
            return False
        return True


# --------------------------------------------------------------------------
# Local machine profile (plan 16.2): bindings live outside project state
# --------------------------------------------------------------------------


class AgentBinding(BaseModel):
    model_config = Strict

    harness: str
    driver: str
    model_selection: str = "native_profile"
    capability_record: str = ""


class ProfileLimits(BaseModel):
    model_config = Strict

    max_parallel_workers: int = 1
    max_native_children: int = 0
    codex_agent_turns: int = 0


class MachineProfile(BaseModel):
    model_config = Strict

    schema_version: int = SCHEMA_VERSION
    profile_id: str
    role_bindings: dict[str, str]
    agents: dict[str, AgentBinding]
    limits: ProfileLimits = Field(default_factory=ProfileLimits)
    security_mode: Literal["trusted_local", "restricted"] = "trusted_local"

    @model_validator(mode="after")
    def _validate_bindings(self) -> MachineProfile:
        for role, agent in self.role_bindings.items():
            if agent not in self.agents:
                raise ValueError(f"role {role!r} binds unknown agent {agent!r}")
        return self


# --------------------------------------------------------------------------
# Driver-facing contracts (plan 16.3). Agents never see TaskState.
# --------------------------------------------------------------------------


class CapabilityState(StrEnum):
    DOCUMENTED = "documented"
    PROBED = "probed"
    ENFORCED = "enforced"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilityReport(BaseModel):
    model_config = Strict

    driver_id: str
    driver_version: str
    harness: str
    harness_version: str | None = None
    os: str
    arch: str
    probe_only: bool = True
    capabilities: dict[str, CapabilityState] = Field(default_factory=dict)
    live_tested: bool = False
    notes: list[str] = Field(default_factory=list)


class CandidateRef(BaseModel):
    """What a worker claims it produced. The controller freezes the real value."""

    model_config = Strict

    base_ref: str = ""
    change_summary: str = ""
    produced_paths: list[str] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    """Shortest structured reviewer output (plan 16.5)."""

    model_config = Strict

    verdict: Literal["accepted", "changes_requested"]
    findings: list[dict[str, Any]] = Field(default_factory=list)


class InvocationRequest(BaseModel):
    model_config = Strict

    invocation_id: str
    attempt_id: str
    run_id: str
    role: Literal["implementer", "reviewer", "planner"]
    task_id: str
    task_revision: int
    goal: str
    acceptance: list[AcceptanceCriterion]
    write_allow: list[str]
    write_deny: list[str]
    workspace: str
    deadline_seconds: int
    spec_digest: str
    #: Where a driver may keep invocation-scoped scratch (config, raw event logs, session
    #: state). Never the project checkout: scaffolding inside the workspace would show up in
    #: candidate snapshots and dirty the tree under test.
    data_dir: str = ""


class InvocationOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class InvocationResult(BaseModel):
    """What a driver may report. Note the absence of any task/acceptance state."""

    model_config = Strict

    invocation_id: str
    outcome: InvocationOutcome
    candidate: CandidateRef | None = None
    review: ReviewOutput | None = None
    agent_turns: int | None = None
    reported_cost: float | None = None
    provider_billed_tokens: int | None = None
    limitations: list[str] = Field(default_factory=list)
    raw_ref: str = ""
    error_code: str | None = None
    error_message: str | None = None


class CancellationReceipt(BaseModel):
    model_config = Strict

    invocation_id: str
    status: Literal["confirmed_stopped", "still_running", "unknown"]
    #: ``cooperative`` = the protocol/graceful path finished the work; ``forced`` = the
    #: managed process boundary was terminated; ``none`` = nothing was stopped.
    mechanism: Literal["cooperative", "forced", "none"] = "none"
    local_process_stopped: bool | None = None
    detail: str = ""


class EventKind(StrEnum):
    """Neutral event vocabulary. A Harness's internal events are never invented here."""

    STARTED = "started"
    PROGRESS = "progress"
    USAGE = "usage"
    PERMISSION_REQUESTED = "permission_requested"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class NormalizedEvent(BaseModel):
    """One observed event, with the raw payload kept only as a bounded reference."""

    model_config = Strict

    kind: EventKind
    sequence: int = 0
    at: str = ""
    message: str = ""
    method: str = ""
    raw_ref: str = ""


class DriverHandle(BaseModel):
    """Bookkeeping for one live invocation.

    Start returns this instead of blocking until the end: the controller (or a test) can
    observe events and ask for a stop while the work is still running.
    """

    model_config = Strict

    invocation_id: str
    attempt_id: str
    run_id: str
    role: str
    workspace: str
    started_at: str
    process_identity: str
    boundary_kind: str
    pid: int | None = None
    session_id: str | None = None
    dispatched: bool = False
    dispatched_at: str | None = None
    event_log: str = ""
    #: Set once the invocation reached a terminal state, so repeated calls are cheap.
    finished: bool = False


class ReconcileResult(BaseModel):
    model_config = Strict

    invocation_id: str
    outcome: ReconcileOutcome
    detail: str = ""
    protocol_cancel_supported: bool | None = None
    local_process_alive: bool | None = None


class HarnessDriver(Protocol):
    """The only seam between the deterministic controller and a real Harness."""

    driver_id: str

    def probe(self, binding: AgentBinding) -> CapabilityReport: ...

    def start(self, request: InvocationRequest) -> InvocationResult: ...

    def cancel(self, invocation_id: str) -> CancellationReceipt: ...

    def reconcile(self, invocation_id: str) -> ReconcileResult: ...


class LifecycleDriver(Protocol):
    """Optional extension: a driver whose invocation can be observed and stopped while live.

    A driver that only implements :class:`HarnessDriver` remains valid; the controller
    degrades to "start returns when the invocation is over", and cancellation then reports
    what it can honestly report. The point of this protocol is that ``start`` must not be
    the only thing that can happen to a running invocation.
    """

    driver_id: str

    def probe(self, binding: AgentBinding) -> CapabilityReport: ...

    def start_handle(self, request: InvocationRequest) -> DriverHandle: ...

    def observe(self, handle: DriverHandle) -> Iterator[NormalizedEvent]: ...

    def collect(self, handle: DriverHandle) -> InvocationResult: ...

    def cancel_handle(self, handle: DriverHandle) -> CancellationReceipt: ...

    def reconcile_handle(self, handle: DriverHandle) -> ReconcileResult: ...


# --------------------------------------------------------------------------
# Controller-generated result contract (plan 16.4)
# --------------------------------------------------------------------------


class CandidateSnapshot(BaseModel):
    model_config = Strict

    base_commit: str
    tree_hash: str


class VerificationResult(BaseModel):
    model_config = Strict

    status: Literal["passed", "failed", "not_run"]
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class ReviewResult(BaseModel):
    model_config = Strict

    status: Literal["accepted", "changes_requested", "not_required", "not_run"]
    isolation: IsolationLevel = IsolationLevel.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)
    checked_fingerprint: str = ""


class UsageFacts(BaseModel):
    """Three counters stay separate (plan 10.2). Unknown is null, never 0."""

    model_config = Strict

    controller_turns_reserved: int = 0
    controller_turns_observed: int | None = None
    provider_billed_tokens: int | None = None
    provider_cost: float | None = None
    subscription_quota_remaining: None = None


class ResultReceipt(BaseModel):
    model_config = Strict

    schema_version: int = SCHEMA_VERSION
    run_id: str
    task_id: str
    attempt_id: str
    task_revision: int
    runtime_build: str
    plan_digest: str
    harness_outcome: InvocationOutcome
    candidate: CandidateSnapshot
    verification: VerificationResult
    review: ReviewResult
    task_state: TaskState
    delivery_state: DeliveryState
    usage: UsageFacts
    limitations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Admission / refusal
# --------------------------------------------------------------------------


class RefusalCode(StrEnum):
    INVALID_SPEC = "invalid_spec"
    PROJECT_MISMATCH = "project_mismatch"
    SCOPE_VIOLATION = "scope_violation"
    UNKNOWN_CHECK = "unknown_check"
    REUSE_NOT_APPROVED = "reuse_not_approved"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RISK_DOWNGRADE = "risk_downgrade"
    RUN_CLAIMED_BY_OTHER = "run_claimed_by_other"
    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"
    LATE_RESULT = "late_result"
    VERIFICATION_FAILED = "verification_failed"
    EVIDENCE_STALE = "evidence_stale"
    REVIEW_REJECTED = "review_rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DRIVER_FAILED = "driver_failed"
    CANCELLED_BY_OPERATOR = "cancelled_by_operator"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


class RefusedError(RuntimeError):
    """A deterministic refusal. Never a model judgment, always a controller rule."""

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


class ValidationIssue(BaseModel):
    model_config = Strict

    code: RefusalCode
    detail: str
    location: str = ""


class ValidationReport(BaseModel):
    model_config = Strict

    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    spec_digest: str = ""
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ValidationReport:
        if self.ok and self.issues:
            raise ValueError("ValidationReport cannot be ok=True with issues")
        return self


# --------------------------------------------------------------------------
# Input packages (used by CLI and tests)
# --------------------------------------------------------------------------


class RunRequest(BaseModel):
    """Everything the controller needs for one run. No credentials, no paths outside."""

    model_config = Strict

    task: TaskSpec
    project: ProjectConfig
    project_root: Path
    workspace_root: Path
    deadline_seconds: int = 900
    controller_id: str = "local-controller"


class RunSummary(BaseModel):
    model_config = Strict

    run_id: str
    task_id: str
    task_revision: int
    spec_digest: str
    task_state: TaskState
    phase: CheckPhase | None = None
    delivery_state: DeliveryState
    claimed_by: str | None = None
    agent_turns_reserved: int = 0
    agent_turns_limit: int = 0
    agent_turns_observed: int | None = None
    block_code: str | None = None
    block_reason: str | None = None
    #: Read-time drift check: does the workspace still match the accepted candidate
    #: fingerprint? ``None`` when the run has no receipt to compare against.
    workspace_matches_receipt: bool | None = None
    created_at: str
    updated_at: str


class EvidenceRecord(BaseModel):
    model_config = Strict

    evidence_id: str
    run_id: str
    attempt_id: str
    kind: Literal["verification", "review"]
    status: EvidenceStatus
    check_id: str = ""
    candidate_fingerprint: str = ""
    checks_digest: str = ""
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    stdout_digest: str = ""
    stderr_digest: str = ""
    detail: str = ""
    created_at: str = ""


class AttemptRecord(BaseModel):
    model_config = Strict

    attempt_id: str
    run_id: str
    task_revision: int
    role: str
    state: AttemptState
    reservation_id: str | None = None
    reserved_agent_turns: int = 0
    reserved_expires_at: str | None = None
    process_id: int | None = None
    process_started_at: str | None = None
    process_identity: str | None = None
    session_id: str | None = None
    invocation_id: str | None = None
    review_invocation_id: str | None = None
    outcome: InvocationOutcome | None = None
    result_digest: str | None = None
    block_code: str | None = None
    created_at: str = ""
    finished_at: str | None = None


class RunInspection(BaseModel):
    """Read model for `status`/`report`; assembled only from SQLite."""

    model_config = Strict

    run: RunSummary
    task_spec: TaskSpec
    attempts: list[AttemptRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    receipt: ResultReceipt | None = None
    model_calls_made: int = 0
