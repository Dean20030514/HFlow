"""The one resolution path shared by ``hflow prepare`` and ``hflow run`` (plan 9.1, 9.2).

Why this module exists: ``prepare`` prints what a run *would* do and ``run`` does it. If the
two computed that independently, a preview could describe one configuration and the run could
execute another - the exact failure this batch removes. So the task file, the project
contract, the machine profile, the role bindings, the driver names, the permission facts and
the admission report are all derived here, once, and both commands consume the same
:class:`ResolvedRun`.

Nothing here calls a model, creates a run row, writes a file or builds a process. The only
side effect available is a refusal.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .admission import predictable_dispatch_problems, validate_task_spec
from .contracts import (
    AgentBinding,
    BudgetPlan,
    CheckDef,
    EffectiveConfig,
    LaunchConfig,
    MachineProfile,
    PendingAuthorization,
    PlannedCheck,
    PrepareReport,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    RoleConfig,
    RunRequest,
    TaskSpec,
    ValidationIssue,
    ValidationReport,
)
#: The one harness this build implements, taken from the driver mapping rather than restated:
#: the command-line path selects a driver without naming a harness, and a second spelling of
#: this string is how a declaration and a launch drift apart.
from .drivers.selected import FAKE_DRIVER_ID, HARNESS_DSH
from .paths import ENV_ALLOW_WRITES
from .packet import PacketTooLargeError
from .profiles import (
    RUN_ROLES,
    load_profile,
    profile_digest,
    requested_profile_id,
    resolve_role_bindings,
)

#: The write opt-in is a local, explicit decision; anything else is "no".
_WRITE_OPT_IN = {"1", "true", "yes"}


@dataclass(frozen=True)
class MachineBindings:
    """The machine half of a run: which agent and driver each role uses.

    Resolved without reading the task or the project, because one caller needs exactly that:
    ``hflow run`` settles the authorization question before the task file is opened, so a
    missing approval cannot be reported as a malformed task.
    """

    profile: MachineProfile | None
    profile_id: str
    bindings: dict[str, tuple[str, AgentBinding]]

    @property
    def is_real_driver(self) -> bool:
        from .drivers.selected import resolve_driver_id

        return any(
            resolve_driver_id(binding) != FAKE_DRIVER_ID
            for _, binding in self.bindings.values()
        )


@dataclass(frozen=True)
class ResolvedRun:
    """Everything both commands derive from the same inputs.

    ``bindings`` is per role, so the implementer and the reviewer can use different agents,
    models and even transports. ``profile`` is ``None`` only on the command-line path, where
    the user named a driver directly.
    """

    spec: TaskSpec
    project: ProjectConfig
    project_root: Path
    spec_path: Path
    data_dir: Path
    profile: MachineProfile | None
    bindings: dict[str, tuple[str, AgentBinding]]
    effective: EffectiveConfig
    admission: ValidationReport
    #: The same pre-dispatch problems the run's own gate raises, resolved from this machine.
    dispatch_preconditions: list[ValidationIssue] = field(default_factory=list)

    @property
    def ready_to_dispatch(self) -> bool:
        """Would `run` get past admission and past its dispatch gate?

        Authorization allowance is not part of this: it depends on a run's history, so it is
        checked when a run actually exists rather than promised here.
        """
        return self.admission.ok and not self.dispatch_preconditions

    @property
    def is_real_driver(self) -> bool:
        """Is any role dispatched to something other than the offline fake?"""
        return any(
            entry.driver_id != "fake" for entry in self.effective.roles
        )

    @property
    def roles(self) -> list[str]:
        """The roles this task will actually dispatch, in dispatch order."""
        required = ["implementer"]
        if self.spec.needs_review(self.project):
            required.append("reviewer")
        return required

    def request(self) -> RunRequest:
        return RunRequest(
            task=self.spec,
            project=self.project,
            project_root=self.project_root,
            workspace_root=self.project_root,
        )

    def execution_root(self) -> tuple[str, bool]:
        """(path, is_final) where the work would happen.

        An in-place run works in the project root, which is known now. A worktree run's path
        contains the run id, which is chosen at dispatch, so the template is reported with
        ``is_final=False`` instead of pretending to know it.
        """
        if self.spec.workspace.mode != "worktree":
            return str(self.project_root), True
        return str(self.project_root.parent / f"{self.project_root.name}.hflow-worktrees" / "<run-id>"), False


# --------------------------------------------------------------------------
# loading the inputs
# --------------------------------------------------------------------------


def load_json_file(path: Path, *, what: str) -> object:
    """Read one JSON document, refusing rather than defaulting."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefusedError(RefusalCode.INVALID_SPEC, f"{what} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC, f"{what} {path} is not valid JSON: {exc}"
        ) from exc


def effective_spec(
    spec: TaskSpec,
    *,
    base_commit: str | None = None,
    workspace_mode: str | None = None,
) -> TaskSpec:
    """Apply command-line overrides *before* admission.

    The overrides become part of the effective TaskSpec, so the stored spec, its digest, the
    rendered packet and the authorization binding all describe the same task. This used to
    live in ``cmd_run``; ``prepare`` needs the identical rule or its preview would describe a
    different spec than the run it previews.
    """
    if not base_commit and not workspace_mode:
        return spec
    mode = workspace_mode or spec.workspace.mode
    overrides: dict[str, Any] = {
        "workspace": {
            "mode": mode,
            "base_commit": base_commit or spec.workspace.base_commit,
            "keep": True if mode == "worktree" else spec.workspace.keep,
        }
    }
    return TaskSpec.model_validate({**spec.model_dump(mode="json"), **overrides})


def resolve_permissions(
    spec: TaskSpec, env: Mapping[str, str] | None = None
) -> tuple[bool, bool]:
    """(implementer_writes, reviewer_writes), decided in one place.

    A run that must change files needs both an explicit local opt-in and an isolated worktree:
    without a worktree the change would land in the user's own checkout. A reviewer never
    writes, because it checks what the implementer produced. Both facts are part of the
    effective configuration, so an approval covers them.
    """
    source = env if env is not None else os.environ
    opted_in = source.get(ENV_ALLOW_WRITES, "").strip().lower() in _WRITE_OPT_IN
    return (opted_in and spec.workspace.mode == "worktree"), False


def _role_config(
    role: str,
    agent_id: str,
    binding: AgentBinding,
    *,
    data_dir: Path,
    env: Mapping[str, str] | None,
) -> RoleConfig:
    """One role's configuration, including the *resolved launch* for a driver that starts one.

    The launch is resolved here and travels with the configuration, so it is covered by
    ``EffectiveConfig.digest()`` - what an approval binds - and consumed by ``build_driver``
    afterwards rather than re-derived from the environment. That is the difference between
    approving a configuration and approving a name.
    """
    from .drivers.acpx_dsh import resolve_launch_config
    from .drivers.selected import resolve_driver_id

    driver_id = resolve_driver_id(binding)
    launch: LaunchConfig | None = None
    if driver_id != FAKE_DRIVER_ID:
        # The launcher profile a real transport starts with. It is the driver's own fixed
        # default; a profile that wants another one will need the binding to carry it, which
        # this build does not claim to support (see README, "Not implemented").
        launch = resolve_launch_config(data_dir=data_dir, env=env)
    return RoleConfig(
        role=role,
        agent=agent_id,
        harness=binding.harness,
        driver=binding.driver,
        driver_id=driver_id,
        model_selection=binding.model_selection,
        capability_record=binding.capability_record,
        launch=launch,
    )


def _effective_from_profile(
    profile: MachineProfile,
    resolved: dict[str, tuple[str, AgentBinding]],
    *,
    data_dir: Path,
    env: Mapping[str, str] | None,
    implementer_writes: bool,
    reviewer_writes: bool,
) -> EffectiveConfig:
    return EffectiveConfig(
        source="machine_profile",
        profile_id=profile.profile_id,
        profile_digest=profile_digest(profile),
        roles=[
            _role_config(role, agent_id, binding, data_dir=data_dir, env=env)
            for role, (agent_id, binding) in resolved.items()
        ],
        security_mode=profile.security_mode,
        limits=profile.limits,
        implementer_writes=implementer_writes,
        reviewer_writes=reviewer_writes,
    )


def _effective_from_command_line(
    binding: AgentBinding,
    *,
    data_dir: Path,
    env: Mapping[str, str] | None,
    implementer_writes: bool,
    reviewer_writes: bool,
) -> EffectiveConfig:
    return EffectiveConfig(
        source="command_line",
        roles=[
            _role_config(role, "command-line", binding, data_dir=data_dir, env=env)
            for role in RUN_ROLES
        ],
        security_mode="unknown",
        implementer_writes=implementer_writes,
        reviewer_writes=reviewer_writes,
    )


def resolved_launches(effective: EffectiveConfig) -> list[LaunchConfig]:
    """The launches an effective configuration would perform."""
    return [entry.launch for entry in effective.roles if entry.launch is not None]


def resolve_machine_bindings(
    *,
    data_dir: Path,
    profile_id: str | None = None,
    driver: str | None = None,
    env: Mapping[str, str] | None = None,
) -> MachineBindings:
    """Resolve the machine half: profile, role bindings, driver names.

    The precedence is defined here and nowhere else:

    1. ``--profile`` (else ``HFLOW_PROFILE``) selects a machine profile; its role bindings are
       used for both roles.
    2. ``--driver`` is the command-line alternative. Passed *together with* a profile it must
       resolve to the same driver id as every role the profile binds, otherwise the two
       sources disagree and the run is refused rather than silently preferring one.
    3. With neither, the offline fake driver is used: the historical default.

    A profile is never partially used: an unknown id, an unreadable file, an unbound role or
    an unimplemented driver name refuses the whole resolution.
    """
    selected_profile = requested_profile_id(profile_id, env)
    if not selected_profile:
        # The command line names a driver, not a harness. This build implements exactly one, so
        # the binding it builds says so rather than leaving the field empty for a later reader
        # to interpret.
        binding = AgentBinding(harness=HARNESS_DSH, driver=driver or "fake")
        return MachineBindings(
            profile=None,
            profile_id="",
            bindings={role: ("command-line", binding) for role in RUN_ROLES},
        )

    profile = load_profile(data_dir, selected_profile)
    bindings = resolve_role_bindings(profile)
    from .drivers.selected import driver_id_for_name, resolve_driver_id

    ids = {role: resolve_driver_id(binding) for role, (_, binding) in bindings.items()}
    if len({id_ == "fake" for id_ in ids.values()}) > 1:
        # An offline stand-in for one role and a real transport for the other is not a
        # configuration anyone can reason about: half the run would be a script and half a
        # model, and the receipt could not say which half produced the candidate.
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"profile {selected_profile!r} mixes the offline fake driver with a real transport "
            f"({', '.join(f'{role}={id_}' for role, id_ in sorted(ids.items()))}). Bind every "
            "role to the same kind of driver.",
        )
    if driver:
        # Compared by driver id, so an alias (``--driver acpx``) matches the profile's name.
        # The harness is the profile's to declare; the flag only names a transport here.
        requested = driver_id_for_name(driver)
        disagreeing = [role for role, id_ in ids.items() if id_ != requested]
        if disagreeing:
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                f"--driver {driver!r} and profile {selected_profile!r} disagree for role(s) "
                f"{', '.join(sorted(disagreeing))}. The profile is the source of the "
                "bindings; pass --driver only when it names the same driver, or drop "
                "--profile to use the command line alone.",
            )
    return MachineBindings(profile=profile, profile_id=selected_profile, bindings=bindings)


def resolve_run(
    *,
    task_path: Path,
    project_root: Path,
    data_dir: Path,
    project_path: Path | None = None,
    profile_id: str | None = None,
    driver: str | None = None,
    base_commit: str | None = None,
    workspace_mode: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedRun:
    """Resolve one task, project and machine configuration into what would be executed."""
    root = Path(project_root).resolve()
    spec_path = Path(task_path)
    spec = TaskSpec.model_validate(load_json_file(spec_path, what="task spec"))
    spec = effective_spec(spec, base_commit=base_commit, workspace_mode=workspace_mode)

    resolved_project_path = (
        Path(project_path) if project_path else root / ".hflow" / "project.json"
    )
    project = ProjectConfig.model_validate(
        load_json_file(resolved_project_path, what="project contract")
    )

    machine = resolve_machine_bindings(
        data_dir=data_dir, profile_id=profile_id, driver=driver, env=env
    )
    implementer_writes, reviewer_writes = resolve_permissions(spec, env)
    if machine.profile is not None:
        effective = _effective_from_profile(
            machine.profile,
            machine.bindings,
            data_dir=Path(data_dir),
            env=env,
            implementer_writes=implementer_writes,
            reviewer_writes=reviewer_writes,
        )
    else:
        _, binding = machine.bindings["implementer"]
        effective = _effective_from_command_line(
            binding,
            data_dir=Path(data_dir),
            env=env,
            implementer_writes=implementer_writes,
            reviewer_writes=reviewer_writes,
        )

    is_real = any(entry.driver_id != FAKE_DRIVER_ID for entry in effective.roles)
    admission = validate_task_spec(spec, project, root, allow_fake_checks=not is_real)
    preconditions = predictable_dispatch_problems(
        spec,
        project,
        production=is_real,
        implementer_writes=implementer_writes,
        launches=resolved_launches(effective),
    )
    return ResolvedRun(
        spec=spec,
        project=project,
        project_root=root,
        spec_path=spec_path,
        data_dir=Path(data_dir),
        profile=machine.profile,
        bindings=machine.bindings,
        effective=effective,
        admission=admission,
        dispatch_preconditions=preconditions,
    )


# --------------------------------------------------------------------------
# drivers, per role
# --------------------------------------------------------------------------


def role_drivers(
    resolved: ResolvedRun,
    *,
    factory: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Build one driver per *distinct* role binding, from the configuration already resolved.

    Two roles bound to the same agent share one driver instance - they are the same
    configuration, and probing it twice would be two launches of the same client. Two roles
    bound to different agents get different instances, which is the point of per-role
    bindings: the reviewer really can be another model or another transport.

    The launch each driver uses is the one from the effective configuration, not a freshly
    resolved one: re-reading the environment here would mean the process could differ from the
    one an approval covered.
    """
    if factory is None:
        from .drivers import selected as selected_drivers

        factory = selected_drivers.build_driver

    built: dict[str, object] = {}
    by_identity: dict[str, object] = {}
    for role in RUN_ROLES:
        _, binding = resolved.bindings[role]
        key = json.dumps(binding.model_dump(mode="json"), sort_keys=True)
        if key not in by_identity:
            entry = resolved.effective.role(role)
            by_identity[key] = factory(
                binding,
                data_dir=resolved.data_dir,
                launch=entry.launch if entry is not None else None,
            )
        built[role] = by_identity[key]
    return built


# --------------------------------------------------------------------------
# the preview
# --------------------------------------------------------------------------


def _planned_checks(spec: TaskSpec, project: ProjectConfig) -> list[PlannedCheck]:
    known = project.check_map()
    planned: list[PlannedCheck] = []
    for check_id in spec.required_check_ids():
        check: CheckDef | None = known.get(check_id)
        if check is None:
            continue  # admission already reported the unknown check; do not invent it here
        required_by = [
            criterion.id for criterion in spec.acceptance if check_id in criterion.check_ids
        ]
        planned.append(PlannedCheck(check=check, required_by=required_by))
    return planned


def _budget_plan(resolved: ResolvedRun, roles: list[str]) -> BudgetPlan:
    spec, project = resolved.spec, resolved.project
    implementer = 1
    reviewer = 1 if "reviewer" in roles else 0
    required = implementer + reviewer
    ceiling = min(spec.budget.max_agent_turns, project.limits.max_agent_turns)
    within = required <= ceiling
    detail = (
        f"implementer {implementer} + reviewer {reviewer} = {required} reserved turn(s); "
        f"task budget {spec.budget.max_agent_turns}, project pre-authorization "
        f"{project.limits.max_agent_turns}"
    )
    if not within:
        detail += (
            f". This task cannot run: it needs {required} turn(s) but only {ceiling} is "
            "available, and a review that a project requires cannot be waived by a task"
        )
    return BudgetPlan(
        implementer_turns=implementer,
        reviewer_turns=reviewer,
        repair_cycles=0,
        required_turns=required,
        task_turn_budget=spec.budget.max_agent_turns,
        project_turn_limit=project.limits.max_agent_turns,
        within_budget=within,
        detail=detail,
    )


def _implementer_packet_preview(resolved: ResolvedRun, workspace: str) -> dict[str, Any]:
    """The packet the implementer will be handed, rendered from the same facts the run uses."""
    from .packet import render_implementer_packet

    try:
        rendered = render_implementer_packet(
            task_id=resolved.spec.task_id,
            task_revision=resolved.spec.revision,
            goal=resolved.spec.goal,
            acceptance=resolved.spec.acceptance,
            scope=resolved.spec.scope,
            workspace=workspace,
            spec_digest=resolved.spec.spec_digest(),
            deadline_seconds=resolved.request().deadline_seconds,
            writes_allowed=resolved.effective.implementer_writes,
        )
    except PacketTooLargeError as exc:
        return {
            "rendered": False,
            "reason": f"the packet does not fit its bound: {exc}",
            "fits": False,
        }
    return {
        "rendered": True,
        "fits": True,
        "workspace": workspace,
        "bytes": rendered.byte_length,
        "digest": rendered.digest,
        "preview": rendered.text,
    }


def _reviewer_packet_preview(resolved: ResolvedRun) -> dict[str, Any]:
    """The reviewer's packet is *not* rendered here, and saying why is more useful than a guess.

    It embeds the frozen candidate identity, the verification status and the evidence rows.
    None of those exist before the implementer has run, so a preview rendered now would be an
    invented input - worse than no preview.
    """
    return {
        "rendered": False,
        "reason": (
            "the reviewer packet embeds the frozen candidate identity, the verification "
            "status and the evidence rows; none exists before the implementer runs, so it is "
            "rendered at review time from recorded facts rather than guessed here"
        ),
        "carries": [
            "goal and acceptance criteria",
            "write scope",
            "task revision and spec digest",
            "the frozen candidate fingerprint and Git identity",
            "approved check commands and their evidence",
            "the canonical ReviewOutput contract",
        ],
    }


def build_prepare_report(
    resolved: ResolvedRun,
    *,
    authorization_mode: str = "m2-live-change",
    max_top_level_submissions_required: int | None = None,
    env: Mapping[str, str] | None = None,
) -> PrepareReport:
    """Assemble the preview. Zero model calls, zero writes, zero run state."""
    from .authorization import current_binding

    roles = resolved.roles
    execution_root, execution_root_is_final = resolved.execution_root()
    budget = _budget_plan(resolved, roles)
    submissions = (
        max_top_level_submissions_required
        if max_top_level_submissions_required is not None
        else budget.required_turns
    )
    pending = PendingAuthorization(required=resolved.is_real_driver)
    if resolved.is_real_driver:
        implementer = resolved.effective.role("implementer")
        assert implementer is not None
        binding = current_binding(
            mode=authorization_mode,  # type: ignore[arg-type]
            driver=implementer.driver,
            project=resolved.project,
            request=resolved.request(),
            spec_path=resolved.spec_path,
            effective=resolved.effective,
        )
        pending = PendingAuthorization(
            required=True,
            mode=binding.mode,
            driver=binding.driver,
            binding_digest=binding.digest(),
            binding=binding.model_dump(mode="json"),
            max_top_level_submissions_required=submissions,
        )

    preview: dict[str, dict[str, Any]] = {}
    if "implementer" in roles:
        preview["implementer"] = _implementer_packet_preview(resolved, execution_root)
    if "reviewer" in roles:
        preview["reviewer"] = _reviewer_packet_preview(resolved)

    notes = [
        "prepare made no model call, created no run row, no workspace and no authorization",
        "the admission result above is the same gate `hflow run` applies to this task",
        "the dispatch preconditions above are the same list the run's own gate raises; "
        "remaining authorization allowance is not checked here, because it depends on a run's "
        "history",
    ]
    if not execution_root_is_final:
        notes.append(
            "the worktree path contains the run id, which is chosen at dispatch: the packet "
            "size and digest above are for the template path and shift with the id's length"
        )
    if resolved.is_real_driver:
        notes.append(
            "a live run needs an authorization artifact the user writes from their own "
            "approval; prepare cannot produce one and the binding above is only what the "
            "approval would have to cover"
        )
        notes.append(
            "the profile selects which agent, transport and write permission each role uses; "
            "the model behind a DSH launch is still chosen by the DSH profile the launcher "
            "starts with. `model_selection` is recorded and reported but is not passed as a "
            "launcher flag yet - `hflow doctor --profile <id>` shows the exact argv"
        )
    if not budget.within_budget:
        notes.append(
            "this task does not fit its budget: it needs more reserved turns than are "
            "available, so a run refuses before dispatching anything"
        )
    for issue in resolved.dispatch_preconditions:
        notes.append(f"dispatch precondition: {issue.code.value}: {issue.detail}")

    return PrepareReport(
        task_id=resolved.spec.task_id,
        task_revision=resolved.spec.revision,
        spec_digest=resolved.spec.spec_digest(),
        spec_path=str(resolved.spec_path),
        project_id=resolved.project.project_id,
        project_root=str(resolved.project_root),
        execution_root=execution_root,
        execution_root_is_final=execution_root_is_final,
        workspace_mode=resolved.spec.workspace.mode,
        driver_mode="live" if resolved.is_real_driver else "offline",
        effective_config=resolved.effective,
        effective_config_digest=resolved.effective.digest(),
        admission=resolved.admission,
        dispatch_preconditions=list(resolved.dispatch_preconditions),
        write_allow=list(resolved.spec.scope.write_allow),
        write_deny=list(resolved.spec.scope.write_deny),
        checks=_planned_checks(resolved.spec, resolved.project),
        budget=budget,
        roles=roles,
        packet_preview=preview,
        authorization=pending,
        notes=notes,
    )


def render_prepare_text(report: PrepareReport) -> str:
    """Human-readable preview. Same facts as the JSON form, no derived estimates."""
    effective = report.effective_config
    lines = [
        f"task          {report.task_id} revision {report.task_revision}",
        f"spec_digest   {report.spec_digest}",
        f"spec_path     {report.spec_path}",
        f"project       {report.project_id} at {report.project_root}",
        f"mode          {report.driver_mode} ({report.workspace_mode} workspace)",
        f"workspace     {report.execution_root}"
        + ("" if report.execution_root_is_final else "  (template: the run id is chosen at dispatch)"),
        "configuration",
        f"  source      {effective.source}"
        + (f" profile={effective.profile_id}" if effective.profile_id else " (no profile selected)"),
    ]
    if effective.profile_digest:
        lines.append(f"  profile_id  {effective.profile_id} digest={effective.profile_digest}")
    for entry in effective.roles:
        lines.append(
            f"  {entry.role:<12} agent={entry.agent} driver={entry.driver}"
            f" -> {entry.driver_id}"
            + (f" model_selection={entry.model_selection}" if entry.model_selection else "")
        )
        launch = entry.launch
        if launch is not None:
            # The programs an approval would cover. Not "the driver name" - the actual launch.
            launch_state = "resolved" if launch.resolvable else "NOT RESOLVABLE"
            lines.append(
                f"               launch [{launch_state}] {launch.client_entry or '(no client)'}"
            )
            lines.append(f"               start  {' '.join(launch.client_argv_prefix)}")
            lines.append(f"               launcher {' '.join(launch.agent_argv)}")
            if launch.dsh_home:
                lines.append(f"               DSH_HOME {launch.dsh_home}")
            if not launch.resolvable:
                lines.append(f"               reason {launch.detail}")
    lines.append(
        f"  writes      implementer={effective.implementer_writes} "
        f"reviewer={effective.reviewer_writes}"
    )
    lines.append(f"  config_hash {effective.digest()}")
    lines.append(f"roles         {', '.join(report.roles)}")
    lines.append("write scope")
    lines.append(f"  allow       {', '.join(report.write_allow) or '(none)'}")
    lines.append(f"  deny        {', '.join(report.write_deny) or '(none)'}")
    lines.append("checks")
    if not report.checks:
        lines.append("  (none required by this task's acceptance criteria)")
    for planned in report.checks:
        check = planned.check
        target = " ".join(check.argv) if check.argv else "(no command: fake check)"
        lines.append(
            f"  {check.id:<12} kind={check.kind} timeout={check.timeout_seconds}s "
            f"required_by={', '.join(planned.required_by)}"
        )
        lines.append(f"               {target}")
    lines.append("budget")
    lines.append(
        f"  turns       require {report.budget.required_turns} "
        f"(implementer {report.budget.implementer_turns}, reviewer {report.budget.reviewer_turns}), "
        f"task budget {report.budget.task_turn_budget}, "
        f"project pre-authorization {report.budget.project_turn_limit}"
    )
    lines.append(f"  fits        {report.budget.within_budget}")
    lines.append("admission")
    if report.admission.ok:
        lines.append("  ok          no admission problem found")
    else:
        for issue in report.admission.issues:
            lines.append(f"  refuse      {issue.code.value} at {issue.location}: {issue.detail}")
    lines.append("dispatch gate")
    if not report.dispatch_preconditions:
        lines.append("  ok          nothing knowable now would stop a dispatch")
    else:
        for issue in report.dispatch_preconditions:
            lines.append(f"  refuse      {issue.code.value} at {issue.location}: {issue.detail}")
        lines.append(
            "  note        a real run would refuse before dispatching; remaining authorization "
            "allowance is checked only once a run exists"
        )
    lines.append("input packet")
    for role, entry in report.packet_preview.items():
        if entry.get("rendered"):
            lines.append(
                f"  {role:<12} {entry['bytes']} bytes digest={entry['digest']}"
            )
        else:
            lines.append(f"  {role:<12} rendered at dispatch: {entry.get('reason', '')}")
    lines.append("authorization")
    if not report.authorization.required:
        lines.append("  required    no (offline driver: the fake driver reaches no model)")
    else:
        lines.append(
            f"  required    yes, up to {report.authorization.max_top_level_submissions_required} "
            f"top-level submission(s), mode={report.authorization.mode}"
        )
        lines.append(f"  binding     {report.authorization.binding_digest}")
        lines.append(
            "  pending     prepare did not create an authorization; only your own approval "
            "produces one"
        )
    for note in report.notes:
        lines.append(f"note          {note}")
    return "\n".join(lines)
