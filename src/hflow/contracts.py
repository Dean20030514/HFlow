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
from collections.abc import Callable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

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


# --------------------------------------------------------------------------
# Batch E2: one bounded business repair
# --------------------------------------------------------------------------
#
# A repair happens only when a task carries an explicit ``RepairPolicy``, and that policy names
# what may trigger it: which approved checks count as a business assertion failing, with which
# exit codes, and whether a substantive reviewer rejection qualifies. HFlow cannot read a root
# cause out of an arbitrary process exit code, and pretending otherwise is how an environment
# failure would get spent as if it were a bug in the candidate.


class RepairTrigger(StrEnum):
    """The only two things that may buy a second implementer attempt."""

    BUSINESS_CHECK_FAILED = "business_check_failed"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"


class RepairDecision(StrEnum):
    ALLOWED = "allowed"
    NOT_ENABLED = "not_enabled"
    NOT_A_BUSINESS_FAILURE = "not_a_business_failure"
    NO_FINDINGS = "no_findings"
    ALREADY_REPAIRED = "already_repaired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_REACHED = "deadline_reached"
    STOP_REQUESTED = "stop_requested"
    NO_CONTENT_CHANGE = "no_content_change"
    #: The worktree is not the frozen candidate a repair would start from (it moved, or files
    #: appeared after the freeze), so the repair was refused before anything was bought.
    WORKSPACE_DRIFT = "workspace_drift"


class CandidateIdentity(BaseModel):
    """One frozen candidate, by every identity that matters. Kept per round.

    ``git_commit``/``git_tree`` are real Git objects and ``fingerprint`` is a content hash over
    the declared write scope: three different things, never conflated. ``parent_commit`` is the
    candidate this round started from, which is what makes a repair's provenance readable.
    """

    model_config = Strict

    round: int = 1
    attempt_id: str = ""
    base_commit: str = ""
    parent_commit: str = ""
    git_commit: str = ""
    git_tree: str = ""
    fingerprint: str = ""
    paths: list[str] = Field(default_factory=list)
    is_repair: bool = False
    #: ``True`` when this round produced no content change against its parent: its scoped
    #: fingerprint is the parent's. A new commit SHA is not progress, and neither is a tree change
    #: the fingerprint cannot see - the controller refuses that one as a scope violation.
    unchanged_from_parent: bool = False


class RepairPolicy(BaseModel):
    """The task's explicit opt-in to one repair, and what may trigger it.

    Deliberately not derived from ``BudgetRequest``: a numeric budget field is not a policy, and
    silently repairing because some old task file says ``max_repair_cycles: 1`` would spend money
    on a behaviour nobody approved.
    """

    model_config = Strict

    max_attempts: int = Field(default=1, ge=1, le=1)
    #: Check ids that may trigger a repair when they fail, and the exit codes that mean "the
    #: business assertion this check encodes failed". A check id absent from this map never
    #: triggers one, whatever its exit code.
    check_exit_codes: dict[str, list[int]] = Field(default_factory=dict)
    #: May a substantive ``changes_requested`` verdict on the current candidate buy a repair?
    allow_reviewer_changes: bool = False

    @model_validator(mode="after")
    def _validate_policy(self) -> RepairPolicy:
        if self.max_attempts != 1:
            raise ValueError(
                "this build implements exactly one bounded repair per run; max_attempts must be 1"
            )
        if not self.check_exit_codes and not self.allow_reviewer_changes:
            raise ValueError(
                "a repair policy that lists no trigger can never repair anything: name at least "
                "one check with the exit codes that mean its business assertion failed, or allow "
                "a reviewer's changes_requested"
            )
        for check_id, codes in self.check_exit_codes.items():
            if not check_id.strip():
                raise ValueError("check ids in a repair policy must be non-empty")
            if not codes:
                raise ValueError(
                    f"check {check_id!r} lists no exit codes: name the code(s) that mean its "
                    "business assertion failed, because not every non-zero exit is one"
                )
            if any(code == 0 for code in codes):
                raise ValueError(
                    f"check {check_id!r} lists exit code 0 as a failure; a passing check is not a "
                    "repair trigger"
                )
        return self

    def business_failure_for(self, check_id: str, exit_code: int | None) -> bool:
        """Is this check's outcome a business assertion failure under the policy?"""
        if exit_code is None:
            return False
        return exit_code in self.check_exit_codes.get(check_id, [])

    def digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


class RepairRecord(BaseModel):
    """What the run decided about repairing, and why. Written once, never rewritten.

    A refusal is as much a fact as a repair: "we did not repair because the check that failed is
    an error, not a business assertion" is the answer an operator needs, and a swallowed reason
    would leave the run looking arbitrary.
    """

    model_config = Strict

    decision: RepairDecision
    trigger: RepairTrigger | None = None
    reason: str = ""
    policy_digest: str = ""
    failed_checks: list[str] = Field(default_factory=list)
    exit_codes: dict[str, int | None] = Field(default_factory=dict)
    round: int = 0
    decided_at: str = ""


class RepairContext(BaseModel):
    """The structured input a repair attempt is given. Rendered into the packet, never invented.

    It carries what the *first* attempt cannot know: which candidate it is starting from, and
    exactly what failed about it. It is not a research brief and not a permission grant - the
    write scope, the checks and the configuration are unchanged, and a log excerpt is a reference
    rather than an instruction.
    """

    model_config = Strict

    original_base_commit: str = ""
    original_base_ref: str = ""
    previous: CandidateIdentity | None = None
    trigger: RepairTrigger
    failed_checks: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    remaining_turns: int = 0
    deadline_seconds: int = 0
    detail: str = ""


class BudgetRequest(BaseModel):
    """How many top-level invocations one run may buy.

    ``max_repair_cycles`` keeps its historical default of 1 and **authorizes nothing**: a numeric
    budget field is not a policy, and repairing because an old task file happens to say ``1``
    would spend money on a behaviour nobody approved. A repair needs an explicit
    :class:`RepairPolicy`, and E2's worst case (a repair after a reviewer rejection) is covered by
    ``max_agent_turns`` like every other dispatch.
    """

    model_config = Strict

    max_agent_turns: int = 4
    max_repair_cycles: int = 1


# --------------------------------------------------------------------------
# Root budget (batch E1): the optional ledger a task's revisions share
# --------------------------------------------------------------------------
#
# Why this is a separate contract and not more fields on ``BudgetRequest``: a task revision
# has a budget for *its* run, while a root has a budget for the *task* across revisions. A
# new revision, a new run id or a resubmitted spec must not hand the task a second allowance,
# so the root is named by facts that do not change when any of those do - the project, the
# canonical repository path and the task id - and the ledger that holds the consumption is
# named by its own absolute path, so moving ``--data-dir`` does not silently move the limit.


def root_id_for(*, project_id: str, repo_path: str, task_id: str) -> str:
    """The mechanical root identity: same project/repository/task => same root.

    Derived rather than chosen. A worker that could name its own root id could also declare a
    fresh one and get an unused allowance for the same task, which is exactly what the root
    ledger exists to prevent. A *different* task id is a different root and still needs its own
    approval; nothing here tries to guess whether two task names describe one requirement.
    """
    return "root-" + digest_of(
        {
            "project_id": project_id,
            "repo_path": str(Path(repo_path).resolve()),
            "task_id": task_id,
        }
    ).removeprefix("sha256:")[:32]


class RootBudgetBinding(BaseModel):
    """What a root covers: one task of one project in one repository, in one ledger.

    ``ledger_path`` is the canonical absolute path of the database that holds the
    consumption. It is part of the binding so that an E-mode authorization cannot be spent
    against a different ``--data-dir``: the same task would otherwise look unused again.
    This is a guard against ordinary path mistakes, not a defence against a hand-edited
    database or a copied ledger - those stay inside the trusted-local boundary.
    """

    model_config = Strict

    root_id: str
    project_id: str
    repo_path: str
    task_id: str
    ledger_path: str

    @classmethod
    def derive(
        cls,
        *,
        project_id: str,
        repo_path: str,
        task_id: str,
        ledger_path: str | Path,
    ) -> RootBudgetBinding:
        repo = str(Path(repo_path).resolve())
        return cls(
            root_id=root_id_for(project_id=project_id, repo_path=repo, task_id=task_id),
            project_id=project_id,
            repo_path=repo,
            task_id=task_id,
            ledger_path=str(Path(ledger_path).resolve()),
        )


class RootBudgetLimits(BaseModel):
    """The immutable ceilings of one root. Never re-initialised for a later revision."""

    model_config = Strict

    max_top_level_submissions: int = Field(ge=1, le=64)
    #: How many *additional* implementer attempts the root may buy. The first implementer
    #: attempt of a root is not a repair. The in-run bounded repair (E2) spends one only when the
    #: task carries a ``repair_policy``; without one a run never repairs. Any later implementer
    #: dispatch on the same root - a later revision's first attempt included - is charged here
    #: as well, whatever its policy.
    max_repairs: int = Field(default=0, ge=0, le=8)
    #: Wall-clock ceiling measured from the root's first successful reservation. It refuses a
    #: dispatch (and a repair) past it, caps each invocation's deadline and each check's timeout
    #: by what is left of it - so an in-flight call is stopped at it by its own capped deadline
    #: - and blocks acceptance once it has passed.
    deadline_seconds: int = Field(default=24 * 60 * 60, ge=60)

    def digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


class RootBudgetPlan(BaseModel):
    """The user's root budget file, as written.

    ``hflow prepare`` computes the binding this would produce and does *not* create the
    ledger: a preview that touched SQLite would be a different command. The binding itself is
    minted at ``run``, where the plan is combined with the resolved repository and data dir.
    """

    model_config = Strict

    limits: RootBudgetLimits
    #: Optional note the user may keep in the file. Never an approval: the approval is the
    #: authorization artifact's own ``user_text``, which no preview and no model can write.
    note: str = ""


class WorkspaceSpec(BaseModel):
    """Optional Git-backed isolation for a task (M2).

    When present, the controller creates a detached worktree at ``base_commit`` and does all
    work there, so the user's own checkout is never written to. When absent, the run happens
    in ``project_root`` directly (the M1 behaviour).
    """

    model_config = Strict

    mode: Literal["worktree", "in_place"] = "in_place"
    base_commit: str = ""
    #: Keep the worktree after the run instead of removing it (failures keep it regardless).
    keep: bool = False


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
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    #: Batch E2. Absent means no repair, whatever ``budget.max_repair_cycles`` says. Written as
    #: ``str | None`` rather than a default instance so a spec that predates repairs digests to
    #: exactly the value it always had and cannot be read as having opted in.
    repair_policy: RepairPolicy | None = None

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
        """Idempotency key input: identical spec text => identical digest.

        Batch E2 adds ``repair_policy``, and a digest that changed when the field is absent would
        be a compatibility break with teeth: every recorded run, authorization binding and
        idempotency lookup keys on this value, so an old task file would no longer match the
        ``spec_digest`` its run was recorded with. The key is therefore dropped while the policy is
        absent, exactly as ``AuthorizationBinding.digest`` drops its post-batch fields - a task
        that predates repairs digests to the value it always had, and a task that opts in digests
        to a different one.
        """
        payload = self.model_dump(mode="json")
        if payload.get("repair_policy") is None:
            payload.pop("repair_policy", None)
        return digest_of(payload)

    def required_check_ids(self) -> list[str]:
        seen: list[str] = []
        for criterion in self.acceptance:
            for check_id in criterion.check_ids:
                if check_id not in seen:
                    seen.append(check_id)
        return seen

    def needs_review(self, project: ProjectConfig) -> bool:
        """Is an independent review invocation required for this task?

        The two flags are a floor and a request, not a switch and an override (plan 6):

        * ``project.review_required=True`` is the project's floor. The task has no say: a spec
          that waives it is refused at admission, and if one reaches this method anyway the
          project's requirement still wins.
        * ``project.review_required=False`` means the project does not require a model review.
          The task then decides: a task that asks for one still gets one, because silently
          skipping a review the task asked for would report a delivery the task never claimed.

        So review is skipped in exactly one case - the project does not require it *and* the task
        does not ask for it. That is the combination in which the reviewed artefact is a program
        check plus human reading rather than a model review.
        """
        if project.review_required:
            return True
        return bool(self.review.required)


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


class LaunchConfig(BaseModel):
    """The concrete program launch a role's driver will perform.

    Why this exists: the logical binding ("driver acpx-dsh") does not decide which programs
    actually run. The client entry point, the interpreter that starts it, the launcher path,
    its fixed flags and the DSH home/profile all come from the machine and the environment.
    An approval that covered only the logical name would still be an approval of *something
    else* once one of those changed.

    So they are resolved once, before an approval, and then **consumed** rather than
    re-derived: :meth:`EffectiveConfig.digest` covers them, and the driver is built from this
    object instead of reading the environment a second time.

    Nothing secret belongs here and nothing does - the argv carries only program paths and
    fixed flags (rule 9). Task text, nonces and credentials never reach this structure.
    """

    model_config = Strict

    driver_id: str
    harness: str
    #: Launcher command line: the program that hosts the agent, plus fixed flags only.
    agent_argv: list[str] = Field(default_factory=list)
    #: Interpreter prefix for the client entry point (for example ``[node]`` or ``[python,-u]``).
    client_argv_prefix: list[str] = Field(default_factory=list)
    #: The client entry point itself (the acpx CLI file).
    client_entry: str = ""
    node: str = ""
    python: str = ""
    dsh_executable: str = ""
    #: The DSH profile the launcher starts with, and the DSH home it will use ("" = ambient).
    profile: str = ""
    dsh_home: str = ""
    #: False when a program this launch needs could not be resolved on this machine. Recorded
    #: rather than raised so a preview can report the missing dependency instead of failing
    #: before it has said anything; the launch itself still refuses.
    resolvable: bool = True
    detail: str = ""


class RoleConfig(BaseModel):
    """One role's binding as resolved, including the driver id it will actually construct.

    ``driver`` is the name the profile wrote (possibly an alias); ``driver_id`` is what that
    name resolves to. Recording both is what makes "the config was accepted" checkable
    instead of assumed.
    """

    model_config = Strict

    role: str
    agent: str
    harness: str
    driver: str
    driver_id: str
    model_selection: str = "native_profile"
    capability_record: str = ""
    #: Present for a driver that launches a program; ``None`` for the offline fake, which
    #: starts nothing.
    launch: LaunchConfig | None = None


class EffectiveConfig(BaseModel):
    """The configuration a run will *actually* use, resolved once.

    Why it is a contract and not a debug print: ``prepare`` shows it and ``run`` consumes it,
    so a preview cannot describe a different configuration than the one that executes.
    :meth:`digest` is what an approval binds - changing the profile, a role's model selection
    or the write permission changes the digest, so an earlier approval stops covering the run
    instead of being silently reused.
    """

    model_config = Strict

    #: Where the bindings came from: a machine profile file, or the command line alone.
    source: Literal["machine_profile", "command_line"]
    profile_id: str = ""
    profile_digest: str = ""
    roles: list[RoleConfig] = Field(default_factory=list)
    security_mode: Literal["trusted_local", "restricted", "unknown"] = "unknown"
    limits: ProfileLimits = Field(default_factory=ProfileLimits)
    #: Permission facts, decided from the run's own mode plus an explicit local opt-in and
    #: recorded here so the approval covers them. A reviewer never writes.
    implementer_writes: bool = False
    reviewer_writes: bool = False

    def role(self, name: str) -> RoleConfig | None:
        for entry in self.roles:
            if entry.role == name:
                return entry
        return None

    def driver_ids(self) -> list[str]:
        return [entry.driver_id for entry in self.roles]

    def digest(self) -> str:
        """Identity of this configuration. Stable across processes, JSON-order independent."""
        return digest_of(self.model_dump(mode="json"))


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
    #: The complete, controller-rendered prompt for this role (``packet.py``). When it is
    #: present the driver sends exactly this text - it is the *whole* task input, so a
    #: driver must never add facts of its own or explore the repository to fill gaps. When
    #: it is empty the driver falls back to ``goal``, which keeps direct driver callers and
    #: the offline fixtures working; the controller always renders a packet.
    packet: str = ""
    #: May this invocation change files? Decided by the controller from the *role* and the
    #: run's approved mode - never by the driver's own default, and never by an ambient
    #: switch. A reviewer is read-only even when the implementer was allowed to write.
    writes_allowed: bool = False
    #: Where a driver may keep invocation-scoped scratch (config, raw event logs, session
    #: state). Never the project checkout: scaffolding inside the workspace would show up in
    #: candidate snapshots and dirty the tree under test.
    data_dir: str = ""
    #: Supplied by the controller: ``True`` when the run's stop was already requested. A driver
    #: must call it at the last moment before creating a process, inside whatever coordination it
    #: uses to publish that invocation's handle, so an operator's stop can win the handoff
    #: instead of being discovered after a child exists. It is a read-only question about the
    #: run's recorded state; it starts nothing, sends nothing and blocks on nothing, so calling
    #: it is safe from a spawn path.
    #:
    #: Why this belongs in the request rather than in the driver's own configuration: the stop
    #: fact lives in the controller's store, and a driver cannot see it. Passing the *question*
    #: (not a snapshot answer) is what keeps the decision at the instant of the spawn.
    #:
    #: Excluded from JSON Schema - it is a live callback, not a serializable field - so
    #: ``hflow schema`` still generates. ``InvocationRequest`` is never persisted or sent over
    #: the wire; it is the in-process hand-off to a driver.
    stop_requested: SkipJsonSchema[Callable[[], bool] | None] = Field(default=None, exclude=True)
    #: Supplied by the controller: called by the driver the moment its spawn decision is final,
    #: with what it observed (``created`` and the pid, if any). This is how "a process exists"
    #: reaches the ledger as a *driver-reported fact* instead of the controller assuming that
    #: asking for a launch means one happened. For a driver that does not call it, the controller
    #: reads its result instead: completed work counts as a launch (spawn kind unknown, no
    #: process claimed), a cancelled result with no work as never started, and anything else
    #: stays "a launch was requested and no process is known".
    #:
    #: Excluded from JSON Schema for the same reason as ``stop_requested``: it is a live callback
    #: in the in-process hand-off, not a serializable field of a persisted contract.
    on_spawn: SkipJsonSchema[SpawnReporter | None] = Field(default=None, exclude=True)


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
    #: Digest of the prompt text this invocation was handed, as the transport reports it. This is
    #: a *local* record of the input, not an acknowledgement from the ACP server or the model -
    #: nothing here can observe remote receipt. The controller compares it with the packet it
    #: rendered, so a driver that sends different text than it was given is caught instead of
    #: having its result attributed to this task.
    prompt_digest: str = ""
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
    #: ``True`` when the run had already ended (``BLOCKED``, ``CANCELLED`` or ``ACCEPTED``) once
    #: the stop's intent was durable: the stop changed nothing about the run, so an unconfirmed
    #: answer here is about finished work and does not, by itself, keep the workspace. ``False``
    #: (the default, and what every older receipt reads as) means the stop decided a live run.
    run_already_ended: bool = False


class SpawnKind(StrEnum):
    """What a driver's launch physically creates, as the driver reports it.

    Kept next to the process fact rather than inferred from the driver's name: "this kind of
    launch creates no child process" is a property of the transport, and the ledger has to record
    it to avoid counting an offline run's two completed invocations as two processes on the
    machine.
    """

    #: A real operating-system child was created (the production launch path).
    PROCESS = "process"
    #: The launch does what it does without creating a child: the offline fake driver, which runs
    #: in-process. It is still a launch, and it still settles.
    NO_PROCESS = "no_process"
    #: No driver reported, so the kind of launch is not known.
    UNKNOWN = "unknown"


class SpawnFact(BaseModel):
    """What a driver observed at the moment its launch decision was final.

    Why this is a contract and not an inference: "the controller asked a driver to launch", "the
    launch happened" and "an operating-system child exists" are three different facts, and only
    the driver knows the last two. A stop that wins the gate, a client that fails to start and a
    launcher that exits before any child exists all look alike from the controller's side - and a
    handle with no pid is not proof either way. A driver that runs its work in-process (the
    offline fake) reports ``created=True`` with no pid and ``spawn_kind=no_process``: the launch
    happened, no child appeared, and no process count may claim one.
    """

    model_config = Strict

    invocation_id: str
    #: True when the launch happened - whether or not it created a child process. False covers
    #: every suppressed or failed launch, including a stop that won the handoff inside the gate.
    created: bool
    #: The child's pid when one exists, ``None`` otherwise, as the driver reported it.
    pid: int | None = None
    #: What this driver's kind of launch physically creates. Defaults to ``unknown`` so a driver
    #: that predates this field cannot accidentally claim a process it never reported.
    spawn_kind: SpawnKind = SpawnKind.UNKNOWN
    detail: str = ""


#: A driver's spawn report: called once per invocation, at the moment the spawn decision is
#: final and before ``start``/``start_handle`` returns. It is not called for an invocation whose
#: launch was suppressed before that decision, so "no report" is itself a fact the controller can
#: keep rather than a gap it has to fill with an assumption.
SpawnReporter = Callable[["SpawnFact"], None]


class InvocationStartState(StrEnum):
    """How far one top-level invocation actually got. Facts, never one merged number.

    A reserved slot is not a requested launch, a requested launch is not a created process, and a
    created process is not a provider request. The ledger keeps them apart so a report cannot
    present a reservation count as "calls to the model", and so a launch that produced no process
    is visible as exactly that instead of being rounded up to a start.
    """

    #: The dispatch transaction committed: the allowance is spent and the intent is durable.
    RESERVED = "reserved"
    #: The controller asked a driver to launch this invocation and no process is known to exist
    #: yet: for a live driver this is the window between the request and the spawn fact.
    REQUESTED = "requested"
    #: The launch was carried out: whatever process the driver's kind of launch creates, it
    #: happened. Whether an operating-system child exists is a *separate* recorded fact
    #: (``started_at`` / ``spawn_kind``), because a driver that runs no child - the offline fake -
    #: also reaches this state when it does its work, and counting it as a process would be a
    #: claim about the machine that is false.
    STARTED = "started"
    #: The reservation never reached a launch: a stop won the handoff, or the controller stopped
    #: before asking. The consumption is kept; only the process is not there.
    NOT_STARTED = "not_started"
    #: A result was observed and applied to this invocation.
    SETTLED = "settled"
    #: A launch happened and was never settled. It blocks the root: no automatic refund, no
    #: retry, no re-dispatch.
    UNKNOWN = "unknown"
    #: A launch was *requested* and the controller never learned whether it happened.
    #: Distinct from ``UNKNOWN`` on purpose: no process is known - so it counts as no process -
    #: while the fact that the launch may have run is what keeps the root blocked until an
    #: operator looks.
    LAUNCH_UNKNOWN = "launch_unknown"


class InvocationIntent(BaseModel):
    """One reserved top-level dispatch, as recorded in the ledger.

    This is the E1 record the root, the run and the authorization counters are committed with.
    It deliberately does not copy the attempt or evidence lifecycles: it answers "what did the
    dispatch transaction reserve, under which root, and how far did it get".
    """

    model_config = Strict

    invocation_id: str
    root_id: str
    run_id: str
    attempt_id: str
    role: Literal["implementer", "reviewer", "planner"]
    authorization_id: str = ""
    round: int = 0
    state: InvocationStartState = InvocationStartState.RESERVED
    reserved_at: str
    #: The counters as committed *by this reservation*. Recorded so a later reader can see the
    #: ledger's state at the moment of the dispatch instead of re-deriving it.
    root_used_at_reservation: int = 0
    authorization_used_at_reservation: int = 0
    is_repair: bool = False
    #: When the controller asked a driver to launch this invocation. ``None`` means no driver was
    #: ever asked - the fact that decides whether a later failure may be called "never started".
    launch_requested_at: str | None = None
    #: When the driver reported that its launch happened (``created``). ``None`` means no launch
    #: was reported as having taken place.
    started_at: str | None = None
    #: When an operating-system child existed, as the driver reported its pid. ``None`` means no
    #: child is known - either because this kind of launch creates none, or because nobody said.
    process_started_at: str | None = None
    #: The child's pid, as the driver reported it. Never the controller's own process standing in
    #: for a child: that would be a claim about a process nobody created.
    process_pid: int | None = None
    #: What this driver's launch creates physically. Recorded rather than inferred, so a process
    #: count cannot be derived from a result state.
    spawn_kind: SpawnKind = SpawnKind.UNKNOWN
    settled_at: str | None = None
    outcome: InvocationOutcome | None = None
    detail: str = ""

    @property
    def launch_requested(self) -> bool:
        """Was a driver asked to launch this invocation?"""
        return self.launch_requested_at is not None

    @property
    def launched(self) -> bool:
        """Did the driver report that its launch happened? True for a childless launch too."""
        return self.started_at is not None

    @property
    def process_created(self) -> bool:
        """Did a driver report creating an operating-system child?

        The only fact a process count may use. Deliberately *not* "the invocation reached
        ``started``" and deliberately not "it settled": a completed offline invocation created no
        child, and an unknown result does not create one either.
        """
        return self.process_started_at is not None

    @property
    def pending(self) -> bool:
        """Does this invocation still block its root?

        Everything that is not explicitly finished blocks: a reservation, a requested launch, a
        created process, and both unknown shapes. ``NOT_STARTED`` and ``SETTLED`` are done - the
        former spent an allowance for nothing, which is recorded rather than refunded.
        """
        return self.state not in {
            InvocationStartState.NOT_STARTED,
            InvocationStartState.SETTLED,
        }


class DispatchReservation(BaseModel):
    """What one call to the dispatch transaction returns.

    ``is_new`` is the difference between "this call reserved the dispatch and is the single
    starter" and "this invocation id is already recorded, so the caller may only coordinate".
    A replay therefore cannot launch the driver a second time.
    """

    model_config = Strict

    is_new: bool
    #: The recorded dispatch record. ``None`` on the legacy path, where a run with no root keeps
    #: its dispatch facts on the attempt row (``invocation_id`` / ``review_invocation_id``).
    invocation: InvocationIntent | None = None
    attempt_id: str
    role: str
    #: The run's own counters as this reservation left them, so a caller can report the spend
    #: without another read that a sibling writer could have moved.
    run_turns_reserved: int = 0
    authorization_used: int = 0
    detail: str = ""


class RootBudgetUsage(BaseModel):
    """Read model over one root's ledger row. Counters here are recorded facts, not estimates."""

    model_config = Strict

    binding: RootBudgetBinding
    limits: RootBudgetLimits
    used_top_level_submissions: int = 0
    used_repairs: int = 0
    run_ids: list[str] = Field(default_factory=list)
    authorization_ids: list[str] = Field(default_factory=list)
    first_dispatch_at: str | None = None
    deadline_at: str | None = None

    @property
    def remaining(self) -> int:
        return self.limits.max_top_level_submissions - self.used_top_level_submissions


class InvocationStateCounts(BaseModel):
    """How many top-level dispatches reached each recorded state, and what physically happened.

    Three families of fact, deliberately not derivable from one another:

    * **dispatch states** - ``reserved``, ``requested``, ``started``, ``not_started``, ``settled``,
      ``unknown``, ``launch_unknown``: how far each invocation got in this build's lifecycle;
    * **processes** - ``processes`` counts invocations whose driver reported an operating-system
      child (``process_started_at`` set). An offline invocation that completed in-process is
      ``settled`` with ``spawn_kind = no_process`` and is **not** a process;
    * a **provider request** stays unknown: no invocation row records one.

    ``ever_started`` is kept as the process count under its historical name, so an existing reader
    that asked "how many invocations were started" now gets the honest answer instead of a number
    derived from result states.
    """

    model_config = Strict

    reserved: int = 0
    #: A launch was asked for and nothing has been reported about it yet.
    requested: int = 0
    #: The launch happened (whatever it creates physically).
    started: int = 0
    not_started: int = 0
    settled: int = 0
    unknown: int = 0
    #: A launch was requested and never reported either way. Not counted as a process; it still
    #: blocks the root.
    launch_unknown: int = 0
    #: ``reserved + requested + started``: allowance spent with no observed result yet.
    open: int = 0
    #: Invocations whose driver reported creating an operating-system child.
    processes: int = 0
    #: Invocations whose driver reported doing its work without a child (the offline driver).
    childless_launches: int = 0

    @property
    def total(self) -> int:
        return (
            self.reserved
            + self.requested
            + self.started
            + self.not_started
            + self.settled
            + self.unknown
            + self.launch_unknown
        )

    @property
    def ever_started(self) -> int:
        """Operating-system children a driver reported creating.

        The name is historical and the meaning is now the recorded process fact: counting a
        ``settled`` offline invocation here was a claim about the machine that was simply false.
        """
        return self.processes


class RepairPlanPreview(BaseModel):
    """What a preview can say about this task's repair plan, before any dispatch exists.

    Always present, with ``enabled=False`` for a task that carries no policy: "this task plans no
    repair" is a fact a reader needs, and an absent field would be indistinguishable from a
    preview that forgot to compute it.
    """

    model_config = Strict

    enabled: bool = False
    #: ``RepairPolicy.digest()``, empty when there is no policy.
    policy_digest: str = ""
    check_exit_codes: dict[str, list[int]] = Field(default_factory=dict)
    allow_reviewer_changes: bool = False
    #: The trigger kinds this policy enables, as ``RepairTrigger`` values.
    triggers: list[str] = Field(default_factory=list)
    #: The fixed loop's dispatch count: implementer (+ reviewer).
    single_loop_dispatches: int = 0
    #: The worst case this run must be able to afford: I1 (+R1) + I2 (+R2). A ceiling, not a
    #: quota - an attempt that is never needed is never bought.
    worst_case_dispatches: int = 0
    detail: str = ""


class RootBudgetPreview(BaseModel):
    """What a preview can honestly say about a root, before any dispatch exists.

    ``models_remaining`` is deliberately absent: whether the root still has allowance is decided
    by the run's own dispatch transaction against the ledger, and a preview that promised a
    number would be reading a file another process may already have spent.
    """

    model_config = Strict

    binding: RootBudgetBinding
    limits: RootBudgetLimits
    #: Top-level dispatches one accepted delivery normally needs: implementer + reviewer.
    #: With a repair policy this is the *worst case* instead (I1 + R1 + I2 + R2), because that is
    #: the number the admission gates have to cover; ``single_loop_dispatches`` keeps the normal
    #: figure visible so the two are not confused.
    required_top_level_submissions: int = 0
    #: False unless the task carries a repair policy. A budget field existing is not the same
    #: fact as a repair being armed.
    repair_enabled: bool = False
    #: The fixed loop's own dispatch count, whatever the policy says: implementer (+ reviewer).
    single_loop_dispatches: int = 0
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
    #: The invocation was stopped *before* its process was created, so there is no child and
    #: none will be made. A driver must publish the handle before spawning and check the stop
    #: inside that same coordination, which is what turns "a stop arrived while we were about
    #: to launch" into "nothing was launched" rather than "a process exists that nobody owns".
    start_cancelled: bool = False


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
    """What the controller actually froze. Two identities, never conflated.

    * ``git_commit`` / ``git_tree`` are real Git objects, present when the run used a Git
      worktree (M2).
    * ``fingerprint`` is a content hash over the declared write scope, always present; it is
      what detects a workspace that changed after verification.
    """

    model_config = Strict

    base_commit: str = ""
    git_commit: str = ""
    git_tree: str = ""
    worktree: str = ""
    #: Content fingerprint over the TaskSpec's write scope. Not a Git object id.
    fingerprint: str = ""


class VerificationResult(BaseModel):
    model_config = Strict

    status: Literal["passed", "failed", "not_run"]
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = ""
    #: Where the approved checks ran (a frozen worktree when one was used).
    workspace: str = ""


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
    #: Set when the candidate was frozen in a Git worktree; the paths that are part of it.
    candidate_paths: list[str] = Field(default_factory=list)
    #: Present only when this receipt records a *later* decision about an execution that had
    #: already ended another way - today, an offline reprocessing of recorded evidence after
    #: an adapter failure. The original decision is never overwritten: it stays in the run's
    #: notes and in this record, so "accepted by later offline reprocessing on build X" can
    #: never be read as "the original run succeeded".
    provenance: dict[str, Any] = Field(default_factory=dict)


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
    #: The reviewer turn produced no usable structured verdict (missing, malformed, ambiguous,
    #: or a turn that did not complete). A turn whose outcome is unknown - including one whose
    #: completion is unbound to its prompt - is ``OUTCOME_UNKNOWN`` instead. Distinct from
    #: ``REVIEW_REJECTED`` on purpose: refusing
    #: acceptance because the *wire* failed is not the reviewer's substantive judgment, and
    #: reporting it as one would be a false statement about the review.
    REVIEW_PROTOCOL_ERROR = "review_protocol_error"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DRIVER_FAILED = "driver_failed"
    CANCELLED_BY_OPERATOR = "cancelled_by_operator"
    #: The workspace a client would be launched in carries the client's own project config
    #: (``.acpxrc.json``). acpx always loads it from ``--cwd`` and lets it override HFlow's
    #: launch - including the agent argv - so the launch is refused before any process exists.
    WORKSPACE_CLIENT_CONFIG = "workspace_client_config"
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
    #: The commit a worktree run starts from, already resolved by the caller from
    #: ``task.workspace.base_commit`` (or HEAD). ``hflow run`` resolves it once, before the
    #: authorization is checked, so the approval and the worktree name the same commit even if
    #: the branch moves in between. Empty: the controller resolves the task's ref itself, once,
    #: before it creates the worktree. The task text is never rewritten with it.
    base_commit: str = ""


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


# --------------------------------------------------------------------------
# Zero-model preparation (`hflow prepare`). A preview, never an authorization.
# --------------------------------------------------------------------------


class PlannedCheck(BaseModel):
    """One approved check this task will run, and which acceptance criteria need it."""

    model_config = Strict

    check: CheckDef
    required_by: list[str] = Field(default_factory=list)


class BudgetPlan(BaseModel):
    """What this task's fixed loop reserves, before anything is spent.

    ``required_turns`` is implementer + reviewer, because the two are separate dispatches with
    separate reservations. It is what an authorization has to cover: a task whose review is
    required while only one submission is authorized is refused before the first invocation,
    and this is the number that says so in advance.
    """

    model_config = Strict

    implementer_turns: int = 0
    reviewer_turns: int = 0
    repair_cycles: int = 0
    required_turns: int = 0
    task_turn_budget: int = 0
    project_turn_limit: int = 0
    within_budget: bool = False
    detail: str = ""


class PendingAuthorization(BaseModel):
    """The approval this run would need. Deliberately *not* an authorization artifact.

    It carries the exact binding ``verify_authorization`` will compare, so a user can approve
    a configuration they have actually seen. It carries no ``user_text`` and no
    ``provided_by``: only the user's own approval produces those, and nothing in this build
    mints one from a preview. ``creates_authorization`` is pinned to ``False`` so that
    promise is checkable rather than asserted.
    """

    model_config = Strict

    required: bool = False
    creates_authorization: Literal[False] = False
    mode: str = ""
    driver: str = ""
    binding_digest: str = ""
    binding: dict[str, Any] = Field(default_factory=dict)
    max_top_level_submissions_required: int = 0


class PrepareReport(BaseModel):
    """Everything a task's execution can be known to cost and to require, before dispatch.

    ``model_calls_made`` is pinned to ``0``: a preview that could spend a model request would
    be a different feature. Whether the configuration is *usable* lives in ``effective_config``
    (resolved) and ``admission`` (the same gate ``run`` applies).
    """

    model_config = Strict

    schema_version: int = SCHEMA_VERSION
    task_id: str
    task_revision: int
    spec_digest: str
    spec_path: str
    project_id: str
    project_root: str
    #: Where the work would happen. For a worktree run the run id is chosen at dispatch, so
    #: this is the path template and ``execution_root_is_final`` is False.
    execution_root: str = ""
    execution_root_is_final: bool = False
    workspace_mode: str = "in_place"
    driver_mode: Literal["offline", "live"] = "offline"
    effective_config: EffectiveConfig
    #: ``effective_config.digest()``, carried explicitly so the JSON preview and the run report
    #: can be compared without either side re-deriving it.
    effective_config_digest: str = ""
    admission: ValidationReport
    #: Problems that are knowable before a dispatch from the spec, the contract and this
    #: machine's resolved launch - the same list the run's own dispatch gate uses. They are
    #: kept apart from ``admission`` because they are a different class of fact ("this task is
    #: defined in a way this build cannot honour" versus "this machine cannot run it now"), and
    #: a preview that called one of them "admitted" would be answering the wrong question.
    dispatch_preconditions: list[ValidationIssue] = Field(default_factory=list)
    write_allow: list[str] = Field(default_factory=list)
    write_deny: list[str] = Field(default_factory=list)
    checks: list[PlannedCheck] = Field(default_factory=list)
    budget: BudgetPlan = Field(default_factory=BudgetPlan)
    roles: list[str] = Field(default_factory=list)
    #: Batch E1. The root this run *would* be spent against when a root budget file was given.
    #: ``None`` means no root: a legacy-shaped run, whose allowance is the artifact's alone.
    #: Reported as data - a preview does not mint the binding an approval would cover.
    root_budget: RootBudgetPreview | None = None
    #: Batch E2. Always present: "this task plans no repair" is a fact, not an absent fact.
    repair_plan: RepairPlanPreview = Field(default_factory=RepairPlanPreview)
    #: role -> what that role's input packet will contain, rendered from the same facts the
    #: controller uses. The reviewer's packet embeds the frozen candidate identity, which does
    #: not exist yet, so it is reported as rendered-at-dispatch instead of being invented here.
    packet_preview: dict[str, dict[str, Any]] = Field(default_factory=dict)
    authorization: PendingAuthorization = Field(default_factory=PendingAuthorization)
    model_calls_made: Literal[0] = 0
    notes: list[str] = Field(default_factory=list)


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
    #: Batch E2. *Why* the check ended the way it did, as ``hflow.verify`` writes it: one of
    #: its ``REASON_*`` values (``completed``, ``nonzero_exit``, ``timed_out``,
    #: ``settlement_forced``, ``settlement_unknown``, ``output_capture_error``,
    #: ``not_launched``), or a runner's refusal to start (``empty_argv``, ``no_artifact_dir``).
    #: Only ``completed`` and ``nonzero_exit`` (``CLEAN_EXIT_REASONS``) are answers about the
    #: candidate; an offline runner reports ``not_launched`` unless a test declares otherwise.
    #: Structured on purpose. An automatic repair must not be decided from the
    #: ``verification_failed`` string or a log keyword, and an evidence row that predates this
    #: field (empty) carries no fact to classify, so it never triggers one.
    exit_reason: str = ""
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
    #: Batch E2: is this the repair attempt rather than the first implementation? Recorded on the
    #: attempt itself, so a reader can tell the two rounds apart without counting rows.
    is_repair: bool = False
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
    #: Batch E2. Every repair decision this run recorded, in order, refusals included: "we did
    #: not repair because that check failed for an environmental reason" is a fact an operator
    #: needs, and an absent record would make the run look arbitrary.
    repair_records: list[RepairRecord] = Field(default_factory=list)
    #: The configuration this run actually used, as recorded when the run row was created.
    #: ``None`` for a run that predates config binding: reported as "not recorded" rather
    #: than back-filled from whatever configuration happens to be current now.
    effective_config: EffectiveConfig | None = None
    model_calls_made: int = 0
    #: Batch E1. The root this run was spent against, and the dispatch records that spent it.
    #: Both stay empty for a legacy run: "not recorded" is reported as such, rather than as a
    #: ledger with zero usage that a reader could mistake for a real root.
    root_budget: RootBudgetUsage | None = None
    invocations: list[InvocationIntent] = Field(default_factory=list)
    invocation_counts: InvocationStateCounts = Field(default_factory=InvocationStateCounts)
