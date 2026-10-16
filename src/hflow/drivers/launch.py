"""How a real acpx -> DSH launch is resolved, and the checks its spawn gate repeats.

Everything here decides *which programs* run, with what environment, and whether a launch may
start now; nothing here starts a process (``acpx_dsh`` does). ``prepare``, ``doctor`` and the
driver call the same functions, so the preview, the approval and the spawn agree.

Facts baked in from the M0 probe and the installed acpx 0.17.1 bundle (not assumptions):

* On Windows a raw agent command *string* is rejected; the agent must be given as a structured
  argv in acpx's config file, so the driver writes a per-invocation config instead of passing
  ``--agent``.
* ``dsh`` resolves to a ``.CMD`` shim, which ``CreateProcess`` cannot launch; the agent argv is
  ``[<SystemRoot>\\System32\\cmd.exe, /d, /c, <dsh>, --profile, acp]`` - one wrapper, absolute,
  so a ``cmd.exe`` in the workspace is never picked up, and ``/d`` so no registry AutoRun command
  runs before it; a shim path cmd.exe would reinterpret is refused (:func:`build_agent_argv`). acpx
  sees an ``.exe`` as the command and spawns it directly (its own batch-shell wrapping applies only
  to a ``.cmd``/``.bat`` command), so no second ``cmd.exe /c`` is added. The wrapper never carries
  task text, nonce, prompt or credentials - only the fixed launcher path and profile flag.
* acpx always loads ``<--cwd>/.acpxrc.json`` and lets it override the driver's config, agent argv
  included, with no opt-out; a workspace that has one is refused before spawn.
* DSH loads ``<cwd>/.env`` at launch into its own environment and its tool processes (documented
  upstream, not observed), so a workspace whose root holds a ``.env`` is refused before spawn too -
  by listing only, the file is never opened (:func:`workspace_root_entry`) - and so is a bound
  ``DSH_HOME`` that is relative, unresolvable, inside the workspace (or the user's checkout and its
  worktree directory) or around it: the agent could write what DSH loads from its home
  (:func:`dsh_home_workspace_problem`).
* Without a credential DSH fails with a no-API-key error before any model work (observed in M0),
  so a launch whose child environment lacks ``DEEPSEEK_API_KEY`` (by name) and whose DSH home
  holds neither ``.credentials.yaml`` nor ``.env`` (by stat; an unbound home is the empty
  per-invocation one) is refused before spawn too (user ruling 2026-10-07;
  :func:`credential_source_problem`). A presence check: it does not prove the credential is valid.
* The child environment is built in one place (:func:`child_environment`): secret-like variables
  other than ``DEEPSEEK_API_KEY`` are withheld (user ruling 2026-10-16), DSH's mode and home
  variables and Git's repository-locating ones are removed, and no bare program is looked up in
  the workspace.

Admission refuses the same workspace, home and credential causes before anything is reserved
(``prepare``). :func:`spawn_gate_refusal` checks them again - with the launch entry files'
content - at the last moment before the process exists, when the dispatch is already reserved.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from ..artifacts import is_secret_like
from ..contracts import AgentBinding, LaunchConfig, RefusalCode, RefusedError, launch_model
from ..gitworkspace import _REPOSITORY_LOCATING_ENV
from ..workspace import DSH_WORKSPACE_ENV_FILE
from .dsh_surfaces import (
    DEEPSEEK_API_KEY_NAME,
    DSH_NPM_PACKAGE,
    ENV_FILE_NAME,
    RECORDED_DSH_NPM_VERSION,
    STORED_CREDENTIALS_NAME,
    credential_sources,
    dsh_npm_bin_dir,
    dsh_npm_install_command,
    dsh_npm_prefix,
)
from .launch_content import bind_launch_content, final_path, launch_content_changes
from .primitives import (
    DSH_AGENTS_HOME_ENV,
    _client_module_tree,
    _path_forms,
    _same_or_inside,
    env_keys,
    find_in_directory,
    find_on_path,
    path_inside,
)


#: The driver a resolved launch is for (``LaunchConfig.driver_id``); ``acpx_dsh`` re-exports it.
DRIVER_ID = "acpx-dsh-acp"
ENV_ACPX_CLI = "HFLOW_ACPX_CLI"
ENV_ACPX_NODE = "HFLOW_ACPX_NODE"
#: Resolution order for the acpx entry point, most explicit first:
#:   1. ``$HFLOW_ACPX_CLI`` (operator intent; wins over everything);
#:   2. ``~/.hflow/`` sibling layout used by the repo-local development install;
#:   3. the M0 probe install inside this checkout.
#: No step installs, upgrades, or downloads anything.
DEV_ACPX_RELATIVE = Path("m0/acpx/node_modules/acpx/dist/cli.js")
PROBE_ACPX_RELATIVE = Path(".probe/acpx/node_modules/acpx/dist/cli.js")
#: DSH reads these at launch (sandbox mode and approval policy; tool set). They are removed from
#: every child environment and never set, so an ambient value cannot widen what a role may do.
STRIPPED_DSH_ENV = ("DSH_PERMISSION_MODE", "DSH_TOOLS_MODE")
#: Set to ``1`` in every child environment. cmd.exe and Node's process spawn (libuv) honour it:
#: a bare program name is then not searched for in the working directory - the workspace -
#: before PATH.
NO_CWD_EXE_SEARCH_ENV = "NoDefaultCurrentDirectoryInExePath"
#: acpx's project config file (read from ``<--cwd>``, no walk-up, no opt-out in 0.17.1; upstream
#: issue #835) and DSH's workspace env file (defined once, in ``hflow.workspace``). Why a launch
#: refuses a workspace holding either: see the module docstring.
WORKSPACE_CLIENT_CONFIG_NAME = ".acpxrc.json"
WORKSPACE_ENV_FILE_NAME = DSH_WORKSPACE_ENV_FILE
#: What a launch refusal found at the spawn gate - when the dispatch is already reserved, so the
#: run stays blocked - tells the operator to do once the cause is removed. Part of the refusal's
#: message, which the ledger stores.
_NEW_REVISION_STEP = (
    "submit a new revision of the task: an identical TaskSpec returns this blocked run, and under "
    "a root budget the new revision's first implementer counts as a repair, so it needs a repair "
    "attempt left on the root."
)


def _found_at_launch(starts_with: str) -> str:
    """The refusal sentence for a cause admission refuses too, found at the spawn gate instead."""
    return (
        f"(A run that starts {starts_with} is refused at admission, before anything is reserved; "
        "this one was found at launch, when the dispatch was already reserved, so the run stays "
        "blocked.)"
    )


#: Where ``GitRepo.worktree_parent`` puts a run's worktrees: ``<checkout>.hflow-worktrees`` beside
#: the checkout. The spawn gate uses it to name the user's checkout from a worktree's path.
WORKTREE_PARENT_SUFFIX = ".hflow-worktrees"
#: Where each invocation's files live, under the data directory: ``<data-dir>/invocations/<id>``.
INVOCATIONS_DIR_NAME = "invocations"
#: The invocation's own home inside that directory. USERPROFILE, HOME and APPDATA of the child
#: point here, so acpx's global config - and DSH's default home - are per invocation.
CHILD_HOME_DIR_NAME = "home"


class DriverSetupError(RuntimeError):
    """The driver cannot be used as configured. Fail loudly, never silently degrade."""


def _needs_batch_wrapper(dsh_executable: str) -> bool:
    """Whether the launcher is a Windows batch shim that ``CreateProcess`` cannot start itself."""
    return os.name == "nt" and dsh_executable.lower().endswith((".cmd", ".bat"))


def system_command_processor(env: Mapping[str, str]) -> Path | None:
    """The absolute ``<SystemRoot>\\System32\\cmd.exe`` named by ``env``, or ``None``.

    Never a bare ``cmd.exe``: the agent is started with the workspace as its cwd, and Node's
    process spawn (libuv) can resolve a bare name there before ``PATH``, so a ``cmd.exe`` at the
    worktree root could run as the launcher. The variable is looked up case-insensitively, as
    Windows does.
    """
    system_root = next((env[key] for key in env_keys(env, "SYSTEMROOT") if env[key]), "")
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "cmd.exe"
    return candidate if candidate.is_absolute() and candidate.is_file() else None


#: Where a launch found its ``dsh`` (:func:`resolve_dsh`), as its ``content_notes`` record it
#: (:data:`DSH_SOURCE_NOTE_PREFIX`) and ``prepare``/``doctor`` show it: ``dsh=<source>``.
DSH_SOURCE_PATH = "PATH"
DSH_SOURCE_DATA_DIR = "npm (data dir)"
DSH_SOURCE_EXPLICIT = "explicit"
DSH_SOURCE_NOTE_PREFIX = "dsh="


def resolve_dsh(data_dir: Path, env: Mapping[str, str]) -> tuple[str, str]:
    """``(absolute dsh, source)`` as a launch finds it, or ``("", "")``. Names and stat only.

    PATH first - what the operator put there is their choice - then exactly the npm CLI HFlow
    looks for under its data directory (``dsh_surfaces.dsh_npm_bin_dir``), by the same lookup
    rules. Nothing else: no other version, no Desktop shim, never a bare name.
    """
    found = find_on_path("dsh", env)
    if found:
        return found, DSH_SOURCE_PATH
    found = find_in_directory("dsh", dsh_npm_bin_dir(data_dir), env)
    return (found, DSH_SOURCE_DATA_DIR) if found else ("", "")


def _dsh_source_note(source: str, data_dir: Path) -> str:
    """The ``content_notes`` entry that records where the launch found its dsh."""
    why = {
        DSH_SOURCE_PATH: "the first absolute PATH entry that holds a dsh",
        DSH_SOURCE_DATA_DIR: (
            f"PATH has no dsh, so the {DSH_NPM_PACKAGE}@{RECORDED_DSH_NPM_VERSION} install under "
            f"the data directory ({dsh_npm_prefix(data_dir)}) is the launcher; its shim, carrier "
            "entry, package.json and the node it runs are bound by content like a PATH-found one"
        ),
        DSH_SOURCE_EXPLICIT: "named by the caller, not looked up",
    }[source]
    return f"{DSH_SOURCE_NOTE_PREFIX}{source}: {why}"


def dsh_not_found_problem(data_dir: Path) -> str:
    """Why a launch has no dsh, naming both places it looked and the npm install that fixes it."""
    return (
        "dsh was not found on PATH (only absolute PATH entries are searched, never the current "
        f"directory) or in {dsh_npm_bin_dir(data_dir)}, where HFlow looks for the npm CLI under "
        "its data directory. A bare name would be looked up in the workspace first, so it is "
        f"never a fallback. Install the npm CLI there - `{dsh_npm_install_command(data_dir)}` - "
        "or put a dsh on PATH."
    )


def _inside_any(program: str, roots: Sequence[Path]) -> Path | None:
    """The first of ``roots`` that contains ``program`` (:func:`path_inside`)."""
    return next((Path(root) for root in roots if path_inside(program, root)), None)


#: Characters cmd.exe may act on in a ``/c`` command line. Node quotes an argument by C runtime
#: rules (only when it holds a space, a tab or a quote), not by cmd's, so a batch launcher argument
#: holding one of them is refused. Observed with ``cmd /d /c <shim> --profile acp`` (Windows 11,
#: 2026-10-14): ``&`` splits the command, ``^`` and ``%VAR%`` are consumed, an unquoted ``(``, ``;``,
#: ``,`` or ``=`` cuts the path there, and an ``@`` in a quoted path makes ``/c`` drop the quotes
#: and cut it at its first space. ``| < > "`` cannot be in a Windows file name, ``!`` expands under
#: delayed expansion (a registry default ``/d`` does not turn off), and ``)`` closes a block. One
#: set, whatever the quoting: some of these were harmless in some positions, which is not a rule.
CMD_METACHARACTERS = frozenset('&|<>^%!"()@;,=')


def batch_argument_problem(argument: str) -> str:
    """Why ``argument`` cannot go on a ``cmd.exe /c`` command line unchanged, or ``""``."""
    found = sorted(set(argument) & CMD_METACHARACTERS)
    if not found:
        return ""
    return (
        f"{argument!r} holds {' '.join(found)}, which cmd.exe interprets in the batch shim's "
        "command line (Node quotes arguments by C runtime rules, not cmd's), so the agent "
        "command would not be the one approved; install the launcher under a path without them"
    )


def build_agent_argv(
    *,
    dsh_executable: str,
    profile: str,
    override: list[str] | None = None,
    command_processor: str = "",
) -> list[str]:
    """The launcher argv as a real command line, wrapped for the Windows batch shim.

    Only the launcher path and the fixed profile flag live here: no task text, no nonce, no
    user content, no credentials. A batch shim is wrapped in ``command_processor`` - the
    absolute ``cmd.exe`` from :func:`system_command_processor` - and refused without one. The
    wrapper runs ``/d``: cmd.exe's registry AutoRun commands would otherwise run first, in the
    workspace, and write into the ACP stdout stream. An argument cmd.exe would reinterpret
    (:func:`batch_argument_problem`) is refused.
    """
    if override is not None:
        return list(override)
    argv = [dsh_executable, "--profile", profile]
    if _needs_batch_wrapper(dsh_executable):
        if not command_processor or not Path(command_processor).is_absolute():
            raise DriverSetupError(
                f"{dsh_executable} is a batch shim and needs the absolute Windows command "
                "processor (<SystemRoot>\\System32\\cmd.exe), which was not found; a bare "
                "cmd.exe would be searched for in the workspace first"
            )
        for argument in argv:
            problem = batch_argument_problem(argument)
            if problem:
                raise DriverSetupError(f"the batch launcher argument {problem}")
        return [command_processor, "/d", "/c", *argv]
    return argv


def resolve_launch_config(
    *,
    data_dir: Path,
    profile: str = "acp",
    dsh_home: Path | None = None,
    acpx_cli: Path | None = None,
    dsh_executable: str | None = None,
    python_executable: str | None = None,
    node_executable: str | None = None,
    agent_argv_override: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    binding: AgentBinding | None = None,
    workspaces: Sequence[Path] = (),
) -> LaunchConfig:
    """Resolve every fact that decides *which programs* a real invocation launches.

    No process is started and no model is reachable from here: this is path resolution plus
    environment lookup, so ``prepare`` and ``doctor`` can call it for free. A missing client is
    reported (``resolvable=False``) rather than raised, because a preview that died before
    printing anything would be less useful than one that says which program is missing.

    This is the one place the launch is decided. The driver is later built *from* the returned
    object, so nothing can re-select a different interpreter after an approval was checked.

    Every program is an absolute path. ``dsh``, ``node`` and ``python`` are looked up in the
    absolute entries of PATH only (:func:`find_on_path`) - ``dsh`` then in exactly one more place,
    the npm CLI under the data directory (:func:`resolve_dsh`), and where it came from is recorded
    as the first content note - and an explicit program (an argument,
    ``HFLOW_ACPX_NODE``, the first word of ``agent_argv_override``) must already be absolute: acpx
    starts the agent with the workspace as its cwd, where a bare or relative name would be
    resolved - so a file in the worktree could run as the agent. A program that is not found is
    reported, never replaced by its bare name. ``workspaces`` are the directories the run's agent
    works in (the project root, the worktree parent); a launch program inside one of them - the
    launcher, ``dsh``, the client interpreter or the acpx entry it runs - is a file the agent
    can write, and is refused the same way, as is a workspace inside the ``node_modules`` tree
    the entry loads its modules from.

    ``binding`` is the role's binding; its ``model_selection`` becomes the launch's ``--model``
    value (none for ``native_profile``). It is validated again here, because a binding can be
    copied without validation, and an invalid value refuses rather than reaching an argv.
    """
    source = env if env is not None else os.environ
    try:
        model = launch_model(binding.model_selection) if binding is not None else ""
    except ValueError as exc:
        raise RefusedError(RefusalCode.INVALID_SPEC, str(exc)) from exc
    resolved_data_dir = Path(data_dir)
    entry = _resolve_client_entry(
        data_dir=resolved_data_dir, explicit=acpx_cli, env=source
    )
    if entry is not None:
        # Followed once, here: the path hashed by the content binding is the path spawned.
        entry = Path(final_path(str(entry)))
    problems: list[str] = []
    if entry is None:
        problems.append(
            "acpx CLI not found. Set HFLOW_ACPX_CLI to the acpx entry point, or install the "
            "project-local copy the M0 probe uses. This driver never installs or upgrades it "
            "silently."
        )

    def program(label: str, explicit: str | None, name: str, *, needed: bool) -> str:
        """An absolute program path, or ``""`` (with a problem recorded when it is needed).

        Links and junctions are followed once (:func:`launch_content.final_path`), so the file
        the content binding hashes is the file that is started.
        """
        if explicit:
            if Path(explicit).is_absolute():
                return final_path(explicit)
            if needed:
                problems.append(
                    f"{label} {explicit!r} is not an absolute path. A relative program is "
                    "resolved against the directory that starts it - for the agent, the "
                    "workspace - so it is never used."
                )
            return ""
        found = find_on_path(name, source)
        if not found and needed:
            problems.append(
                f"{name} was not found on PATH (only absolute PATH entries are searched, never "
                "the current directory). A bare name would be looked up in the workspace first, "
                "so it is never a fallback; put it on PATH or name its absolute path."
            )
        return final_path(found) if found else found

    suffix = entry.suffix.lower() if entry is not None else ""
    if dsh_executable:
        resolved_dsh = program("dsh", dsh_executable, "dsh", needed=agent_argv_override is None)
        dsh_source = DSH_SOURCE_EXPLICIT
    else:
        found_dsh, dsh_source = resolve_dsh(resolved_data_dir, source)
        resolved_dsh = final_path(found_dsh) if found_dsh else ""
        if not resolved_dsh and agent_argv_override is None:
            problems.append(dsh_not_found_problem(resolved_data_dir))
    resolved_python = program(
        "python", python_executable, "python", needed=suffix == ".py"
    )
    resolved_node = program(
        "node",
        node_executable or source.get(ENV_ACPX_NODE),
        "node",
        needed=suffix in {".js", ".mjs", ".cjs"},
    )
    # An explicit DSH home wins; otherwise the ambient one is *recorded*, because the child
    # inherits this process's environment and would use it.
    resolved_home = dsh_home or (Path(source["DSH_HOME"]) if source.get("DSH_HOME") else None)

    command_processor = system_command_processor(source)
    agent_argv: list[str] = []
    if agent_argv_override is not None and (
        not agent_argv_override or not Path(agent_argv_override[0]).is_absolute()
    ):
        problems.append(
            f"the agent launcher {agent_argv_override[:1]!r} is not an absolute path. A relative "
            "program is resolved in the workspace acpx starts it in, so it is never used."
        )
    elif agent_argv_override is not None or resolved_dsh:
        try:
            agent_argv = build_agent_argv(
                dsh_executable=resolved_dsh,
                profile=profile,
                override=agent_argv_override,
                command_processor=str(command_processor) if command_processor is not None else "",
            )
        except DriverSetupError as exc:
            # Reported, not raised, like a missing client: the launch has no argv it may use.
            problems.append(str(exc))
    client_prefix = _client_prefix_for(entry, node=resolved_node, python=resolved_python)
    if client_prefix and not client_prefix[0]:
        client_prefix = []  # its interpreter was not resolved; the problem is already recorded

    checked: set[str] = set()
    for label, launched in (
        ("the agent launcher", agent_argv[0] if agent_argv else ""),
        ("dsh", resolved_dsh if agent_argv_override is None else ""),
        ("the client interpreter", client_prefix[0] if client_prefix else ""),
        # The script that interpreter runs. Its package root (the parent of the node_modules
        # it lies in) contains it, so a package root inside a workspace is caught here too.
        ("the acpx client entry", str(entry) if entry is not None else ""),
    ):
        if not launched or launched in checked:
            continue
        checked.add(launched)
        inside = _inside_any(launched, workspaces)
        if inside is not None:
            problems.append(
                f"{label} {launched} lies inside the workspace {inside}: a file the agent can "
                "write must never run as a launch program."
            )
    module_tree = _client_module_tree(entry) if entry is not None else None
    if module_tree is not None:
        for workspace in workspaces:
            if _inside_any(str(workspace), [module_tree]) is not None:
                problems.append(
                    f"the workspace {workspace} lies inside {module_tree}, the node_modules tree "
                    f"the acpx client entry {entry} loads its modules from: a file the agent can "
                    "write must never run as part of a launch program."
                )
    resolvable = not problems
    detail = " ".join(problems)
    launch = LaunchConfig(
        driver_id=DRIVER_ID,
        harness="dsh",
        agent_argv=agent_argv,
        client_argv_prefix=client_prefix,
        client_entry=str(entry) if entry is not None else "",
        node=resolved_node,
        python=resolved_python,
        dsh_executable=resolved_dsh,
        profile=profile,
        dsh_home=str(resolved_home) if resolved_home is not None else "",
        model=model,
        resolvable=resolvable,
        detail=detail,
    )
    if not resolvable:
        return launch
    # H6: the launch entry files bound by content. A file that cannot be hashed, or a binding
    # that cannot name what it says it covers, makes the launch not resolvable - refused at
    # prepare, like a missing program.
    digests, content_problems, content_notes = bind_launch_content(launch, source)
    # The npm shim's node is known only once the carrier is classified; it is a launch program
    # like the others, so the same containment rule applies to it.
    for item in digests:
        inside = _inside_any(item.path, workspaces) if item.kind == "carrier_interpreter" else None
        if inside is not None:
            content_problems.append(
                f"the npm shim's node {item.path} lies inside the workspace {inside}: a file the "
                "agent can write must never run as a launch program."
            )
    if content_problems:
        return launch.model_copy(
            update={"resolvable": False, "detail": " ".join(content_problems)}
        )
    if resolved_dsh and resolved_dsh in agent_argv:
        # Recorded where prepare and the probe already show the launch's binding notes; outside
        # the effective-configuration digest, like every content note (the path itself is in it).
        content_notes = [_dsh_source_note(dsh_source, resolved_data_dir), *content_notes]
    return launch.model_copy(
        update={"content_digests": digests, "content_notes": content_notes}
    )


def _resolve_client_entry(
    *, data_dir: Path, explicit: Path | None, env: Mapping[str, str]
) -> Path | None:
    """Which acpx entry point this machine has, or ``None``. Never installs anything."""
    if explicit is not None:
        candidate = Path(explicit)
        return candidate if candidate.exists() else None
    override = env.get(ENV_ACPX_CLI)
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None
    repo_root = Path(__file__).resolve().parents[3]
    for candidate in (data_dir / DEV_ACPX_RELATIVE, repo_root / PROBE_ACPX_RELATIVE):
        if candidate.exists():
            return candidate
    return None


def _client_prefix_for(entry: Path | None, *, node: str, python: str) -> list[str]:
    """Interpreter prefix for the client entry point, chosen by its kind."""
    if entry is None:
        return []
    suffix = entry.suffix.lower()
    if suffix in {".js", ".mjs", ".cjs"}:
        return [node]
    if suffix == ".py":
        return [python, "-u"]
    return []


def child_home_for(data_dir: Path, invocation_id: str) -> Path:
    """The home directory one invocation's child gets (USERPROFILE, HOME, APPDATA's parent)."""
    return Path(data_dir) / INVOCATIONS_DIR_NAME / invocation_id / CHILD_HOME_DIR_NAME


def effective_dsh_home(
    launch: LaunchConfig, *, child_home: Path
) -> tuple[Literal["bound", "per_invocation"], Path]:
    """The DSH home a child of this launch uses, and how it comes to use it.

    A bound ``DSH_HOME`` is set on every child, so it is that path. With none bound the driver
    removes ``DSH_HOME`` from the child and points USERPROFILE/HOME at ``child_home``; DSH's
    upstream resolution is explicit > non-empty ``DSH_HOME`` > ``~/.dsh``, so its home is then
    ``child_home/.dsh``, created empty for each invocation. That second case is inferred from
    upstream source (defaultDshHome joins os.homedir(), which reads USERPROFILE first on
    Windows); not observed. Nothing is created or resolved here: ``child_home`` may contain
    ``primitives.INVOCATION_ID_PLACEHOLDER``.
    """
    if launch.dsh_home:
        return "bound", Path(launch.dsh_home)
    return "per_invocation", Path(child_home) / ".dsh"


#: The one secret-like variable an agent child keeps: the credential DSH needs (user ruling
#: 2026-10-16). Every other name ``artifacts.is_secret_like`` matches is withheld.
AGENT_CREDENTIAL_ENV = DEEPSEEK_API_KEY_NAME


class ChildEnvironment(dict[str, str]):
    """An agent child's environment, and the secret-like names :func:`child_environment` withheld.

    A plain mapping to everything that uses it - the spawn, the spawn gate's credential check, and
    ``prepare``'s and ``doctor``'s previews - so they all judge the same environment.
    ``withheld_secret_like`` (sorted names, never values) is what the launch record reports
    (``dsh_surfaces.observe_launch_surfaces``).
    """

    withheld_secret_like: tuple[str, ...] = ()


def _is_agent_credential(name: str) -> bool:
    """``name`` is :data:`AGENT_CREDENTIAL_ENV` as the OS reads it (any case on Windows)."""
    if os.name == "nt":
        return name.upper() == AGENT_CREDENTIAL_ENV
    return name == AGENT_CREDENTIAL_ENV


def child_environment(
    source: Mapping[str, str], *, extra_env: Mapping[str, str], dsh_home: str
) -> ChildEnvironment:
    """The environment a child process is started with, built from ``source``.

    Every variable whose name looks like a credential (``artifacts.is_secret_like``, the fragments
    the check environment uses) is withheld - from ``source`` and ``extra_env`` alike - except
    ``DEEPSEEK_API_KEY`` (:data:`AGENT_CREDENTIAL_ENV`), the credential DSH itself needs, which the
    ``no_credential_source`` check looks for among the child's names (user ruling 2026-10-16). A
    denylist, not an allowlist: proxy variables and everything else pass as before. The withheld
    names are kept on the result (:class:`ChildEnvironment`) for the launch record. This is not a
    credential sandbox: a credential under another name, or in a file the agent can read, still
    reaches it.

    ``DSH_HOME`` is part of the *resolved launch*, so it is set from the bound value or
    removed - never inherited by accident. Copying the ambient environment and only
    overriding a non-empty bound home would leave an unset variable unset, and a
    ``DSH_HOME`` added *after* the launch was resolved would then still reach the child:
    the approval would say one thing and the process would do another. ``extra_env`` is
    subject to the same rule, because it is not a way to smuggle a different launch past
    the binding.

    ``DSH_PERMISSION_MODE`` and ``DSH_TOOLS_MODE`` are removed and never set: DSH reads its
    sandbox mode, approval policy and tool set from them at launch, so an ambient
    ``danger-full-access`` would silently unconfine every role. Choosing a value per role is
    not this driver's decision; inheriting one by accident is ruled out here.

    ``DSH_AGENTS_HOME`` is removed and never set (:data:`DSH_AGENTS_HOME_ENV`): an inherited one
    would point DSH at an instruction file outside the invocation's own home that no approval
    binds. Without it DSH falls back to ``~/.agents/AGENTS.md`` under the child's home - the
    per-invocation home the driver points USERPROFILE/HOME at, created empty.

    The Git variables that locate a repository (``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``
    and the rest of ``gitworkspace._REPOSITORY_LOCATING_ENV``) are removed too, as HFlow removes
    them from its own git calls: inherited, they would point DSH's git and the agent's shell at
    another repository or index - the user's checkout, say - instead of the workspace's.

    These DSH and Git variables are removed in every spelling on Windows, which reads variable
    names case-insensitively (:func:`env_keys`), and by their exact name elsewhere; a bound
    ``DSH_HOME`` is then set in exactly that spelling.

    ``NoDefaultCurrentDirectoryInExePath=1`` is always set, in exactly that spelling, after
    removing every other spelling: acpx starts the agent with the workspace as its cwd, and
    without it both cmd.exe (the npm DSH batch shim runs a bare ``node`` unless a node.exe sits
    beside it - the one it finds is bound by content, ``carrier_interpreter``; the Desktop shim runs
    an absolute ``DeepSeek Harness.exe``) and Node's spawn look in that cwd before PATH - a
    ``node.cmd`` the implementer wrote would then run as the reviewer's agent. For the same reason
    a relative PATH entry, which would be resolved against the workspace, is not passed on.
    """
    env = ChildEnvironment(source)
    env.update(extra_env)
    for name in ("DSH_HOME", *STRIPPED_DSH_ENV, DSH_AGENTS_HOME_ENV, *_REPOSITORY_LOCATING_ENV):
        for key in env_keys(env, name) if os.name == "nt" else [name]:
            env.pop(key, None)
    withheld = sorted(key for key in env if is_secret_like(key) and not _is_agent_credential(key))
    for key in withheld:
        del env[key]
    env.withheld_secret_like = tuple(withheld)
    if dsh_home:
        env["DSH_HOME"] = dsh_home
    for key in env_keys(env, NO_CWD_EXE_SEARCH_ENV):
        del env[key]
    env[NO_CWD_EXE_SEARCH_ENV] = "1"
    for key in env_keys(env, "PATH"):
        env[key] = os.pathsep.join(
            entry
            for entry in env[key].split(os.pathsep)
            if entry.strip() and Path(entry.strip().strip('"')).is_absolute()
        )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _workspace_client_config(workspace: Path) -> Path | None:
    """:func:`workspace_root_entry` for acpx's project config (kept for ``prepare``'s import)."""
    return workspace_root_entry(workspace, WORKSPACE_CLIENT_CONFIG_NAME)


def _workspace_env_file(workspace: Path) -> Path | None:
    """:func:`workspace_root_entry` for DSH's ``.env`` (kept for ``prepare``'s import)."""
    return workspace_root_entry(workspace, WORKSPACE_ENV_FILE_NAME)


def workspace_root_entry(workspace: Path, file_name: str) -> Path | None:
    """The workspace root's entry named ``file_name`` (any letter case) if one exists, else ``None``.

    ``lstat`` rather than ``exists``: a file, a directory, a broken link or a junction all count,
    because what acpx would make of any of them is not something to reason about here. The name
    is matched without regard to case, on every filesystem: where the filesystem ignores case
    (NTFS by default), acpx's open of ``.acpxrc.json`` finds ``.ACPXRC.JSON`` too, and refusing
    a lookalike elsewhere is the safe side. The returned path carries the spelling found on disk.
    Only the workspace root's own entries count; acpx does not walk into subdirectories (nor
    does DSH for its ``.env``). Nothing is opened: a listing and at most one ``lstat``.
    """
    candidate = workspace / file_name
    try:
        names = os.listdir(workspace)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        # Any other failure (access denied, for one) does not show the entry is absent.
        return candidate
    wanted = file_name.casefold()
    for name in names:
        if name.casefold() == wanted:
            return workspace / name
    try:
        # The listing missed it; a direct lookup is the open the client itself would make.
        os.lstat(candidate)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return candidate
    return candidate


def dsh_home_workspace_problem(dsh_home: str, workspaces: Sequence[Path]) -> str:
    """Why a bound ``DSH_HOME`` may not be used with these directories, or ``""``.

    DSH loads its patch layers (which can replace the sandbox and approval rows), ``AGENTS.md``,
    skills and ``.env`` from its home, and never checks where that home is (documented at dsh
    5badb150, not observed). A home the agent can write is therefore refused: one that equals or
    lies inside any of ``workspaces`` (the agent's cwd, the user's checkout, the worktree
    directory) - and one that contains any of them, which puts ``$DSH_HOME/AGENTS.md`` and the
    home ``.env`` on the project's ancestry. Both the path as written and its links resolved are
    compared, case-folded, and either one inside refuses: DSH compares the string it was given,
    while a link or junction can point somewhere else. A path that cannot be resolved refuses.

    A relative value is refused before anything is resolved: DSH would resolve it against the
    directory it starts in - the workspace - and whether DSH expands a leading ``~`` is not
    verified, so a ``~`` value is refused as relative too (``Path.is_absolute`` is ``False`` for
    it). ``dsh_home`` empty (unbound) is no problem: the driver then gives each invocation its own
    home under the data directory. Read-only: path resolution only, nothing is opened or created.
    """
    if not dsh_home:
        return ""
    if not Path(dsh_home).is_absolute():
        return (
            f"DSH_HOME {dsh_home!r} is not an absolute path: DSH would resolve it against the "
            "directory it starts in - the workspace - and whether it expands a leading '~' is "
            "not verified"
        )
    try:
        home_forms = _path_forms(dsh_home)
    except (OSError, ValueError) as exc:
        return (
            f"DSH_HOME {dsh_home} could not be resolved ({type(exc).__name__}: {exc}), so "
            "whether it lies inside the workspace is unknown"
        )
    for workspace in workspaces:
        try:
            root_forms = _path_forms(workspace)
        except (OSError, ValueError) as exc:
            return (
                f"the workspace {workspace} could not be resolved ({type(exc).__name__}: "
                f"{exc}), so whether DSH_HOME {dsh_home} lies inside it is unknown"
            )
        if any(_same_or_inside(home, root) for home in home_forms for root in root_forms):
            return (
                f"DSH_HOME {dsh_home} is, or lies inside, {workspace}: the agent can write "
                "there, and DSH loads its patch layers, AGENTS.md, skills and .env from its "
                "home at the next launch"
            )
        if any(_same_or_inside(root, home) for home in home_forms for root in root_forms):
            return (
                f"{workspace} lies inside DSH_HOME {dsh_home}: the home's AGENTS.md and .env "
                "would sit on the project's ancestry, inside what the agent works in"
            )
    return ""


def credential_source_problem(
    child_env: Mapping[str, str],
    *,
    dsh_home_kind: Literal["bound", "per_invocation"],
    dsh_home: Path,
) -> str:
    """Why DSH started with ``child_env`` and this home would have no credential source, or ``""``.

    User ruling 2026-10-07, "refuse before dispatch". DSH's credential precedence (documented at
    dsh-v0.2.0-rc.2, not observed) is the launch environment, then ``$DSH_HOME/.credentials.yaml``,
    then ``<cwd>/.env``, then ``$DSH_HOME/.env``. A real launch never starts on a workspace
    ``.env`` (``workspace_env_file``), so what is left is ``DEEPSEEK_API_KEY`` among the child's
    variable *names* and, for a bound home, a regular file by either home name - the facts
    ``dsh_surfaces.credential_sources`` reports, by name and stat only (nothing is opened, sized
    or hashed). With ``DSH_HOME`` unbound (``dsh_home_kind="per_invocation"``) the home is the
    per-invocation one, created empty, so only the environment counts.

    A presence check, not a validity check: an empty or revoked key, or a home file that holds no
    credential, passes. What it rules out is the launch M0 observed fail with a no-API-key error
    before any model work - which HFlow would record as ``outcome_unknown``, with the approved
    submission spent and a ledger entry left for ``hflow ledger settle``.
    """
    sources = credential_sources(child_env, dsh_home_kind=dsh_home_kind, dsh_home=dsh_home)
    if sources.visible:
        return ""
    key = (
        f"{DEEPSEEK_API_KEY_NAME} is not in the launch environment (its name was looked for; no "
        "value is ever read)"
    )
    if sources.dsh_home_kind == "per_invocation":
        home = (
            f"DSH_HOME is unbound, so DSH's home is the per-invocation {sources.dsh_home}, created "
            "empty for each invocation (inferred from upstream source, not observed)"
        )
    else:
        listed = ", ".join(f"{name} {state}" for name, state in sources.home_files)
        home = (
            f"the bound DSH home {sources.dsh_home} holds neither {STORED_CREDENTIALS_NAME} nor "
            f"{ENV_FILE_NAME} as a regular file ({listed}; stat only, never opened)"
        )
    return (
        f"no credential source is visible to DSH: {key}, and {home}; a workspace "
        f"{ENV_FILE_NAME} is never launched on (workspace_env_file)"
    )


def spawn_workspaces(workspace: Path) -> list[Path]:
    """The directories a launch in ``workspace`` must keep a bound DSH home out of.

    The workspace itself, and - when it is a run's worktree (``<checkout>.hflow-worktrees/<id>``,
    :data:`WORKTREE_PARENT_SUFFIX`) - that worktree directory and the user's checkout it was named
    after. The checkout is derived from the name alone; admission checks the real project root.
    """
    absolute = Path(os.path.abspath(workspace))
    roots = [absolute]
    parent = absolute.parent
    suffix = WORKTREE_PARENT_SUFFIX.casefold()
    if parent.name.casefold().endswith(suffix) and len(parent.name) > len(suffix):
        roots.append(parent)
        roots.append(parent.parent / parent.name[: -len(suffix)])
    return roots


def launch_content_refusal(launch: LaunchConfig, env: Mapping[str, str]) -> str:
    """Why the launch entry files no longer match the launch's recorded content, or ``""``.

    ``env`` is the environment the child is about to get; the npm carrier's ``node`` is looked up
    in it again. Hashes again (node.exe is ~90 MB, ~60 ms warm). Anything unexpected while
    checking is a refusal too: the check fails closed. A file swapped after this check and before
    ``CreateProcess`` is not caught - Windows offers no exec-by-handle.
    """
    try:
        changes = launch_content_changes(launch, env=env)
    except Exception as exc:  # noqa: BLE001 - an unchecked launch is not started
        changes = [f"the content check itself failed: {type(exc).__name__}: {exc}"]
    if not changes:
        return ""
    return (
        "the launch entry files no longer match the content bound when the launch was "
        "resolved: " + "; ".join(changes) + "."
    )


def spawn_gate_refusal(
    launch: LaunchConfig, *, workspace: Path, env: Mapping[str, str], child_home: Path
) -> tuple[RefusalCode, str] | None:
    """Why this launch may not start now, judged at the last moment before the process exists.

    Checked for every invocation and both roles (the reviewer runs on the worktree the
    implementer wrote), on the environment the child is about to get and the home it would use,
    so a cause that appears after admission - a file in the workspace, a removed variable, a
    deleted home file, a changed launch file - is caught here. Admission refuses the same causes
    before anything is reserved (``prepare.start_workspace_client_config``,
    ``start_workspace_env_file``, ``launch_dsh_home_problem``, ``launch_credential_problem``); one
    found here was found when the dispatch was already reserved, so each message names the step
    that works then. Names, listings and stat only, except the content check, which hashes the
    launch entry files again.
    """
    client_config = workspace_root_entry(workspace, WORKSPACE_CLIENT_CONFIG_NAME)
    if client_config is not None:
        return (
            RefusalCode.WORKSPACE_CLIENT_CONFIG,
            f"{client_config} exists in the workspace. acpx always loads it and lets "
            "it override HFlow's launch, including the agent command, so no client "
            f"process was started. {_found_at_launch('with one')} Remove the file (or "
            "directory), then " + _NEW_REVISION_STEP,
        )
    env_file = workspace_root_entry(workspace, WORKSPACE_ENV_FILE_NAME)
    if env_file is not None:
        return (
            RefusalCode.WORKSPACE_ENV_FILE,
            f"{env_file} exists in the workspace. DSH loads <cwd>/.env at launch "
            "into its own environment and its tool processes (documented upstream, "
            "not observed); HFlow never opens it, so no client process was started. "
            f"{_found_at_launch('with one')} Remove or rename the file (or directory), "
            "then " + _NEW_REVISION_STEP,
        )
    home_problem = dsh_home_workspace_problem(launch.dsh_home, spawn_workspaces(workspace))
    if home_problem:
        return (
            RefusalCode.DSH_HOME_IN_WORKSPACE,
            f"{home_problem}. No client process was started. Point DSH_HOME at an "
            "absolute directory outside the project, its worktrees and this "
            "workspace (or unset it, so each invocation gets its own home), then "
            "prepare again: DSH_HOME is part of the approved launch, so a real run "
            "needs an authorization for the new binding, and since this dispatch "
            "was already reserved the run stays blocked - submit a new revision.",
        )
    home_kind, dsh_home = effective_dsh_home(launch, child_home=child_home)
    credential_problem = credential_source_problem(env, dsh_home_kind=home_kind, dsh_home=dsh_home)
    if credential_problem:
        return (
            RefusalCode.NO_CREDENTIAL_SOURCE,
            f"{credential_problem}. No client process was started: without a "
            "credential DSH fails with a no-API-key error before any model work "
            f"(observed in M0). {_found_at_launch('without one')} Make "
            "a credential visible - DEEPSEEK_API_KEY in the environment hflow runs "
            "in, or a .credentials.yaml or .env in a bound DSH home - then "
            + _NEW_REVISION_STEP
            + " This is a presence check: it does not prove a credential is valid.",
        )
    content_refusal = launch_content_refusal(launch, env)
    if content_refusal:
        return (
            RefusalCode.LAUNCH_CONTENT_CHANGED,
            f"{content_refusal} No client process was started; the run stays "
            "blocked. Run `hflow prepare` again to see the files' current digests and "
            "re-issue the authorization for them.",
        )
    return None
