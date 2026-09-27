"""Task admission: the deterministic gate in front of every dispatch (plan 11, 16.1).

Everything here is a pure function of the TaskSpec, the project contract, and the
run's already-granted budget. No model is involved, so ``status``/``report`` and
the gate itself cost nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .contracts import (
    LaunchConfig,
    ProjectConfig,
    ReuseStatus,
    RefusalCode,
    RefusedError,
    TaskSpec,
    ValidationIssue,
    ValidationReport,
)
from .paths import ENV_ALLOW_WRITES
from .store import SCHEMA_VERSION as STORE_SCHEMA_VERSION
from .workspace import check_scope

# Ordered from least to most demanding; a task may not lower the project's floor.
_RISK_ORDER = {"low": 0, "standard": 1, "strict": 2}

#: The one delivery level this build implements. Everything else is refused up front rather
#: than silently delivered at this level.
_IMPLEMENTED_DELIVERY = "local_candidate"


def validate_task_spec(
    spec: TaskSpec,
    project: ProjectConfig,
    project_root: Path,
    *,
    allow_fake_checks: bool = True,
) -> ValidationReport:
    """Return every admission problem at once, instead of failing one at a time.

    ``allow_fake_checks`` is the one parameter that depends on *how* the task will be
    executed rather than on the task itself. ``kind=fake`` checks exist for offline work: they
    return a caller-chosen verdict and execute nothing. That is honest for a development run
    and unacceptable for a real delivery, where it would let a receipt claim a program
    verification that never ran. The caller passes ``False`` for a real Harness run.
    """
    issues: list[ValidationIssue] = []
    warnings: list[str] = []

    if spec.schema_version != STORE_SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                code=RefusalCode.INVALID_SPEC,
                detail=(
                    f"schema_version {spec.schema_version} is not supported by this runtime "
                    f"(expected {STORE_SCHEMA_VERSION})"
                ),
                location="schema_version",
            )
        )

    if spec.task_id.strip() != spec.task_id or not spec.task_id.strip():
        issues.append(
            ValidationIssue(
                code=RefusalCode.INVALID_SPEC,
                detail="task_id must be a non-empty identifier without surrounding whitespace",
                location="task_id",
            )
        )

    known_checks = project.check_map()
    for criterion in spec.acceptance:
        for check_id in criterion.check_ids:
            if check_id not in known_checks:
                issues.append(
                    ValidationIssue(
                        code=RefusalCode.UNKNOWN_CHECK,
                        detail=(
                            f"acceptance {criterion.id!r} references check {check_id!r}, "
                            "which is not an approved check in the project contract"
                        ),
                        location=f"acceptance.{criterion.id}",
                    )
                )

    # --- dependencies ------------------------------------------------------
    # ``dependencies`` exists in the contract but nothing in this build schedules a DAG: no
    # ready/claimed query, no invalidation propagation, no serialization. Accepting a
    # non-empty list would start a task whose stated prerequisite was never checked, so it is
    # refused as unimplemented rather than silently ignored (plan 6.7).
    if spec.dependencies:
        issues.append(
            ValidationIssue(
                code=RefusalCode.NOT_IMPLEMENTED,
                detail=(
                    "dependencies="
                    + ", ".join(repr(dep) for dep in spec.dependencies)
                    + " is not implemented: this build runs one task at a time with no "
                    "dependency scheduling, so a prerequisite would be ignored rather than "
                    "enforced. Nothing was dispatched."
                ),
                location="dependencies",
            )
        )

    # --- checks that cannot support a real delivery -----------------------
    # A ``fake`` check returns a verdict chosen by the test harness and executes nothing.
    # It is the right tool offline and a false claim in a real receipt, so this build refuses
    # it here - before a run row, a workspace or an authorization exists - instead of
    # discovering it after paying for an implementation turn.
    if not allow_fake_checks:
        for check_id in spec.required_check_ids():
            check = known_checks.get(check_id)
            if check is not None and check.kind == "fake":
                issues.append(
                    ValidationIssue(
                        code=RefusalCode.NOT_IMPLEMENTED,
                        detail=(
                            f"check {check_id!r} is declared kind='fake', which executes nothing "
                            "and returns a caller-chosen verdict. A real Harness run must be "
                            "verified by approved command checks; nothing was dispatched."
                        ),
                        location=f"checks.{check_id}",
                    )
                )

    for problem in check_scope(spec.scope, project_root, project.write_deny):
        issues.append(
            ValidationIssue(code=RefusalCode.SCOPE_VIOLATION, detail=problem, location="scope")
        )

    # --- reuse admission (plan 11.1/11.3) ---------------------------------
    reuse = spec.reuse
    if reuse.status == ReuseStatus.DECIDED:
        if reuse.choice == "none":
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail="reuse.status=decided requires an explicit choice",
                    location="reuse.choice",
                )
            )
        if not reuse.need.strip():
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail="new-component tasks must state what capability is needed",
                    location="reuse.need",
                )
            )
        # Reusing or adapting a component is only decided once its compatibility question is
        # answered (plan 9.3). A *failed* fit test is a decision to change component or build,
        # never a reason to keep choice=reuse and dispatch anyway - reporting it as a pass
        # would be a false statement about an experiment that already ran.
        if reuse.choice in {"reuse", "adapt"} and reuse.fit_test_status in {"pending", "failed"}:
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail=(
                        f"reuse.choice={reuse.choice} with required_fit_test "
                        f"{reuse.fit_test_status!r} cannot dispatch: the compatibility question "
                        "must be answered by a recorded result before the component is adopted"
                    ),
                    location="reuse.fit_test_status",
                )
            )
        # Claiming no fit test is needed is a claim, so it carries its own short argument
        # (plan 9.3). Without one, "not_required" is indistinguishable from a forgotten test.
        if (
            reuse.choice in {"reuse", "adapt"}
            and reuse.fit_test_status == "not_required"
            and not reuse.reason.strip()
        ):
            issues.append(
                ValidationIssue(
                    code=RefusalCode.REUSE_NOT_APPROVED,
                    detail=(
                        f"reuse.choice={reuse.choice} without a required_fit_test must state, in "
                        "reuse.reason, why the component is already proven to fit this use"
                    ),
                    location="reuse.reason",
                )
            )
    elif reuse.status == ReuseStatus.EXISTING_DECISION and not reuse.reference.strip():
        issues.append(
            ValidationIssue(
                code=RefusalCode.REUSE_NOT_APPROVED,
                detail="existing_decision requires a reference to the recorded decision",
                location="reuse.reference",
            )
        )

    # --- budget authorization --------------------------------------------
    if spec.budget.max_agent_turns > project.limits.max_agent_turns:
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    f"task requests {spec.budget.max_agent_turns} agent turns but the project "
                    f"pre-authorizes at most {project.limits.max_agent_turns}"
                ),
                location="budget.max_agent_turns",
            )
        )
    if spec.budget.max_repair_cycles > project.limits.max_repair_cycles:
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    f"task requests {spec.budget.max_repair_cycles} repair cycles but the project "
                    f"pre-authorizes at most {project.limits.max_repair_cycles}"
                ),
                location="budget.max_repair_cycles",
            )
        )

    # --- risk floor -------------------------------------------------------
    if _RISK_ORDER[spec.risk] < _RISK_ORDER[project.min_risk_for_review]:
        issues.append(
            ValidationIssue(
                code=RefusalCode.RISK_DOWNGRADE,
                detail=(
                    f"task risk {spec.risk!r} is below the project minimum "
                    f"{project.min_risk_for_review!r}"
                ),
                location="risk",
            )
        )
    if project.review_required and not spec.review.required:
        issues.append(
            ValidationIssue(
                code=RefusalCode.RISK_DOWNGRADE,
                detail="this project requires independent review; a task cannot waive it",
                location="review.required",
            )
        )

    # --- delivery level ---------------------------------------------------
    # This build can only deliver a local candidate. Asking for integrated/published used to
    # be a warning, which meant a run "succeeded" while the requested delivery level was never
    # reached - a success exit masking an unmet requirement. It is a refusal now: reaching a
    # lower level than requested needs an explicit operator decision, and there is no
    # auto-downgrade in this build (the run row keeps the requested mode, so a later decision
    # can still be recorded against what was actually asked for).
    if spec.delivery.mode != _IMPLEMENTED_DELIVERY:
        issues.append(
            ValidationIssue(
                code=RefusalCode.NOT_IMPLEMENTED,
                detail=(
                    f"delivery.mode={spec.delivery.mode!r} is not implemented by this build; only "
                    f"{_IMPLEMENTED_DELIVERY!r} is. Nothing was dispatched, so no lower delivery "
                    "level will be presented as the requested one."
                ),
                location="delivery.mode",
            )
        )

    return ValidationReport(
        ok=not issues, issues=issues, spec_digest=spec.spec_digest(), warnings=warnings
    )


def assert_admissible(
    spec: TaskSpec,
    project: ProjectConfig,
    project_root: Path,
    *,
    allow_fake_checks: bool = True,
) -> None:
    """``validate_task_spec`` as a fail-fast refusal, used before creating a run."""
    report = validate_task_spec(
        spec, project, project_root, allow_fake_checks=allow_fake_checks
    )
    if not report.ok:
        first = report.issues[0]
        raise RefusedError(first.code, first.detail)


def repair_policy_problems(
    spec: TaskSpec, project: ProjectConfig
) -> list[ValidationIssue]:
    """Is this task shaped so that the repair policy it carries can be honoured at all?

    Three facts decide that, and each is knowable from the spec and the contract alone - so they
    are refused before a run row, a worktree, an authorization or a dispatch exists, rather than
    half-way through a loop the run cannot finish. The policy itself has already been validated by
    its own contract (``RepairPolicy``); what is checked here is whether the rest of this task is
    shaped to carry one.

    Deliberately *not* gated on ``production``: the controller honours ``spec.repair_policy``
    whatever driver is bound, so an offline repair in place would write into the user's own
    checkout exactly as a live one would. These are facts about the task, not about this machine.
    """
    issues: list[ValidationIssue] = []
    if spec.repair_policy is None:
        return issues

    review_turns = 1 if spec.needs_review(project) else 0
    worst_case = 2 * (1 + review_turns)

    # A repair starts from the previous round's frozen candidate. Only an isolated Git worktree
    # keeps one; an in-place run has no frozen candidate to start from and would have to repair
    # the user's own checkout.
    if spec.workspace.mode != "worktree":
        issues.append(
            ValidationIssue(
                code=RefusalCode.SCOPE_VIOLATION,
                detail=(
                    "this task carries a repair_policy but workspace.mode="
                    f"{spec.workspace.mode!r}. A repair starts from the frozen candidate of "
                    "the previous round, which only an isolated Git worktree keeps: set "
                    "workspace.mode='worktree' with a base commit, or drop repair_policy. "
                    "Nothing was dispatched."
                ),
                location="workspace.mode",
            )
        )
    # A repaired candidate must be verified and independently reviewed again, so a policy without
    # a review has nothing that could accept the repair it buys.
    if review_turns == 0:
        issues.append(
            ValidationIssue(
                code=RefusalCode.NOT_IMPLEMENTED,
                detail=(
                    "this task carries a repair_policy but it will not be reviewed "
                    f"(project.review_required={project.review_required}, task "
                    f"review.required={spec.review.required}). A repaired candidate is "
                    "verified and reviewed again from scratch before it can be accepted, so "
                    "this build cannot support a repair policy without a review: require a "
                    "review (project floor or review.required=true) or drop repair_policy. "
                    "Nothing was dispatched."
                ),
                location="review.required",
            )
        )
    # The run's own ceiling has to cover the whole worst-case loop: I1 (+R1) + I2 (+R2).
    # Discovering a short ceiling after the first attempt means paying for work whose repair and
    # review can never be bought.
    if spec.budget.max_agent_turns < worst_case:
        loop = "I1 + R1 + I2 + R2" if review_turns else "I1 + I2"
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    f"this task carries a repair_policy but budget.max_agent_turns="
                    f"{spec.budget.max_agent_turns} does not cover the worst-case repair loop "
                    f"of {worst_case} top-level dispatch(es) ({loop}): the repair attempt and "
                    "the review that must follow it are dispatches the first attempt cannot "
                    f"know about. Raise budget.max_agent_turns to at least {worst_case}, or "
                    "drop repair_policy. Nothing was dispatched."
                ),
                location="budget.max_agent_turns",
            )
        )
    return issues


def predictable_dispatch_problems(
    spec: TaskSpec,
    project: ProjectConfig,
    *,
    production: bool,
    implementer_writes: bool,
    launches: Sequence[LaunchConfig] = (),
) -> list[ValidationIssue]:
    """Problems knowable before a dispatch, from the task, the contract and this machine.

    These are a different class of fact from ``validate_task_spec``'s: that one asks "is this
    task defined in a way this build can honour?", this one asks "can this machine run it
    *now*?". Both are refusals before anything is spent, so both belong in a preview - a
    ``prepare`` that answered "admitted" for a task every run would refuse was answering the
    wrong question.

    Shared deliberately, and every input already resolved: the write permission comes from
    ``prepare.resolve_permissions`` and the launches from the effective configuration, so
    ``prepare`` and the controller's dispatch gate cannot drift apart. There is no environment
    read here at all.

    What is *not* here is stated rather than implied: this cannot see whether the target
    repository is reachable (the worktree step does), whether the launcher actually works (the
    zero-model preflight does), whether the model will succeed, or how much authorization
    allowance is left - that depends on this run's history, and the controller checks it
    separately.

    The repair-policy rules are reported for every run, offline included, because they are facts
    about the task rather than about the machine; the rules below them are machine facts and are
    reported only for a real delivery.
    """
    issues: list[ValidationIssue] = list(repair_policy_problems(spec, project))

    if not production:
        # An offline run is not a delivery: the fake driver scripts its own change and its
        # checks are fake by construction, so the machine rules would refuse every offline run.
        return issues

    # 1. A run that must change files needs a write permission that is actually on and a
    #    workspace that is not the user's own checkout. Writes off plus a non-empty write scope
    #    is a known-bad combination: the implementer cannot edit, so the run would spend a turn
    #    and then fail verification.
    if spec.scope.write_allow:
        if spec.workspace.mode != "worktree":
            issues.append(
                ValidationIssue(
                    code=RefusalCode.SCOPE_VIOLATION,
                    detail=(
                        "this task declares write paths but workspace.mode="
                        f"{spec.workspace.mode!r}. A real change must run in an isolated Git "
                        "worktree (workspace.mode='worktree' with a base commit); an in-place "
                        "run would write into the user's own checkout"
                    ),
                    location="workspace.mode",
                )
            )
        if not implementer_writes:
            issues.append(
                ValidationIssue(
                    code=RefusalCode.SCOPE_VIOLATION,
                    detail=(
                        f"this task declares write paths but {ENV_ALLOW_WRITES} is not enabled, so "
                        "the invocation would be launched read-only and could not make the "
                        "change. Enable writes for this run (see docs/operations.md) or submit a "
                        "task that changes nothing"
                    ),
                    location="scope.write_allow",
                )
            )

    # 2. The budget must cover the whole fixed loop. A review is its own top-level invocation,
    #    so a task that needs one needs two reserved turns; discovering that after the
    #    implementation turn means paying for work that can never be accepted. The matching
    #    check against the authorization's *remaining* allowance is separate, because it
    #    depends on this run's history rather than on the spec.
    if spec.needs_review(project) and spec.budget.max_agent_turns < 2:
        issues.append(
            ValidationIssue(
                code=RefusalCode.BUDGET_EXCEEDED,
                detail=(
                    "review is required (project floor or task request) but "
                    f"budget.max_agent_turns={spec.budget.max_agent_turns} covers only the "
                    "implementation turn. A reviewed delivery needs at least 2"
                ),
                location="budget.max_agent_turns",
            )
        )

    # 3. Every role's launch must have resolved to real programs. This is recorded on the
    #    launch config rather than raised when it is resolved, so a preview can name the missing
    #    dependency instead of dying before it prints anything. Two roles sharing one resolved
    #    launch produce one problem, not two copies of it.
    seen_launches: set[tuple[str, str]] = set()
    for launch in launches:
        if launch.resolvable:
            continue
        key = (launch.driver_id, launch.detail)
        if key in seen_launches:
            continue
        seen_launches.add(key)
        issues.append(
            ValidationIssue(
                code=RefusalCode.NOT_IMPLEMENTED,
                detail=(
                    f"the launch for driver {launch.driver_id!r} could not be resolved, so no "
                    f"process could be started: {launch.detail or 'no detail'}"
                ),
                location="effective_config.launch",
            )
        )

    return issues
