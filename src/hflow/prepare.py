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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .admission import predictable_dispatch_problems, validate_task_spec
from .contracts import (
    AgentBinding,
    BudgetPlan,
    CheckDef,
    EffectiveConfig,
    LaunchConfig,
    LaunchSurfaces,
    MachineProfile,
    PendingAuthorization,
    PlannedCheck,
    PrepareReport,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    RepairPlanPreview,
    RepairPolicy,
    RepairTrigger,
    RoleConfig,
    RootBudgetBinding,
    RootBudgetLimits,
    RootBudgetPlan,
    RootBudgetPreview,
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
    #: The worktree run's base, resolved once to a commit (see :func:`resolve_base_commit`).
    #: Empty for an in-place run, or when the ref does not resolve here.
    base_commit: str = ""

    @property
    def ready_to_dispatch(self) -> bool:
        """Would `run` get past admission and past its dispatch gate?

        Authorization allowance is not part of this: it depends on a run's history, so it is
        checked when a run actually exists rather than promised here. Neither is anything only the
        ledger knows - a root's used repairs, or whether a rootless run's task already has a
        root - because a preview does not open the database.
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
            base_commit=self.base_commit,
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


def load_root_budget_plan(path: Path) -> RootBudgetPlan:
    """Read one root budget file: the user's ceilings for one root, never an approval.

    A missing file, invalid JSON, an unknown field or a limit this build cannot honour is
    refused rather than defaulted: a ceiling that came from a build default is a ceiling nobody
    chose. Only the *shape* is checked here. Whether the root still has allowance is decided by
    the run's own dispatch transaction against the ledger, because a preview cannot know what an
    earlier revision of the same task already consumed.
    """
    from .authorization import root_budget_from_plan

    raw = load_json_file(Path(path), what="root budget file")
    if not isinstance(raw, dict):
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"root budget file {path} must be a JSON object with a 'limits' member (see "
            f"docs/operations.md), not a JSON {type(raw).__name__}",
        )
    try:
        plan = RootBudgetPlan.model_validate(raw)
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"root budget file {path} is not a valid root budget plan: {exc}",
        ) from exc
    return root_budget_from_plan(plan)


def load_repair_policy(path: Path) -> RepairPolicy:
    """Read one repair policy: a bare ``RepairPolicy`` document, never a default.

    The file is the task's explicit opt-in to one bounded repair, so an unreadable, malformed or
    unsupported document is refused rather than defaulted: a policy that came from a build default
    would arm a behaviour nobody wrote down. Only the document is read here - whether this *scope*
    can support the policy (an isolated worktree, a review, a run ceiling that covers the worst
    case) is refused by ``admission`` before a dispatch exists, because that depends on the rest
    of the task and not on this file.
    """
    raw = load_json_file(Path(path), what="repair policy file")
    if not isinstance(raw, dict):
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"repair policy file {path} must be a JSON object with the RepairPolicy fields "
            f"(max_attempts, check_exit_codes, allow_reviewer_changes), not a JSON "
            f"{type(raw).__name__}",
        )
    try:
        return RepairPolicy.model_validate(raw)
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"repair policy file {path} is not a valid RepairPolicy: {exc}",
        ) from exc


def root_binding_for(resolved: ResolvedRun) -> RootBudgetBinding:
    """The root a run would be spent against. It computes the ledger path and opens nothing.

    The path is derived, never created: that is what keeps ``prepare`` from producing the
    SQLite file it names. The same call is made by ``run``, so the binding the preview reports
    and the binding the artifact must cover are the same object, field for field.
    """
    from .authorization import resolve_root_binding

    return resolve_root_binding(
        project_id=resolved.project.project_id,
        request=resolved.request(),
        data_dir=resolved.data_dir,
    )


def root_budget_preview(
    resolved: ResolvedRun,
    plan: RootBudgetPlan,
    *,
    repair: RepairPlanPreview,
) -> RootBudgetPreview:
    """What a preview can honestly say about a root: what it covers, its ceilings, its clock.

    It deliberately reports no "remaining" number: the ledger is not opened here - that is what
    keeps ``prepare`` from having a side effect - and even a number read now could be spent by
    another process before this run reserves. Every dispatch and every counter is decided by the
    run's own transaction.

    Two counts are reported rather than one, because they answer different questions: what one
    accepted delivery normally costs (``single_loop_dispatches``) and what this run must be able
    to afford if its repair policy is armed (``required_top_level_submissions``). Printing only
    the larger number would read as if every delivery cost four invocations.
    """
    binding = root_binding_for(resolved)
    limits = plan.limits
    if repair.enabled:
        repair_clause = (
            "This task carries a repair policy, so the run must be able to afford the worst "
            f"case of {repair.worst_case_dispatches} top-level submission(s) "
            "(I1 + R1 + I2 + R2) - a ceiling, not a quota: an attempt that is never needed is "
            "never bought. One repair is the maximum, and it is spent only on a declared "
            f"business check failure or a substantive reviewer rejection. This root allows at "
            f"most {limits.max_top_level_submissions}"
        )
    else:
        repair_clause = (
            "No repair is planned for this task: it carries no repair policy, so the "
            f"{limits.max_repairs}-repair counter is recorded and enforced but nothing spends "
            "it, and the normal count is also the maximum. This root allows at most "
            f"{limits.max_top_level_submissions}"
        )
    detail = (
        f"one accepted delivery normally needs {repair.single_loop_dispatches} top-level "
        f"submission(s) (implementer + reviewer). {repair_clause}. The "
        f"{limits.deadline_seconds}s deadline starts at the root's first successful reservation "
        f"and is recorded as deadline_at. Whether any allowance remains is decided by the run's "
        f"own dispatch transaction against {binding.ledger_path}, not by this preview"
    )
    return RootBudgetPreview(
        binding=binding,
        limits=limits,
        required_top_level_submissions=repair.worst_case_dispatches,
        repair_enabled=repair.enabled,
        single_loop_dispatches=repair.single_loop_dispatches,
        detail=detail,
    )


def effective_spec(
    spec: TaskSpec,
    *,
    base_commit: str | None = None,
    workspace_mode: str | None = None,
    repair_policy: RepairPolicy | None = None,
) -> TaskSpec:
    """Apply command-line overrides *before* admission.

    The overrides become part of the effective TaskSpec, so the stored spec, its digest, the
    rendered packet and the authorization binding all describe the same task. This used to
    live in ``cmd_run``; ``prepare`` needs the identical rule or its preview would describe a
    different spec than the run it previews.

    ``--repair-policy-file`` is the same kind of override, with one extra rule: a task that
    already names a repair policy and a flag that names another are two sources disagreeing about
    what may buy a repair, and this build refuses that rather than silently preferring one. The
    same policy written twice is not a disagreement - comparing digests keeps the resolution
    idempotent, so re-running an already-policied task with its own policy file works.
    """
    if repair_policy is not None and spec.repair_policy is not None:
        if repair_policy.digest() != spec.repair_policy.digest():
            raise RefusedError(
                RefusalCode.INVALID_SPEC,
                "the task file names one repair policy and --repair-policy-file another (task "
                f"{spec.repair_policy.digest()}, file {repair_policy.digest()}). Two sources "
                "disagreeing about what may buy a repair is not something this build resolves by "
                "preferring one: nothing was dispatched and no allowance was consumed.",
            )
    if not base_commit and not workspace_mode and repair_policy is None:
        return spec
    overrides: dict[str, Any] = {}
    if base_commit or workspace_mode:
        mode = workspace_mode or spec.workspace.mode
        overrides["workspace"] = {
            "mode": mode,
            "base_commit": base_commit or spec.workspace.base_commit,
            "keep": True if mode == "worktree" else spec.workspace.keep,
        }
    if repair_policy is not None:
        overrides["repair_policy"] = repair_policy.model_dump(mode="json")
    return TaskSpec.model_validate({**spec.model_dump(mode="json"), **overrides})


def resolve_base_commit(spec: TaskSpec, project_root: Path) -> str:
    """The commit a worktree run starts from: ``workspace.base_commit`` (or HEAD), resolved once.

    A ref such as 'HEAD' or 'main' is a name, not a base: inside the run's worktree HEAD is the
    candidate itself, and a branch can move while the run works. So the name is resolved to a
    commit here, once, and that commit is what the authorization binding names and what ``run``
    hands the controller (``RunRequest.base_commit``). The task text keeps the name, so its digest
    is unchanged; a branch that moves between ``prepare`` and ``run`` resolves to another commit,
    and an approval written for the old one no longer matches.

    Returns ``""`` for an in-place run, which has no worktree to start, and when the ref does not
    resolve here (no repository, no such commit). Nothing is refused at this point: the run's own
    worktree gate refuses an unresolvable base before anything is claimed, and the binding then
    carries the task's text as it always did. Read-only: one ``rev-parse``, no ref or worktree.
    """
    if spec.workspace.mode != "worktree":
        return ""
    from .gitworkspace import GitError, GitRepo

    try:
        return GitRepo.discover(project_root).resolve_commit(spec.workspace.base_commit or "HEAD")
    except (GitError, RefusedError):
        return ""


def launch_workspaces(spec: TaskSpec, project_root: Path) -> list[Path]:
    """The directories this run's agents work in; no launch program may be a file inside one.

    The project root always (an in-place run's workspace), and for a worktree run the directory
    its worktrees are created in (``GitRepo.worktree_parent``). acpx starts the agent there, and
    an agent can write there, so a launcher found inside would be a program the agent chose.
    Read-only: a worktree run's repository is discovered, nothing is created.
    """
    roots = [Path(project_root)]
    if spec.workspace.mode == "worktree":
        from .gitworkspace import GitError, GitRepo

        try:
            roots.append(GitRepo.discover(project_root).worktree_parent())
        except (GitError, RefusedError):
            pass  # no repository: the worktree gate refuses the run before anything is claimed
    return roots


def start_workspace_client_config(
    spec: TaskSpec, project_root: Path, base_commit: str = ""
) -> str:
    """Why the workspace this run starts in already holds acpx's project config, or ``""``.

    acpx always loads ``<cwd>/.acpxrc.json`` and lets it replace the agent command, so the
    driver refuses to launch on such a workspace - but only at its spawn gate, after the dispatch
    was reserved, and an identical TaskSpec then returns that blocked run. When the file is
    already in what the run starts from, it is knowable now, so admission refuses before anything
    is spent: the project root for an in-place run, the base commit's tree for a worktree run (a
    worktree is a checkout of that commit, so a copy the user did not commit is not in it).
    ``base_commit`` is the resolved base (``RunRequest.base_commit``) when the caller has one.
    The name is matched without regard to case, as the driver's spawn gate matches it: a
    checkout on a case-insensitive filesystem holds ``.ACPXRC.JSON`` as written, and acpx's open
    of ``.acpxrc.json`` finds it. Only the root's own entries count.
    Read-only: one listing of the project root, or one ``ls-tree`` of the base's root.
    """
    from .drivers.acpx_dsh import WORKSPACE_CLIENT_CONFIG_NAME, _workspace_client_config

    if spec.workspace.mode != "worktree":
        found = _workspace_client_config(Path(project_root))
        if found is None:
            return ""
        return (
            f"{found} exists in the project root this in-place run works in; remove it (or the "
            "directory) before submitting"
        )
    from .gitworkspace import GitError, GitRepo

    base = base_commit or spec.workspace.base_commit or "HEAD"
    try:
        # No pathspec: a pathspec matches case-sensitively even with core.ignorecase. Without
        # -r only the root's entries are listed; -z keeps names unquoted.
        listed = GitRepo.discover(project_root).run("ls-tree", "-z", "--name-only", base)
    except (GitError, RefusedError):
        return ""  # no repository or no such base: the worktree gate refuses that on its own
    wanted = WORKSPACE_CLIENT_CONFIG_NAME.casefold()
    matched = next(
        (name for name in listed.split("\0") if name and name.casefold() == wanted), ""
    )
    if not matched:
        return ""
    return (
        f"{matched} is in base commit {base}, which this worktree run "
        "starts from; commit its removal before submitting (a real run then needs an "
        "authorization issued for the new base)"
    )


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
    workspaces: Sequence[Path] = (),
) -> RoleConfig:
    """One role's configuration, including the *resolved launch* for a driver that starts one.

    The launch is resolved here and travels with the configuration, so it is covered by
    ``EffectiveConfig.digest()`` - what an approval binds - and consumed by ``build_driver``
    afterwards rather than re-derived from the environment. That is the difference between
    approving a configuration and approving a name. The role's own binding goes into the
    resolution, so its ``model_selection`` becomes that launch's ``--model`` value, and the run's
    ``workspaces`` (:func:`launch_workspaces`) go in so no launch program is a file inside them.
    """
    from .drivers.acpx_dsh import resolve_launch_config
    from .drivers.selected import resolve_driver_id

    driver_id = resolve_driver_id(binding)
    launch: LaunchConfig | None = None
    if driver_id != FAKE_DRIVER_ID:
        # The launcher profile a real transport starts with. It is the driver's own fixed
        # default; a profile that wants another one will need the binding to carry it, which
        # this build does not claim to support (see README, "Not implemented").
        launch = resolve_launch_config(
            data_dir=data_dir, env=env, binding=binding, workspaces=workspaces
        )
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
    workspaces: Sequence[Path] = (),
) -> EffectiveConfig:
    return EffectiveConfig(
        source="machine_profile",
        profile_id=profile.profile_id,
        profile_digest=profile_digest(profile),
        roles=[
            _role_config(
                role, agent_id, binding, data_dir=data_dir, env=env, workspaces=workspaces
            )
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
    workspaces: Sequence[Path] = (),
) -> EffectiveConfig:
    return EffectiveConfig(
        source="command_line",
        roles=[
            _role_config(
                role, "command-line", binding, data_dir=data_dir, env=env, workspaces=workspaces
            )
            for role in RUN_ROLES
        ],
        security_mode="unknown",
        implementer_writes=implementer_writes,
        reviewer_writes=reviewer_writes,
    )


def resolved_launches(effective: EffectiveConfig) -> list[LaunchConfig]:
    """The launches an effective configuration would perform."""
    return [entry.launch for entry in effective.roles if entry.launch is not None]


def launch_surface_preview(
    resolved: ResolvedRun,
    env: Mapping[str, str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, LaunchSurfaces]:
    """What each role's DSH would read on its own, as far as it is knowable before dispatch.

    A worktree does not exist yet, so its files are left to the spawn-time record; a
    per-invocation home is named, not looked into. Nothing is created and nothing is bound.
    An observation that fails leaves that role out, with the reason appended to ``notes``:
    like the spawn and probe paths, a record never stops the preview.
    """
    from .drivers.acpx_dsh import (
        INVOCATION_ID_PLACEHOLDER,
        child_environment,
        child_home_for,
        effective_dsh_home,
    )
    from .drivers.dsh_surfaces import observe_launch_surfaces

    workspace = resolved.project_root if resolved.spec.workspace.mode != "worktree" else None
    child_home = child_home_for(Path(resolved.data_dir).resolve(), INVOCATION_ID_PLACEHOLDER)
    surfaces: dict[str, LaunchSurfaces] = {}
    for entry in resolved.effective.roles:
        if entry.launch is None:
            continue
        try:
            kind, home = effective_dsh_home(entry.launch, child_home=child_home)
            surfaces[entry.role] = observe_launch_surfaces(
                entry.launch,
                dsh_home=home,
                dsh_home_kind=kind,
                child_env=child_environment(
                    env if env is not None else os.environ,
                    extra_env={},
                    dsh_home=entry.launch.dsh_home,
                ),
                workspace=workspace,
                look_in_home=kind == "bound",
            )
        except Exception as exc:  # noqa: BLE001 - a record never stops the preview
            if notes is not None:
                notes.append(
                    f"{entry.role} launch surfaces could not be examined: "
                    f"{type(exc).__name__}: {exc}"
                )
    return surfaces


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
    repair_policy: RepairPolicy | None = None,
    env: Mapping[str, str] | None = None,
    root_bound: bool = False,
    root_limits: RootBudgetLimits | None = None,
) -> ResolvedRun:
    """Resolve one task, project and machine configuration into what would be executed.

    ``root_bound`` says whether this run is given a root budget (``--root-budget-file``): a repair
    policy on a real transport needs one, and that rule is a dispatch precondition like the rest.
    ``root_limits`` are that file's ceilings, when the caller has them: a root that cannot pay for
    an armed repair is refused by the run before anything is recorded, so it is reported here too
    (``root_repair_problems``).
    """
    root = Path(project_root).resolve()
    spec_path = Path(task_path)
    spec = TaskSpec.model_validate(load_json_file(spec_path, what="task spec"))
    spec = effective_spec(
        spec,
        base_commit=base_commit,
        workspace_mode=workspace_mode,
        repair_policy=repair_policy,
    )

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
    workspaces = launch_workspaces(spec, root)
    if machine.profile is not None:
        effective = _effective_from_profile(
            machine.profile,
            machine.bindings,
            data_dir=Path(data_dir),
            env=env,
            implementer_writes=implementer_writes,
            reviewer_writes=reviewer_writes,
            workspaces=workspaces,
        )
    else:
        _, binding = machine.bindings["implementer"]
        effective = _effective_from_command_line(
            binding,
            data_dir=Path(data_dir),
            env=env,
            implementer_writes=implementer_writes,
            reviewer_writes=reviewer_writes,
            workspaces=workspaces,
        )

    is_real = any(entry.driver_id != FAKE_DRIVER_ID for entry in effective.roles)
    base = resolve_base_commit(spec, root)
    admission = validate_task_spec(spec, project, root, allow_fake_checks=not is_real)
    preconditions = predictable_dispatch_problems(
        spec,
        project,
        production=is_real,
        implementer_writes=implementer_writes,
        launches=resolved_launches(effective),
        real_transport=is_real,
        root_bound=root_bound,
        workspace_client_config=(
            start_workspace_client_config(spec, root, base)
            if is_real
            else ""
        ),
    )
    if root_limits is not None:
        preconditions.extend(root_repair_problems(spec, root_limits))
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
        base_commit=base,
    )


def root_repair_problems(spec: TaskSpec, limits: RootBudgetLimits) -> list[ValidationIssue]:
    """A root whose repair counter cannot pay for the task's armed repair policy.

    The part of the controller's repair gate (``Controller._assert_root_repair_allowance``) that
    the two files alone decide: the repair needs at least one. What the ledger adds - repairs
    already used, and a later revision's first implementer, which the root also charges as a
    repair - is the run's own check; a preview does not open the ledger.
    """
    if spec.repair_policy is None or limits.max_repairs >= 1:
        return []
    return [
        ValidationIssue(
            code=RefusalCode.BUDGET_EXHAUSTED,
            detail=(
                f"the root budget file allows max_repairs {limits.max_repairs}, and this task's "
                "repair policy needs at least 1 (2 when an earlier revision already dispatched "
                "an implementer on this root - the run checks that against the ledger). The run "
                "refuses before anything is recorded; set max_repairs to cover the repair, or "
                "drop repair_policy"
            ),
            location="root_budget",
        )
    ]


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


def repair_dispatch_counts(spec: TaskSpec, project: ProjectConfig) -> tuple[int, int]:
    """``(the fixed loop, the worst case)`` in top-level dispatches.

    The fixed loop is implementer (+ reviewer): what one accepted delivery costs. With a repair
    policy the ceiling doubles, because the repair attempt and the review that must follow it are
    two dispatches the first attempt cannot know about - the plan's ``I1 (+R1) + I2 (+R2)``. It is
    the number the run's own ceiling, the root and the authorization all have to cover before I1.
    A ceiling, not a quota: an attempt that is never needed is never bought.
    """
    single = 1 + (1 if spec.needs_review(project) else 0)
    if spec.repair_policy is None:
        return single, single
    return single, 2 * single


def _repair_plan_preview(resolved: ResolvedRun) -> RepairPlanPreview:
    """The repair plan as data, with the same discipline as the root budget preview.

    Present or absent, the digest of the policy an approval would cover, the triggers that are
    enabled (check ids with their declared exit codes, and whether reviewer changes count), the
    worst-case dispatch count - and an explicit statement of what the single repair may be spent
    on. Nothing here promises a repair will happen: a preview cannot know whether a check will
    fail, and a policy is a permission, not a plan to spend.
    """
    spec, project = resolved.spec, resolved.project
    single, worst_case = repair_dispatch_counts(spec, project)
    policy = spec.repair_policy
    if policy is None:
        return RepairPlanPreview(
            enabled=False,
            single_loop_dispatches=single,
            worst_case_dispatches=worst_case,
            detail=(
                "no repair_policy on this task, so no repair is attempted and "
                "budget.max_repair_cycles arms nothing - a numeric budget field is not a policy. "
                f"The worst case stays the fixed loop of {single} top-level dispatch(es). A "
                "repair needs an explicit policy naming the checks and exit codes that mean a "
                "business assertion failed, or allowing a substantive reviewer rejection"
            ),
        )

    triggers: list[str] = []
    if policy.check_exit_codes:
        triggers.append(RepairTrigger.BUSINESS_CHECK_FAILED.value)
    if policy.allow_reviewer_changes:
        triggers.append(RepairTrigger.REVIEW_CHANGES_REQUESTED.value)
    declared = "; ".join(
        f"{check_id} with exit code(s) {', '.join(str(code) for code in codes)}"
        for check_id, codes in sorted(policy.check_exit_codes.items())
    )
    if policy.check_exit_codes and policy.allow_reviewer_changes:
        spent_on = (
            f"a declared business check failure ({declared}) or a substantive reviewer "
            "changes_requested (both enabled by this policy)"
        )
    elif policy.check_exit_codes:
        spent_on = (
            f"a declared business check failure ({declared}); a reviewer changes_requested does "
            "not qualify under this policy"
        )
    else:
        spent_on = (
            "a substantive reviewer changes_requested with usable findings; this policy declares "
            "no check failure that may buy one"
        )
    detail = (
        f"one repair is the maximum for this run. It is spent only on {spent_on}. A timeout, a "
        "killed or uncollected check, a transport, environment or driver error, a malformed "
        "review, an unknown outcome or an empty findings list buys no repair. A repaired "
        "candidate is verified and independently reviewed again from scratch. The worst case is "
        f"a ceiling, not a quota: I1{' + R1' if single > 1 else ''} + I2"
        f"{' + R2' if single > 1 else ''} = {worst_case} top-level dispatch(es), and an attempt "
        "that is never needed is never bought"
    )
    return RepairPlanPreview(
        enabled=True,
        policy_digest=policy.digest(),
        check_exit_codes={key: list(value) for key, value in policy.check_exit_codes.items()},
        allow_reviewer_changes=policy.allow_reviewer_changes,
        triggers=triggers,
        single_loop_dispatches=single,
        worst_case_dispatches=worst_case,
        detail=detail,
    )


def _budget_plan(resolved: ResolvedRun, roles: list[str]) -> BudgetPlan:
    spec, project = resolved.spec, resolved.project
    single, worst_case = repair_dispatch_counts(spec, project)
    implementer = 1
    reviewer = 1 if "reviewer" in roles else 0
    # ``required_turns`` is what an authorization has to cover. With a repair policy armed that is
    # the worst case, not the fixed loop: an approval that only covered I1 + R1 could not pay for
    # the repair the task explicitly enabled, and the run would refuse it after the first attempt.
    required = worst_case
    ceiling = min(spec.budget.max_agent_turns, project.limits.max_agent_turns)
    within = required <= ceiling
    cycle_note = (
        f"; one repair cycle is armed, so the worst case is {worst_case} "
        f"(fixed loop {single} + one repair round)"
        if spec.repair_policy is not None
        else ""
    )
    detail = (
        f"implementer {implementer} + reviewer {reviewer} = {single} reserved turn(s) for one "
        f"accepted delivery{cycle_note}; {required} reserved turn(s) required in total, against "
        f"task budget {spec.budget.max_agent_turns} and project pre-authorization "
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
        repair_cycles=1 if spec.repair_policy is not None else 0,
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
    root_budget_plan: RootBudgetPlan | None = None,
    env: Mapping[str, str] | None = None,
) -> PrepareReport:
    """Assemble the preview. Zero model calls, zero writes, zero run state.

    ``root_budget_plan`` is the parsed ``--root-budget-file``, when one was given. The root it
    describes is reported as *data* - the binding, the ceilings, the clock - and the pending
    authorization carries the same root, so the digest the preview prints is the digest an
    artifact must have for this run to accept it. Neither step touches the ledger.
    """
    from .authorization import current_binding

    roles = resolved.roles
    execution_root, execution_root_is_final = resolved.execution_root()
    repair = _repair_plan_preview(resolved)
    budget = _budget_plan(resolved, roles)
    root_preview = (
        root_budget_preview(resolved, root_budget_plan, repair=repair)
        if root_budget_plan is not None
        else None
    )
    # What an approval has to cover. ``budget.required_turns`` is already the worst case when a
    # repair policy is armed, so the artifact the preview describes covers the loop the run's own
    # gate prices before I1 - not just its first half.
    submissions = (
        max_top_level_submissions_required
        if max_top_level_submissions_required is not None
        else budget.required_turns
    )
    # A root budget file makes the artifact necessary even for the offline driver: every charge
    # against a root is recorded with the artifact that bought it, and the store refuses to spend
    # a root under an empty authorization id. For the offline driver `run` mints a labelled
    # record; for any other it requires the user's own artifact.
    needs_authorization = resolved.is_real_driver or root_preview is not None
    pending = PendingAuthorization(required=needs_authorization)
    if needs_authorization:
        implementer = resolved.effective.role("implementer")
        assert implementer is not None
        binding = current_binding(
            mode=authorization_mode,  # type: ignore[arg-type]
            driver=implementer.driver,
            project=resolved.project,
            request=resolved.request(),
            spec_path=resolved.spec_path,
            effective=resolved.effective,
            # The same root ``run`` will bind when handed the same file: the two must agree or
            # the preview would print an artifact digest that the run then refuses.
            root_binding=root_preview.binding if root_preview is not None else None,
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
        "history; nor is anything only the ledger knows (a root's used repairs, or whether this "
        "task already has a root, which a run without --root-budget-file is refused for)",
    ]
    if not execution_root_is_final:
        notes.append(
            "the worktree path contains the run id, which is chosen at dispatch: the packet "
            "size and digest above are for the template path and shift with the id's length"
        )
    surfaces = launch_surface_preview(resolved, env, notes) if resolved.is_real_driver else {}
    if resolved.is_real_driver:
        notes.append(
            "a live run needs an authorization artifact the user writes from their own "
            "approval; prepare cannot produce one and the binding above is only what the "
            "approval would have to cover"
        )
        notes.append(
            "the profile selects which agent, transport, write permission and model each role "
            "uses. A `model_selection` other than native_profile is passed as the client's "
            "`--model <value>` (bound in the launch above); native_profile passes no flag and "
            "the DSH profile the launcher starts with decides. The model_selection capability "
            "is still documented only: no set_config_option round trip with a real DSH has been "
            "observed, so each run records what its stream showed - `hflow doctor --profile "
            "<id>` shows the exact argv"
        )
        notes.append(
            "launch surfaces (what DSH reads on its own: its home's patch files, AGENTS.md and "
            "skills, a workspace .env by presence and size only, AGENTS.md/CLAUDE.md files, "
            "skills, DSH_* variable names, and the client and carrier versions) are recorded, "
            "not enforced, and are not part of the approval binding; a worktree's are observed "
            "at each invocation's spawn"
        )
        if any(
            surface.dsh_home_kind == "per_invocation" and not surface.deepseek_api_key_inherited
            for surface in surfaces.values()
        ):
            if resolved.spec.workspace.mode == "worktree":
                env_source = "worktree: only what the base commit tracks"
            else:
                workspace_envs = [
                    surface.workspace_env
                    for surface in surfaces.values()
                    if surface.workspace_env is not None
                ]
                env_file = workspace_envs[0] if workspace_envs else None
                env_state = (
                    "present"
                    if env_file is not None and env_file.present
                    else "unknown"
                    if env_file is not None and env_file.present is None
                    else "absent"
                )
                env_path = env_file.path if env_file is not None else str(resolved.project_root)
                env_source = f"in-place: {env_state} at {env_path}"
            notes.append(
                "DEEPSEEK_API_KEY is not in the launch environment and DSH_HOME is unbound, so "
                "the per-invocation DSH home holds no stored credential (inferred from upstream "
                "source); the documented source left is a .env in the workspace DSH starts in "
                f"({env_source}). M0 observed DSH fail with a no-API-key error when it had no "
                "credential"
            )
    if not budget.within_budget:
        notes.append(
            "this task does not fit its budget: it needs more reserved turns than are "
            "available, so a run refuses before dispatching anything"
        )
    if repair.enabled:
        notes.append(
            f"this task carries a repair policy (digest {repair.policy_digest}): at most one "
            "repair, spent only on a declared business check failure or a substantive reviewer "
            f"rejection, with a worst case of {repair.worst_case_dispatches} top-level "
            "dispatch(es) for this run. It is a ceiling, not a quota: an attempt that is never "
            "needed is never bought"
        )
    if root_preview is not None:
        notes.append(
            f"a root budget file was given: this run would register root "
            f"{root_preview.binding.root_id} and spend both roles' dispatches against "
            f"{root_preview.binding.ledger_path}. That path is derived here, not created - "
            "prepare opened no database"
        )
        notes.append(
            "whether the root still has allowance, and whether its deadline has passed, is "
            "decided by the run's own dispatch transaction against the ledger; this preview "
            "cannot promise a remaining number"
        )
        if repair.enabled:
            notes.append(
                f"the root's repair counter ({root_preview.limits.max_repairs}) is spent by the "
                "repair attempt itself (and by a later revision's first implementer, which the "
                "root already counts as a repair), and the ceiling above is what this root has "
                "to cover before I1: a root that cannot afford the loop or the repair refuses "
                "the run rather than stopping half-way"
            )
        else:
            notes.append(
                "no repair is planned for this task: it carries no repair_policy, so the root's "
                "repair counter is recorded and enforced but nothing spends it. "
                "budget.max_repair_cycles is a historical number and arms nothing"
            )
        if not resolved.is_real_driver:
            notes.append(
                "this run uses the offline fake driver, but the root budget file still makes an "
                "authorization necessary: every charge against a root is recorded with the "
                "artifact that bought it. `run` mints a clearly-labelled offline record (its "
                "user_text says it is not an approval, and it binds the fake driver) unless you "
                "pass your own --authorization-file"
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
        repair_plan=repair,
        roles=roles,
        root_budget=root_preview,
        packet_preview=preview,
        launch_surfaces=surfaces,
        authorization=pending,
        notes=notes,
    )


def _repair_plan_lines(repair: RepairPlanPreview) -> list[str]:
    """The repair plan in the text form: the same facts the JSON carries, no promise of a repair.

    "Present or absent" is said explicitly, because ``budget.max_repair_cycles`` still reads 1 on
    every task that predates E2 and a reader could mistake that number for an armed repair.
    """
    lines = ["repair plan"]
    if not repair.enabled:
        lines.append(
            "  none        no repair_policy on this task: no repair is attempted, whatever "
            f"budget.max_repair_cycles says. Worst case stays {repair.single_loop_dispatches} "
            "top-level dispatch(es)"
        )
        return lines
    lines.append(f"  enabled     policy digest {repair.policy_digest}")
    if repair.check_exit_codes:
        for check_id, codes in sorted(repair.check_exit_codes.items()):
            lines.append(
                f"  trigger     business_check_failed: {check_id} -> exit code(s) "
                f"{', '.join(str(code) for code in codes)}"
            )
    else:
        lines.append(
            "  trigger     business_check_failed: (no check may buy a repair under this policy)"
        )
    lines.append(
        "  trigger     review_changes_requested: "
        + ("allowed" if repair.allow_reviewer_changes else "not allowed")
    )
    lines.append(
        f"  maximum     one repair, worst case {repair.worst_case_dispatches} top-level "
        f"dispatch(es) (fixed loop {repair.single_loop_dispatches} + one repair round); a "
        "ceiling, not a quota"
    )
    lines.append(f"  detail      {repair.detail}")
    return lines


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
            if launch.model:
                lines.append(f"               client flag --model {launch.model}")
            surface = report.launch_surfaces.get(entry.role)
            if surface is None:
                if launch.dsh_home:
                    lines.append(f"               DSH home bound {launch.dsh_home}")
            elif surface.dsh_home_kind == "bound":
                lines.append(f"               DSH home bound {surface.dsh_home}")
            else:
                lines.append(
                    f"               DSH home per-invocation {surface.dsh_home} (DSH_HOME "
                    "unbound: created empty for each invocation, inferred; not looked into "
                    "before dispatch)"
                )
            if not launch.resolvable:
                lines.append(f"               reason {launch.detail}")
    lines.append(
        f"  writes      implementer={effective.implementer_writes} "
        f"reviewer={effective.reviewer_writes}"
    )
    lines.append(f"  config_hash {effective.digest()}")
    if report.launch_surfaces:
        from .report import launch_surfaces_lines

        lines.append("launch surfaces (recorded, not enforced, not part of the approval)")
        for role, surface in report.launch_surfaces.items():
            lines.extend(launch_surfaces_lines(role, surface, indent="  "))
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
        f"(implementer {report.budget.implementer_turns}, reviewer {report.budget.reviewer_turns}"
        + (f", repair cycles {report.budget.repair_cycles}" if report.budget.repair_cycles else "")
        + f"), task budget {report.budget.task_turn_budget}, "
        f"project pre-authorization {report.budget.project_turn_limit}"
    )
    lines.append(f"  fits        {report.budget.within_budget}")
    lines.extend(_repair_plan_lines(report.repair_plan))
    lines.append("root budget")
    root = report.root_budget
    if root is None:
        lines.append(
            "  not bound   no --root-budget-file was given: this run would spend the "
            "authorization's own allowance, with no root ledger row and no root repair counter"
        )
    else:
        lines.append(
            f"  binding     {root.binding.root_id} (project {root.binding.project_id}, "
            f"task {root.binding.task_id})"
        )
        lines.append(f"  repo        {root.binding.repo_path}")
        lines.append(
            f"  ledger      {root.binding.ledger_path}  (derived; prepare created nothing)"
        )
        lines.append(
            f"  limits      top-level submissions {root.limits.max_top_level_submissions}, "
            f"repairs {root.limits.max_repairs}, deadline {root.limits.deadline_seconds}s"
        )
        lines.append(
            f"  needs       {root.required_top_level_submissions} top-level submission(s) worst "
            f"case (fixed loop {root.single_loop_dispatches}: implementer + reviewer)"
        )
        if root.repair_enabled:
            lines.append(
                "  repair      ARMED by this task's repair policy: the worst case above is a "
                "ceiling, not a quota, and one repair is the maximum"
            )
        else:
            lines.append(
                f"  repair      none planned - this task carries no repair policy, so the "
                f"{root.limits.max_repairs}-repair counter is recorded but nothing spends it"
            )
        lines.append(
            f"  deadline    {root.limits.deadline_seconds}s counted from the root's first "
            "successful reservation (recorded as deadline_at), not from now"
        )
        lines.append(
            "  allowance   not checked here: the run's own dispatch transaction decides whether "
            "any is left"
        )
        lines.append(f"  detail      {root.detail}")
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
