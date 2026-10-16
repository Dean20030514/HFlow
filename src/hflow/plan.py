"""Saved plans: ``prepare --plan-out``, ``approve --plan`` and ``run --plan``.

Without a plan, a real task needs the operator to copy ``authorization.binding`` out of
``prepare --json`` into a hand-built artifact and then repeat every ``prepare`` flag, identically,
to ``run``. A plan writes the resolution inputs and the preview down once:

* ``prepare --plan-out PLAN`` records the inputs (paths made absolute) and the full report; it
  replaces an older plan at ``PLAN`` and refuses to replace any other file;
* ``approve --plan PLAN`` resolves those inputs again through the same function, refuses a plan
  whose binding changed since (stale), and then asks a person at an interactive terminal: it
  shows the summary and an *approval code* (the first 8 hex of the binding digest), reads that
  code back, then reads the approval in the person's own words, and only then writes an
  authorization artifact;
* ``run --plan PLAN`` takes every resolution input from the plan and checks the approval exactly
  as a run with flags does. ``approve --plan PLAN --run`` goes on to exactly that, in the same
  process, once the approval is written - and only then.

Approving stays a separate human step. Nothing is taken from an argument, and a standard input
that is not a terminal (a pipe, a file) is refused without being read, so ``echo ok | hflow
approve`` approves nothing; a wrong code - ``no``, ``y``, an empty line - approves nothing either.
This is a deliberate-act gate, not authentication: a process that drives a pseudo-terminal and
reads the code off the screen can still type both lines, which is the provenance limit
``docs/operations.md`` states for every authorization artifact. A plan is not an authorization: it
carries no ``user_text`` and no ``provided_by``, and ``AuthorizationRecord`` forbids its fields,
so a plan handed to ``--authorization-file`` is refused. Nothing here calls a model or opens the
ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from pydantic import ValidationError

from .contracts import (
    LAUNCH_CONTENT_FIELDS,
    EffectiveConfig,
    PlanArguments,
    PrepareReport,
    RefusalCode,
    RefusedError,
    RootBudgetLimits,
    RunPlan,
)
from .prepare import Resolution, pending_binding, root_binding_for
from .profiles import (
    ENV_PROFILE,
    load_json_file,
    requested_profile_id,
    summarize_validation_error,
)

if TYPE_CHECKING:  # annotations only
    from .authorization import AuthorizationBinding, AuthorizationRecord

#: The stem suffix a plan file conventionally carries (``task.plan.json``); dropped from the
#: default authorization file name so that reads ``task.<digest>.authorization.json``.
PLAN_STEM_SUFFIX = ".plan"

#: ``(PlanArguments field, flag)`` of every input ``prepare`` and ``run`` resolve a task from - the
#: CLI's resolution flags, in its order. With ``run --plan`` they come from the plan, never the
#: command line, and :func:`prepare_command` prints them back from a plan.
RESOLUTION_FLAGS = (
    ("task", "--task"),
    ("project", "--project"),
    ("project_root", "--project-root"),
    ("profile", "--profile"),
    ("driver", "--driver"),
    ("allow_writes", "--allow-writes"),
    ("root_budget_file", "--root-budget-file"),
    ("repair_policy_file", "--repair-policy-file"),
)
DEFAULT_PROJECT_ROOT = "."
#: ``PlanArguments`` keys an older build recorded from flags it no longer has (``--base-commit``,
#: ``--workspace``): the task file carries both. A plan that set one is refused with the command
#: that prepares it again; a plan that left them ``null`` loads without them.
RETIRED_PLAN_ARGUMENTS = {"base_commit": "--base-commit", "workspace": "--workspace"}


def prepare_command(arguments: PlanArguments, plan_path: Path) -> str:
    """The ``hflow prepare ... --plan-out PLAN`` that writes this plan again, ready to paste.

    Rebuilt from the inputs the plan recorded (absolute paths, the profile it named), so
    re-preparing a stale plan does not mean retyping every flag. A profile the plan did not name
    is left out, so ``HFLOW_PROFILE`` decides again, as it did the first time, and
    ``--data-dir`` is added when the plan's data directory is not the default one (an older
    plan's ``authorization_mode`` has no flag: a re-prepared plan is for ``m2-live-change``). It
    resolves afresh: what it prints is a new plan to review. ``--allow-writes`` is printed when
    the plan recorded the write opt-in, however it was given then.
    """
    from .hints import command, data_dir_word, word

    words = ["prepare"]
    for field, flag in RESOLUTION_FLAGS:
        value = getattr(arguments, field)
        if value is None or value is False:
            continue
        words += [flag] if value is True else [flag, word(str(value))]
    words += ["--plan-out", word(os.path.abspath(plan_path))]
    return command(data_dir_word(arguments.data_dir), *words)


def frozen_arguments(
    arguments: PlanArguments, env: Mapping[str, str] | None = None
) -> PlanArguments:
    """``arguments`` as a plan records them: every path absolute, the selected profile named.

    Paths are made absolute against this process's working directory, so ``approve`` and ``run``
    find the same files from anywhere. The profile is the one this resolution selected
    (``--profile``, else ``HFLOW_PROFILE``) and is recorded by name, so once a plan names one a
    later ``HFLOW_PROFILE`` is not consulted (``--profile`` beats it). A plan that selected *no*
    profile records ``None``, and then a later ``HFLOW_PROFILE`` does change what ``approve`` and
    ``run --plan`` resolve. That is not a way round the plan: the binding names the profile, so the
    new resolution is refused as a stale plan, or - an offline plan now resolving to a real driver
    - as a run with no authorization. The task's ``workspace.base_commit`` stays as the task file
    says: a ref resolves again, and a moved branch then shows as a changed binding.
    """

    def absolute(value: str | None) -> str | None:
        return os.path.abspath(value) if value else value

    return arguments.model_copy(
        update={
            "task": os.path.abspath(arguments.task),
            "project": absolute(arguments.project),
            "project_root": os.path.abspath(arguments.project_root),
            "profile": requested_profile_id(arguments.profile, env) or None,
            "root_budget_file": absolute(arguments.root_budget_file),
            "repair_policy_file": absolute(arguments.repair_policy_file),
            "data_dir": os.path.abspath(arguments.data_dir),
        }
    )


def resolution_binding(resolution: Resolution, *, authorization_mode: str) -> AuthorizationBinding:
    """The binding this resolution amounts to - for an approval, or for a staleness check.

    For a plan that needs an approval it is the binding ``prepare`` prints and ``run`` verifies;
    for an offline plan it is computed the same way, so an edited task, contract or base commit
    makes an offline plan stale too.
    """
    resolved = resolution.resolved
    return pending_binding(
        resolved,
        authorization_mode=authorization_mode,
        root_binding=root_binding_for(resolved) if resolution.root_plan is not None else None,
    )


def build_plan(
    arguments: PlanArguments,
    resolution: Resolution,
    report: PrepareReport,
    *,
    created_at: str,
    controller_build: str,
) -> RunPlan:
    binding = resolution_binding(resolution, authorization_mode=arguments.authorization_mode)
    return RunPlan(
        created_at=created_at,
        controller_build=controller_build,
        arguments=arguments,
        prepare_report=report,
        binding=binding.model_dump(mode="json"),
        binding_digest=binding.digest(),
        root_limits=resolution.root_plan.limits if resolution.root_plan is not None else None,
    )


def check_plan_target(path: Path) -> Path:
    """``path`` made absolute, or a refusal when writing a plan there would destroy another file.

    :func:`write_plan` replaces its target whole, so ``--plan-out task.json`` - or an
    authorization file, or the project contract - would silently overwrite the file it names. A
    plan may therefore only go where nothing exists yet or where an older plan is: a JSON object
    whose ``kind`` is ``"hflow-plan"`` (any schema version - a stale or outdated plan is exactly
    what re-preparing replaces). A directory, a file that cannot be read as JSON, and every other
    document are refused and left as they were.
    """
    target = Path(os.path.abspath(path))
    if not os.path.lexists(target):
        return target

    def refused(why: str) -> RefusedError:
        return RefusedError(
            RefusalCode.INVALID_SPEC,
            f"--plan-out {target} {why}; a plan only ever replaces an older plan. Nothing was "
            "written and the file was left as it was; choose another path for the plan",
        )

    if target.is_dir():
        raise refused("is a directory")
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise refused(
            f"exists and could not be read as an HFlow plan ({type(exc).__name__})"
        ) from exc
    if not isinstance(raw, dict) or raw.get("kind") != "hflow-plan":
        raise refused('exists and is not an HFlow plan (no "kind": "hflow-plan")')
    return target


def write_plan(path: Path, plan: RunPlan) -> Path:
    """Write ``plan`` to ``path`` atomically; only an older plan there is ever replaced.

    The text goes to a temporary file in the same directory first and is moved over ``path`` in
    one ``os.replace``, so a reader never sees half a plan. Replacing a plan is allowed on purpose:
    a plan is a preview, and re-preparing is how a stale one is refreshed. Any other existing file
    is refused (:func:`check_plan_target`). Approvals are never replaced
    (:func:`write_authorization`).
    """
    target = check_plan_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(plan.model_dump_json(indent=2) + "\n")
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return target


def load_plan(path: Path) -> RunPlan:
    """Read one plan, refusing anything that is not a valid plan of this schema."""
    raw = load_json_file(Path(path), what="plan file")
    if not isinstance(raw, dict) or raw.get("kind") != "hflow-plan":
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{path} is not an HFlow plan (no \"kind\": \"hflow-plan\"); write one with "
            "`hflow prepare ... --plan-out <file>`",
        )
    _upgrade_plan_arguments(raw, Path(path))
    try:
        return RunPlan.model_validate(raw)
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{path} is not a valid plan: {summarize_validation_error(exc)}",
        ) from exc


def _upgrade_plan_arguments(raw: dict, path: Path) -> None:
    """Read an older plan's ``arguments`` as this build records them, or refuse the plan.

    * No ``allow_writes`` (a plan from before ``--allow-writes``): the write permission the plan's
      own report recorded (``effective_config.implementer_writes``) - the value its binding digest,
      and any approval of it, already cover. The environment is not read.
    * :data:`RETIRED_PLAN_ARGUMENTS` left ``null`` are dropped. A plan that set one resolved a task
      other than its file says, and no flag reproduces that any more, so it is refused - naming the
      command that prepares it again from the task file - rather than run with the override
      silently gone (or failing as an unknown field).
    """
    arguments = raw.get("arguments")
    if not isinstance(arguments, dict):
        return
    if "allow_writes" not in arguments:
        report = raw.get("prepare_report")
        config = report.get("effective_config") if isinstance(report, dict) else None
        arguments["allow_writes"] = (
            isinstance(config, dict) and config.get("implementer_writes") is True
        )
    given = []
    for key, flag in RETIRED_PLAN_ARGUMENTS.items():
        if key in arguments and arguments.pop(key) is not None:
            given.append(flag)
    if not given:
        return
    try:
        again = ": " + prepare_command(PlanArguments.model_validate(arguments), path)
    except ValidationError:
        again = " with `hflow prepare ... --plan-out <file>`"
    raise RefusedError(
        RefusalCode.INVALID_SPEC,
        f"{path} was prepared with {' and '.join(given)}, which this build no longer has: the "
        "task file's workspace.mode and workspace.base_commit are the only source. Nothing was "
        "written or dispatched; put the value in the task file and re-prepare the plan" + again,
    )


#: The binding key whose change :func:`plan_drift` explains field by field.
_EFFECTIVE_CONFIG_KEY = "effective_config_digest"


def _environment_sources(plan: RunPlan) -> dict[str, str]:
    """Effective-configuration fields read from the resolving process's environment, by name.

    ``prepare``, ``approve`` and ``run --plan`` each resolve the plan in their own process, so a
    shell with another value of one of these variables resolves another configuration - and the
    plan turns stale with only a digest to show for it. Leaf field name -> the variables it is
    read from: ``acpx_dsh.resolve_launch_config`` (a real role's launch: ``dsh``/``python``/
    ``node`` are looked up on ``PATH`` - ``dsh`` then in the data directory's npm install - and
    ``cmd.exe`` under ``SystemRoot``). The profile is read from ``HFLOW_PROFILE`` only while the
    plan names none (:func:`frozen_arguments`). The write opt-in is not among them: a plan
    records it (``PlanArguments.allow_writes``).
    """
    from .drivers.acpx_dsh import ENV_ACPX_CLI, ENV_ACPX_NODE

    sources = {
        "dsh_home": "DSH_HOME",
        "client_entry": ENV_ACPX_CLI,
        "node": f"{ENV_ACPX_NODE}, PATH",
        "client_argv_prefix": f"{ENV_ACPX_NODE}, PATH",
        "python": "PATH",
        "dsh_executable": "PATH, the data directory's npm DSH install",
        "agent_argv": "PATH, the data directory's npm DSH install, SystemRoot",
    }
    if plan.arguments.profile is None:
        sources.update(source=ENV_PROFILE, profile_id=ENV_PROFILE, profile_digest=ENV_PROFILE)
    return sources


def _config_view(config: Mapping[str, object]) -> dict[str, object]:
    """``config`` (an ``EffectiveConfig`` dump) as its digest sees it: launch content left out.

    The content digests are bound by ``launch_content_digest``, a binding key of their own, so
    they never explain a change of ``effective_config_digest``.
    """
    view = json.loads(json.dumps(config))  # a deep copy; the dump is JSON already
    for role in view.get("roles") or []:
        launch = role.get("launch") if isinstance(role, dict) else None
        if isinstance(launch, dict):
            for field in LAUNCH_CONTENT_FIELDS:
                launch.pop(field, None)
    return view


def _changed_fields(planned: object, now: object, path: str) -> list[tuple[str, object, object]]:
    """``(field path, planned, now)`` for every leaf that differs; roles are matched by name."""
    if planned == now:
        return []
    if isinstance(planned, dict) and isinstance(now, dict):
        changed: list[tuple[str, object, object]] = []
        for key in [*planned, *(key for key in now if key not in planned)]:
            changed += _changed_fields(planned.get(key), now.get(key), f"{path}.{key}")
        return changed

    def by_role(value: object) -> dict[str, object] | None:
        if not isinstance(value, list):
            return None
        named = {item.get("role"): item for item in value if isinstance(item, dict)}
        if len(named) != len(value) or not all(isinstance(name, str) for name in named):
            return None
        return named  # type: ignore[return-value]

    planned_roles, now_roles = by_role(planned), by_role(now)
    if planned_roles is not None and now_roles is not None:
        changed = []
        for role in [*planned_roles, *(role for role in now_roles if role not in planned_roles)]:
            changed += _changed_fields(
                planned_roles.get(role), now_roles.get(role), f"{path}[{role}]"
            )
        return changed
    return [(path, planned, now)]


def effective_config_changes(plan: RunPlan, now: EffectiveConfig) -> list[str]:
    """Which fields of the planned effective configuration differ from ``now``, in words.

    One line per changed field, naming the environment variable it is read from when it is one
    of :func:`_environment_sources` - the usual reason a plan turns stale without any file having
    changed. Wording only: whether a plan is stale is decided by the binding digest.
    """
    sources = _environment_sources(plan)
    lines: list[str] = []
    for path, planned, current in _changed_fields(
        _config_view(plan.prepare_report.effective_config.model_dump(mode="json")),
        _config_view(now.model_dump(mode="json")),
        "effective_config",
    ):
        line = f"{path}: planned {planned!r} != now {current!r}"
        variables = sources.get(path.rsplit(".", 1)[-1])
        if variables:
            line += f" (read from the environment: {variables})"
        lines.append(line)
    return lines


def plan_drift(
    plan: RunPlan,
    *,
    binding: AuthorizationBinding,
    root_limits: RootBudgetLimits | None,
    submissions_required: int | None = None,
    effective: EffectiveConfig | None = None,
) -> list[str]:
    """What differs between ``plan`` and a fresh resolution of its arguments; empty when current.

    With ``effective`` (the fresh resolution's effective configuration), a changed
    ``effective_config_digest`` is followed by the configuration fields that changed.
    """
    differences: list[str] = []
    current = binding.model_dump(mode="json")
    if binding.digest() != plan.binding_digest:
        for key in sorted(set(plan.binding) | set(current)):
            planned, now = plan.binding.get(key), current.get(key)
            if planned != now:
                differences.append(f"{key}: planned {planned!r} != now {now!r}")
                if key == _EFFECTIVE_CONFIG_KEY and effective is not None:
                    differences.extend(effective_config_changes(plan, effective))
        if not differences:
            differences.append(
                f"binding digest: planned {plan.binding_digest} != now {binding.digest()}"
            )
    if root_limits != plan.root_limits:
        planned_limits = plan.root_limits.model_dump(mode="json") if plan.root_limits else None
        now_limits = root_limits.model_dump(mode="json") if root_limits else None
        differences.append(f"root_limits: planned {planned_limits!r} != now {now_limits!r}")
    planned_count = plan.prepare_report.authorization.max_top_level_submissions_required
    if submissions_required is not None and submissions_required != planned_count:
        differences.append(
            f"required top-level submissions: planned {planned_count} != now {submissions_required}"
        )
    return differences


def require_current(
    plan: RunPlan,
    plan_path: Path,
    resolution: Resolution,
    *,
    approved: bool,
    submissions_required: int | None = None,
) -> None:
    """Refuse ``plan`` as stale unless ``resolution`` - its arguments resolved again - is what it
    planned: the same binding, the same root ceilings and, when given, the same submission count.

    ``approve`` and ``run --plan`` both call it, after resolving and before anything is asked,
    written or dispatched. ``approved`` picks the remedy the refusal names (re-approve, or only
    re-prepare an offline plan).
    """
    differences = plan_drift(
        plan,
        binding=resolution_binding(
            resolution, authorization_mode=plan.arguments.authorization_mode
        ),
        root_limits=resolution.root_plan.limits if resolution.root_plan is not None else None,
        submissions_required=submissions_required,
        effective=resolution.resolved.effective,
    )
    if differences:
        raise stale_plan_error(plan, plan_path, differences, approved=approved)


def stale_plan_error(
    plan: RunPlan, plan_path: Path, differences: list[str], *, approved: bool
) -> RefusedError:
    """The refusal for a plan whose inputs no longer resolve to what was planned.

    It ends with the ``prepare`` command that writes the plan again (:func:`prepare_command`).
    """
    again = (
        "re-prepare it, review the new plan and `hflow approve --plan` it"
        if approved
        else "re-prepare it and review the new plan"
    )
    return RefusedError(
        RefusalCode.RISK_DOWNGRADE,
        f"plan {plan_path} is stale: its inputs no longer resolve to what was prepared ("
        + "; ".join(differences)
        + f"). Nothing was written or dispatched; {again}: "
        + prepare_command(plan.arguments, plan_path),
    )


def default_plan_path(task_path: Path) -> Path:
    """Where ``approve --task`` writes a task's plan by default: ``<task stem>.plan.json`` next to
    the task, a trailing ``.task`` dropped from the stem (``T-1.task.json`` -> ``T-1.plan.json``,
    the name ``init task``'s printed ``prepare --plan-out`` uses too)."""
    task = Path(os.path.abspath(task_path))
    stem = task.name.removesuffix(".json").removesuffix(".task")
    return task.with_name(f"{stem}.plan.json")


def default_authorization_path(plan_path: Path, binding_digest: str) -> Path:
    """Where ``approve`` writes, and ``run --plan`` looks for, this plan's approval by default.

    ``<plan stem>.<first 12 hex of the binding digest>.authorization.json`` next to the plan (a
    trailing ``.plan`` is dropped from the stem). The digest is in the name so a re-prepared plan
    whose binding changed never collides with, or picks up, an approval of the old one.
    """
    plan = Path(os.path.abspath(plan_path))
    stem = plan.stem
    if stem.endswith(PLAN_STEM_SUFFIX) and len(stem) > len(PLAN_STEM_SUFFIX):
        stem = stem[: -len(PLAN_STEM_SUFFIX)]
    short = binding_digest.split(":", 1)[-1][:12]
    return plan.with_name(f"{stem}.{short}.authorization.json")


def same_directory(left: str | Path, right: str | Path) -> bool:
    """Whether two paths name the same place: absolute, case-folded where the OS folds case.

    Used for a data directory and for an authorization file alike; nothing is resolved on disk.
    """
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


#: How many hex characters of the binding digest the approval code is.
APPROVAL_CODE_LENGTH = 8

#: Where a scripted flow is pointed instead: the hand-written artifact.
HAND_WRITTEN_ARTIFACT_DOCS = (
    'docs/operations.md, "Appendix: writing the artifact by hand"'
)


def approval_code(binding_digest: str) -> str:
    """The code a person types back to approve: the first 8 hex of the plan's binding digest.

    It is shown next to the summary, so it can only be typed by someone looking at what is being
    approved, and it differs for every binding - a reflexive ``y`` or ``no`` never matches.
    """
    return binding_digest.split(":", 1)[-1][:APPROVAL_CODE_LENGTH].lower()


def is_terminal(stream: TextIO | None) -> bool:
    """Whether ``stream`` is an interactive console. A missing or closed stream is not one."""
    if stream is None:
        return False
    try:
        return bool(stream.isatty()) and _console_handle(stream)
    except (AttributeError, OSError, ValueError):
        return False


def _console_handle(stream: TextIO) -> bool:
    """On Windows, whether ``stream``'s handle is a console - ``isatty`` alone is not enough.

    Windows answers ``isatty()`` true for every character device, the ``NUL`` device included
    (observed: ``python -c "import sys; print(sys.stdin.isatty())" < NUL`` prints ``True``), and
    ``NUL`` is what a scheduled task, a service or an agent's shell tool often hands a child as
    standard input. Only a console input handle answers ``GetConsoleMode``. Elsewhere, and for a
    stream with no operating-system handle (a stand-in that only says it is a terminal), the
    ``isatty`` answer stands.
    """
    if os.name != "nt":
        return True
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):  # io.UnsupportedOperation is both
        return True
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(descriptor)
    except OSError:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    get_console_mode = kernel32.GetConsoleMode
    get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_console_mode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(get_console_mode(handle, ctypes.byref(mode)))


def require_terminal(stdin: TextIO | None) -> None:
    """Refuse, without reading anything, unless standard input is an interactive console.

    ``approve`` is the step a person performs. Piped or redirected input is how a script - or the
    agent that wants the task to run - would feed it, so it is refused rather than read.
    """
    if not is_terminal(stdin):
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "`hflow approve` is for a person at a terminal, and standard input is not an "
            "interactive console (piped or redirected), so nothing was read and nothing was "
            "approved or written. Run it in a terminal and type the approval code and your "
            "approval yourself; a scripted flow writes the authorization file by hand instead "
            f"(see {HAND_WRITTEN_ARTIFACT_DOCS})",
        )


def ask_approval(stdin: TextIO, prompt: TextIO, *, code: str) -> str:
    """Ask for the approval code, then for the approval itself; return the approval text.

    Both lines are read from ``stdin`` and both prompts written to ``prompt`` - the stream the
    summary and the code were written to, so redirecting stdout never hides what is being
    approved. The code compares whitespace-trimmed and case-insensitively; anything else refuses
    before the second line is read. The approval is the person's own words, kept as typed (only
    surrounding whitespace is trimmed); an empty line refuses. Never an argument: a command line
    is what the process that wants to run could type for itself.
    """
    prompt.write(f"Type the approval code shown above ({len(code)} characters) to continue: ")
    prompt.flush()
    typed = stdin.readline().strip()
    if typed.lower() != code.lower():
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the approval code did not match, so nothing was approved and nothing was written. "
            "Re-run `hflow approve --plan` and type the code it shows once you have read the plan",
        )
    prompt.write("Approve this run in your own words (one line; empty cancels): ")
    prompt.flush()
    user_text = stdin.readline().strip()
    if not user_text:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "no approval text was given (empty input cancels); nothing was written",
        )
    return user_text


def approval_record(
    plan: RunPlan, user_text: str, *, authorization_id: str, authorized_at: str
) -> AuthorizationRecord:
    """The authorization artifact for ``plan``, carrying the user's own ``user_text``.

    Everything else is the plan's: the binding, the root ceilings and the submission count the
    preview said the run needs. ``origin`` is ``user_artifact`` because the text is the user's.
    """
    from .authorization import AuthorizationBinding, AuthorizationRecord

    if not user_text.strip():
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "no approval text was given; an empty note is not an approval. Nothing was written",
        )
    pending = plan.prepare_report.authorization
    try:
        record = AuthorizationRecord(
            authorization_id=authorization_id,
            provided_by="user",
            user_text=user_text,
            authorized_at=authorized_at,
            max_top_level_submissions=pending.max_top_level_submissions_required,
            binding=AuthorizationBinding.model_validate(plan.binding),
            root_limits=plan.root_limits,
            origin="user_artifact",
        )
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "this plan cannot be approved as it stands: "
            f"{summarize_validation_error(exc)}. Nothing was written",
        ) from exc
    if record.binding_digest() != plan.binding_digest:
        raise RefusedError(
            RefusalCode.INTERNAL_ERROR,
            "the authorization built from this plan digests to "
            f"{record.binding_digest()}, not the plan's {plan.binding_digest}. Nothing was written",
        )
    return record


def write_authorization(path: Path, record: AuthorizationRecord) -> Path:
    """Create ``path`` holding ``record``; an existing file is never overwritten.

    An approval is a record of what the user said. Replacing one in place would erase that
    statement (and a consumed approval's file), so a second approval needs a new file.
    """
    target = Path(os.path.abspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(indent=2) + "\n")
    except FileExistsError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{target} already exists and an authorization file is never overwritten. Nothing "
            "was written; pass --out with a new path, or use the existing approval",
        ) from exc
    return target
