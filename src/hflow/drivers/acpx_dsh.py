"""The single thin production Driver: acpx -> official DSH ACP.

Scope of this module: launch, stdin/stdout framing, neutral event projection, process
boundary, stop, and conservative reconcile. It does **not** decide whether a task is
accepted - admission, budget, attempt, evidence, review and ``ResultReceipt`` stay in the
controller.

Facts baked in from the M0 probe and the installed acpx 0.17.1 bundle (not assumptions):

* On Windows a raw agent command *string* is rejected; the agent must be given as a
  structured argv in acpx's config file, so the driver writes a per-invocation config
  instead of passing ``--agent``.
* ``dsh`` resolves to a ``.CMD`` shim, which ``CreateProcess`` cannot launch; the agent argv
  is ``[<SystemRoot>\\System32\\cmd.exe, /c, <dsh>, --profile, acp]`` - one wrapper, absolute,
  so a ``cmd.exe`` in the workspace is never picked up. acpx sees an ``.exe`` as the command
  and spawns it directly (its own batch-shell wrapping applies only to a ``.cmd``/``.bat``
  command), so no second ``cmd.exe /c`` is added. The wrapper never carries task text, nonce,
  prompt or credentials - only the fixed launcher path and profile flag.
* acpx always loads ``<--cwd>/.acpxrc.json`` and lets it override this driver's config,
  agent argv included, with no opt-out; a workspace that has one is refused before spawn.
* DSH loads ``<cwd>/.env`` at launch (documented upstream, not observed), so a workspace whose
  root holds a ``.env`` is refused before spawn too - by listing only, the file is never opened
  - and so is a bound ``DSH_HOME`` that is relative, unresolvable, inside the workspace (or the
  user's checkout and its worktree directory) or around it: the agent could write what DSH
  loads from its home.
* The task body goes through acpx's documented stdin path (``-f -``), written from its own
  thread after both output readers started, then the child's stdin is closed so input is
  complete. The ACP stdin between acpx and DSH is acpx's own
  pipe and is never touched from here.
* The body is the controller's rendered input packet, verbatim (``hflow.packet``). This
  driver adds no task facts of its own: it does not read the repository to fill a gap, and
  when the request carries no packet it transports the bare ``goal`` inside a digest
  envelope. The digest of the text it was handed is reported back in the invocation result,
  so a driver that sends something other than the packet is caught. That digest is a local
  record of the input, not an acknowledgement from the agent or the model.
* A profile's model is passed as acpx's global ``--model <value>`` flag, the validated value
  verbatim (``LaunchConfig.model``). acpx applies it after ``session/new`` with
  ``session/set_config_option`` and before ``session/prompt``, and refuses a value the agent
  did not advertise without sending the prompt. ``native_profile`` passes no flag.
* ``acpx cancel`` reaches a *queue owner* for a persisted session, which one-shot ``exec``
  does not have. ``exec`` itself sends ``session/cancel`` for the active prompt when its own
  process receives SIGINT, SIGTERM or SIGHUP - but HFlow cannot deliver those to the client:
  it is started with ``CREATE_NEW_PROCESS_GROUP`` (Ctrl+C is disabled for that group), a
  Ctrl+Break arrives in Node as SIGBREAK, which acpx does not handle, and Windows has no
  external SIGTERM/SIGHUP. M0 observed a CTRL_BREAK killing the client (exit ``0xC000013A``)
  before any cancel reached the agent. The driver therefore records the ``cancel``
  capability as ``unsupported`` for this launcher rather than pretending.
* ``stopReason=end_turn`` is turn settlement, not success: DSH settles blocked and aborted
  turns as ``end_turn`` too (documented, not observed). It is reported as ``completed``;
  acceptance is decided by the controller's checks and review, never here.

Stopping therefore has one honest mechanism here: close the managed process boundary after
a bounded grace period. That is reported as ``mechanism="forced"`` and never as a
successful protocol cancellation, and a stop that cannot be confirmed stays
``still_running``/``unknown``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple

from ..contracts import (
    AgentBinding,
    CancellationReceipt,
    CapabilityReport,
    CapabilityState,
    DriverHandle,
    EventKind,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    LaunchConfig,
    LaunchSurfaces,
    ModelApplied,
    ModelChange,
    ModelObservation,
    NormalizedEvent,
    ReconcileOutcome,
    ReconcileResult,
    RefusalCode,
    RefusedError,
    SpawnFact,
    SpawnKind,
    StreamOrder,
    ReviewOutput,
    launch_model,
)
from ..ids import utc_now
from ..artifacts import BoundedTextSink, StreamCapture
from ..packet import packet_digest
from ..paths import ENV_ALLOW_WRITES  # noqa: F401 - re-exported for existing importers
from ..review import (
    MAX_ANSWER_BYTES,
    REVIEW_AMBIGUOUS,
    REVIEW_INVALID,
    REVIEW_MISSING,
    AnswerTranscript,
    ReviewDecodeError,
    decode_review,
)
from .acp_events import V1_STOP_REASONS, project_line
from .winjob import ProcessBoundary, close_output_handles, popen_in_boundary, process_gone

DRIVER_ID = "acpx-dsh-acp"
DRIVER_VERSION = "0.1.0"

ENV_ACPX_CLI = "HFLOW_ACPX_CLI"
ENV_ACPX_NODE = "HFLOW_ACPX_NODE"
#: Opt-in to file writes for a real invocation. Defined once in ``hflow.paths`` (the admission
#: gate, the resolution path and the controller all have to agree on the name) and re-exported
#: here for the callers that already import it from this module.

#: Resolution order for the acpx entry point, most explicit first:
#:   1. ``$HFLOW_ACPX_CLI`` (operator intent; wins over everything);
#:   2. ``~/.hflow/`` sibling layout used by the repo-local development install;
#:   3. the M0 probe install inside this checkout.
#: No step installs, upgrades, or downloads anything.
DEV_ACPX_RELATIVE = Path("m0/acpx/node_modules/acpx/dist/cli.js")
PROBE_ACPX_RELATIVE = Path(".probe/acpx/node_modules/acpx/dist/cli.js")
#: Cap on the retained raw log for one invocation (plan 8.2). Everything the client writes is
#: read and digested; only this prefix is kept, and the difference is recorded as truncation.
MAX_RAW_LOG_BYTES = 32 * 1024 * 1024
#: Cap on retained lines: the log file is the retained record, memory only needs recent context.
MAX_BUFFERED_LINES = 5000
#: Cap on a single line still waiting for its newline. A line longer than this cannot be a
#: protocol message, and letting it grow would make the memory bound nominal rather than real.
MAX_PENDING_LINE_BYTES = 1024 * 1024
#: How much is read from a child pipe at a time.
CAPTURE_READ_CHUNK = 64 * 1024
#: How often the stream follower re-checks a file that has not grown yet.
STREAM_POLL_SECONDS = 0.05
#: How long ``collect`` waits for the reader to finish after the client exits.
STREAM_DRAIN_TIMEOUT_SECONDS = 5.0
#: Bounded wait before the boundary is terminated anyway. Nothing asks the client to stop in
#: between - no cancel signal can be delivered to it (see the module docstring).
FORCE_STOP_GRACE_SECONDS = 2.0
#: How long to wait for the boundary to report itself empty after termination.
BOUNDARY_EMPTY_TIMEOUT_SECONDS = 10.0
#: How long ``release`` waits for each reader thread before leaving it (and its pipe) alone.
RELEASE_JOIN_SECONDS = 2.0
#: How long ``collect`` waits, once the client's tree is gone, for the stdin writer to finish.
#: With the tree gone the pipe has no reader left, so the writer's blocked write fails at once.
STDIN_WRITER_JOIN_SECONDS = 2.0
#: How much of the prompt the stdin writer hands to the pipe per write, so what was accepted
#: before a failure can be reported.
STDIN_WRITE_CHUNK = 64 * 1024
#: DSH reads these at launch (sandbox mode and approval policy; tool set). They are removed from
#: every child environment and never set, so an ambient value cannot widen what a role may do.
STRIPPED_DSH_ENV = ("DSH_PERMISSION_MODE", "DSH_TOOLS_MODE")
#: Set to ``1`` in every child environment. cmd.exe and Node's process spawn (libuv) honour it:
#: a bare program name is then not searched for in the working directory - the workspace -
#: before PATH.
NO_CWD_EXE_SEARCH_ENV = "NoDefaultCurrentDirectoryInExePath"
#: acpx's project config file name. acpx always reads ``<--cwd>/.acpxrc.json`` (no walk-up, no
#: opt-out in 0.17.1; upstream issue #835) and lets it override the global config this driver
#: writes - including the agent argv, which no CLI flag can override on Windows.
WORKSPACE_CLIENT_CONFIG_NAME = ".acpxrc.json"
#: DSH's workspace env file. DSH's app boot reads ``<cwd>/.env`` and applies what it sets to its
#: own environment and its tool processes (documented at dsh 5badb150, not observed). A real
#: launch refuses a workspace whose root holds an entry by this name; it is never opened.
WORKSPACE_ENV_FILE_NAME = ".env"
#: Where ``GitRepo.worktree_parent`` puts a run's worktrees: ``<checkout>.hflow-worktrees`` beside
#: the checkout. The spawn gate uses it to name the user's checkout from a worktree's path.
WORKTREE_PARENT_SUFFIX = ".hflow-worktrees"
#: Written around a packet so the prompt text a driver actually sent is recoverable, and so a
#: bare ``goal`` (a direct driver call) is still traceable to the request it came from. The
#: digest covers the prompt text alone, so this envelope cannot change what the digest means.
PROMPT_ENVELOPE = "HFLOW-PROMPT-DIGEST"
#: How much of a JSON-RPC error message answering the prompt is kept: it is agent text, quoted
#: (repr) in error_message, a limitation and block_reason.
MAX_PROMPT_ERROR_MESSAGE_CHARS = 500
#: Where each invocation's files live, under the data directory: ``<data-dir>/invocations/<id>``.
INVOCATIONS_DIR_NAME = "invocations"
#: The invocation's own home inside that directory. USERPROFILE, HOME and APPDATA of the child
#: point here, so acpx's global config - and DSH's default home - are per invocation.
CHILD_HOME_DIR_NAME = "home"
#: Stands in for an invocation id that does not exist yet (prepare, doctor, probe). A path that
#: contains it is a name, never a directory: nothing resolves or creates it.
INVOCATION_ID_PLACEHOLDER = "<invocation-id>"


def effective_prompt(request: InvocationRequest) -> str:
    """The exact prompt text to send for one invocation.

    The controller renders the input packet (``hflow.packet``) and puts it in the request;
    the driver only transports it. An empty packet means the caller passed a bare ``goal``,
    which is wrapped in the digest envelope so the sent prompt is still bound to its request.
    """
    return request.packet or (
        f"{PROMPT_ENVELOPE}: {packet_digest(request.goal)}\n\n{request.goal}"
    )


class DriverSetupError(RuntimeError):
    """The driver cannot be used as configured. Fail loudly, never silently degrade."""


class ExitBoundary(NamedTuple):
    """What an invocation's boundary held once its client was gone.

    Recorded when the boundary is closed, because a closed job cannot be asked again: from then
    on this is the only answer, and "nothing can be seen" must not be read as "nothing is there".
    """

    #: The boundary was confirmed empty.
    emptied: bool
    #: Processes still in the boundary after the client exited (``None``: the job could not say).
    left_behind: int | None
    detail: str


class PromptErrorResponse(NamedTuple):
    """A JSON-RPC error response carrying an observed ``session/prompt`` request id."""

    #: The id exactly as the stream carried it.
    request_id: Any
    #: ``error.code`` when it is an int (not a bool), else ``None``.
    code: int | None
    #: ``error.message`` cut to MAX_PROMPT_ERROR_MESSAGE_CHARS; "" when absent or not a string.
    message: str
    #: Method of a request other than the prompt that carried the same id after the prompt was sent
    #: ("" when none did). The agent numbers its own requests from 0 too (ACP SDK), so the error may
    #: answer that request and is not attributed.
    reused_by: str

    @property
    def code_text(self) -> str:
        return "(no integer code)" if self.code is None else str(self.code)


class _TerminalResponse(NamedTuple):
    """One response that carried a ``stopReason``, and where in the stream it arrived."""

    request_id: Any
    stop_reason: Any
    #: Stream index of its line (see ``AcpxDshDriver._line_counts``).
    line_index: int
    #: The session the latest ``session/prompt`` named when it arrived (``None``: no prompt yet),
    #: and that session's update counts at that moment, so what came after it can be counted at
    #: the end without keeping every update.
    session_id: str | None
    updates_before: int
    message_chunks_before: int


def _session_of(params: dict[str, Any]) -> str:
    """The ``sessionId`` a message's params name, or ``""`` when they name none."""
    session = params.get("sessionId")
    return session if isinstance(session, str) else ""


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
    system_root = next(
        (value for key, value in env.items() if key.upper() == "SYSTEMROOT" and value), ""
    )
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "cmd.exe"
    return candidate if candidate.is_absolute() and candidate.is_file() else None


def _env_lookup(env: Mapping[str, str], name: str) -> str:
    """``env[name]``, matched case-insensitively on Windows as the OS does; ``""`` when unset."""
    if os.name != "nt":
        return env.get(name, "")
    return next((value for key, value in env.items() if key.upper() == name.upper()), "")


def find_on_path(name: str, env: Mapping[str, str]) -> str:
    """The absolute file that PATH lookup finds for ``name``, or ``""``.

    Only the absolute entries of ``env``'s PATH are searched - never the current directory and
    never a relative entry. ``shutil.which`` cannot be used: on Windows it searches the current
    directory first, even when given an explicit ``path``, and then returns a *relative* result
    that a child would resolve against its own cwd - for the agent, the workspace. On Windows the
    name is tried with each ``PATHEXT`` extension unless it already carries one.
    """
    if os.name == "nt":
        extensions = [
            ext for ext in (_env_lookup(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if ext
        ]
        names = (
            [name]
            if name.lower().endswith(tuple(ext.lower() for ext in extensions))
            else [name + ext for ext in extensions]
        )
    else:
        names = [name]
    for entry in _env_lookup(env, "PATH").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not Path(entry).is_absolute():
            continue
        for candidate_name in names:
            candidate = Path(entry) / candidate_name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return ""


def _inside_any(program: str, roots: Sequence[Path]) -> Path | None:
    """The first of ``roots`` that contains ``program`` (as written or with links resolved)."""
    forms = {
        os.path.normcase(os.path.abspath(program)),
        os.path.normcase(os.path.realpath(program)),
    }
    for root in roots:
        for root_form in {
            os.path.normcase(os.path.abspath(root)),
            os.path.normcase(os.path.realpath(root)),
        }:
            prefix = root_form.rstrip("\\/") + os.sep
            if any(form == root_form or form.startswith(prefix) for form in forms):
                return Path(root)
    return None


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
    absolute ``cmd.exe`` from :func:`system_command_processor` - and refused without one.
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
        return [command_processor, "/c", *argv]
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
    absolute entries of PATH only (:func:`find_on_path`), and an explicit program (an argument,
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

    from .launch_content import bind_launch_content, final_path

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
    resolved_dsh = program(
        "dsh", dsh_executable, "dsh", needed=agent_argv_override is None
    )
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
    digests, content_problems, content_notes = bind_launch_content(launch)
    if content_problems:
        return launch.model_copy(
            update={"resolvable": False, "detail": " ".join(content_problems)}
        )
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


def _client_module_tree(entry: Path) -> Path | None:
    """The nearest ``node_modules`` directory enclosing ``entry``, or ``None`` for a loose file.

    That is where the client's dependencies are installed and resolved from, so a workspace
    inside it would let the agent rewrite code the client runs.
    """
    for parent in Path(os.path.abspath(entry)).parents:
        if parent.name.casefold() == "node_modules":
            return parent
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
    :data:`INVOCATION_ID_PLACEHOLDER`.
    """
    if launch.dsh_home:
        return "bound", Path(launch.dsh_home)
    return "per_invocation", Path(child_home) / ".dsh"


def child_environment(
    source: Mapping[str, str], *, extra_env: Mapping[str, str], dsh_home: str
) -> dict[str, str]:
    """The environment a child process is started with, built from ``source``.

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

    ``NoDefaultCurrentDirectoryInExePath=1`` is always set, in exactly that spelling, after
    removing every other spelling: acpx starts the agent with the workspace as its cwd, and
    without it both cmd.exe (the npm DSH batch shim runs a bare ``node`` unless a node.exe sits
    beside it; the Desktop shim runs an absolute ``DeepSeek Harness.exe``) and Node's spawn look
    in that cwd before PATH - a ``node.cmd`` the implementer wrote would then run as the
    reviewer's agent. For the same reason a relative PATH entry, which would be resolved
    against the workspace, is not passed on.
    """
    env = dict(source)
    env.update(extra_env)
    if dsh_home:
        env["DSH_HOME"] = dsh_home
    else:
        env.pop("DSH_HOME", None)
    for name in STRIPPED_DSH_ENV:
        env.pop(name, None)
    for key in [key for key in env if key.upper() == NO_CWD_EXE_SEARCH_ENV.upper()]:
        del env[key]
    env[NO_CWD_EXE_SEARCH_ENV] = "1"
    for key in [key for key in env if key.upper() == "PATH"]:
        env[key] = os.pathsep.join(
            entry
            for entry in env[key].split(os.pathsep)
            if entry.strip() and Path(entry.strip().strip('"')).is_absolute()
        )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _rpc_id(value: Any) -> tuple[str, Any] | None:
    """A JSON-RPC id as a key that keeps its type: ``2``, ``2.0`` and ``True`` stay distinct."""
    if value is None or isinstance(value, (dict, list)):
        return None
    return type(value).__name__, value


def _option_of(config_options: Any, category: str, preferred_id: str) -> dict[str, Any] | None:
    """The ``select`` option of one category, the way acpx picks it.

    Category match first, and among those the one whose id is ``preferred_id``; an option that
    only has that id (no category) is the last resort.
    """
    if not isinstance(config_options, list):
        return None
    chosen: dict[str, Any] | None = None
    best = -1
    for option in config_options:
        if not isinstance(option, dict) or option.get("type") != "select":
            continue
        if option.get("category") == category:
            rank = 2 if option.get("id") == preferred_id else 1
        elif option.get("id") == preferred_id:
            rank = 0
        else:
            continue
        if rank > best:
            chosen, best = option, rank
    return chosen


def _option_values(options: Any) -> set[str]:
    """The values a ``select`` option offers, from a flat list or from provider groups."""
    values: set[str] = set()
    for entry in options if isinstance(options, list) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("value"), str):
            values.add(entry["value"])
        values |= _option_values(entry.get("options"))
    return values


class _ModelWatch:
    """What one invocation's stream shows about its model option. Observation only.

    Reads the ``session/new`` response's ``configOptions``, every ``config_option_update``, and
    each outbound ``session/set_config_option`` for the model with its response. It never
    decides an outcome; ``collect`` asks it narrow questions.
    """

    def __init__(self, requested: str | None) -> None:
        self.requested = requested
        self.advertised = False
        self.config_id = ""
        #: Every value the advertised model option offers (flat or grouped), verbatim.
        self.values: set[str] = set()
        self.initial_value: str | None = None
        self.effective_value: str | None = None
        self.source = ""
        self.thought_level: str | None = None
        self.changes: list[ModelChange] = []
        self.session_created = False
        self.session_id = ""
        self._prompt_seen = False
        self._ambiguous = False
        #: Request ids are only unique within one direction; the raw stream has both.
        self._seen_request_ids: set[tuple[str, Any]] = set()
        self._tracked_request_ids: set[tuple[str, Any]] = set()
        #: Outbound requests awaiting their response: id key -> (method, session).
        self._pending: dict[tuple[str, Any], tuple[str, str]] = {}
        self.skipped_updates = 0
        self.skipped_sets = 0
        #: Outcomes of the model's set_config_option requests, in order: True = succeeded.
        self.set_results: list[bool] = []
        self.set_requests = 0
        #: The client's own error line: a JSON-RPC error with a null id, as acpx prints it.
        self.client_error = ""

    def observe(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        key = _rpc_id(message.get("id"))
        if isinstance(method, str):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            session = _session_of(params)
            model_set = (
                method == "session/set_config_option"
                and params.get("configId") == (self.config_id or "model")
            )
            tracked = method == "session/new" or (
                model_set and bool(self.session_id) and session == self.session_id
            )
            if key is not None:
                if key in self._seen_request_ids and (
                    tracked or key in self._tracked_request_ids
                ):
                    self._ambiguous = True
                self._seen_request_ids.add(key)
                if tracked:
                    self._tracked_request_ids.add(key)
            if key is not None and method == "session/new" and not self.session_created:
                self._pending[key] = (method, "")
            elif model_set:
                if key is not None and self.session_id and session == self.session_id:
                    self._pending[key] = (method, session)
                    self.set_requests += 1
                else:
                    self.skipped_sets += 1
            elif method == "session/update":
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                if update.get("sessionUpdate") == "config_option_update":
                    if self.session_id and session == self.session_id:
                        self._read_options(update.get("configOptions"), "config_option_update")
                    else:
                        self.skipped_updates += 1
            return
        answered = self._pending.pop(key, None) if key is not None else None
        result = message.get("result")
        if answered is not None and self._ambiguous:
            return
        method, session = answered if answered is not None else (None, "")
        if (
            method == "session/new"
            and not self.session_created
            and isinstance(result, dict)
            and "error" not in message
            and isinstance(result.get("sessionId"), str)
            and result["sessionId"]
        ):
            self.session_created = True
            self.session_id = result["sessionId"]
            option = _option_of(result.get("configOptions"), "model", "model")
            if option is not None:
                self.advertised = True
                self.config_id = str(option.get("id") or "")
                self.values = _option_values(option.get("options"))
                current = option.get("currentValue")
                self.initial_value = current if isinstance(current, str) else None
                self.effective_value = self.initial_value
                self.source = "session/new"
            self._read_thought_level(result.get("configOptions"))
        elif method == "session/set_config_option" and session == self.session_id:
            # Any JSON-RPC success counts as the agent accepting the request, whatever shape its
            # result has; the effective value is only taken from a result that reports one.
            ok = "result" in message and "error" not in message
            self.set_results.append(ok)
            if ok and isinstance(result, dict):
                self._read_options(result.get("configOptions"), "session/set_config_option")
        elif (
            answered is None
            and "id" in message
            and message.get("id") is None
            and "error" in message
            and not self.client_error
        ):
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            self.client_error = str(error.get("message") or error)[:500]

    def bind_prompt(self, session: str) -> None:
        """Check the first prompt against the session whose configuration was observed."""
        if self._prompt_seen:
            return
        self._prompt_seen = True
        if not session or not self.session_id or session != self.session_id:
            self._ambiguous = True

    def _read_options(self, config_options: Any, source: str) -> None:
        option = _option_of(config_options, "model", self.config_id or "model")
        if option is not None and isinstance(option.get("currentValue"), str):
            value = option["currentValue"]
            if value != self.effective_value:
                self.changes.append(ModelChange(source=source, value=value))
            self.effective_value = value
            self.source = source
        self._read_thought_level(config_options)

    def _read_thought_level(self, config_options: Any) -> None:
        option = _option_of(config_options, "thought_level", "reasoning_effort")
        if option is not None and isinstance(option.get("currentValue"), str):
            self.thought_level = option["currentValue"]

    def refused(self) -> bool:
        """The requested model was refused, as the stream shows it.

        Either the client could not offer it - no model option was advertised, or the value is
        not among the advertised ones - so no change request went out; or the agent answered the
        change request with an error. A value that was already current needs no request, so its
        absence is not a refusal, and a request still awaiting its answer is unknown, not refused.
        """
        if self._ambiguous or not self.session_created or self._pending:
            return False
        if self.set_requests == 0:
            return self.requested is not None and (
                not self.advertised or self.requested not in self.values
            )
        return bool(self.set_results) and not any(self.set_results)

    def observation(self) -> ModelObservation:
        return ModelObservation(
            advertised=self.advertised,
            config_id=self.config_id,
            initial_value=self.initial_value,
            requested=self.requested,
            effective_value=self.effective_value,
            changes=list(self.changes),
            source=self.source,
            thought_level=self.thought_level,
        )

    def applied(self, *, rejected: bool) -> ModelApplied:
        """Classify the requested model from what the stream showed. Never a guess upward.

        ``rejected`` is ``collect``'s confirmed pre-prompt refusal. Otherwise: an answered change
        request is ``accepted`` only while the effective value is the request; an unanswered one
        is ``unknown``. With no change request, ``passed`` is said only while the stream's last
        reported value is still the request - a refusal the stream shows (not advertised, not in
        the catalog) that an earlier outcome branch shadowed, or a later
        ``config_option_update`` that moved the model away, is ``unknown``, not ``passed``.
        """
        if not self.requested:
            return ModelApplied.NOT_PASSED
        if self._ambiguous or not self.session_created:
            return ModelApplied.UNKNOWN
        if rejected or (self.set_results and not any(self.set_results)):
            return ModelApplied.REJECTED
        if any(self.set_results):
            return (
                ModelApplied.ACCEPTED
                if self.effective_value == self.requested
                else ModelApplied.UNKNOWN
            )
        if self.set_requests:
            return ModelApplied.UNKNOWN
        # No change request went out. The rejection branch did not confirm a refusal (rule 7: no
        # upgrade without an observation), so a refusal or a different value is unknown.
        if (
            self.refused()
            or self.initial_value != self.requested
            or self.effective_value != self.requested
        ):
            return ModelApplied.UNKNOWN
        return ModelApplied.PASSED


def _workspace_client_config(workspace: Path) -> Path | None:
    """The workspace's acpx project config if any entry by that name exists, else ``None``.

    See :func:`_workspace_root_entry` for how the entry is found.
    """
    return _workspace_root_entry(workspace, WORKSPACE_CLIENT_CONFIG_NAME)


def _workspace_env_file(workspace: Path) -> Path | None:
    """The workspace's ``.env`` if any root entry by that name exists, else ``None``.

    Found by listing and ``lstat`` only (:func:`_workspace_root_entry`): the file may hold
    credentials and is never opened, so what it sets is unknown and any entry refuses.
    """
    return _workspace_root_entry(workspace, WORKSPACE_ENV_FILE_NAME)


def _workspace_root_entry(workspace: Path, file_name: str) -> Path | None:
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


def _path_forms(path: str | Path) -> set[str]:
    """``path`` as written (made absolute) and with links resolved, both case-folded.

    Raises ``OSError``/``ValueError`` when the path cannot be resolved; a caller refuses then.
    A NUL is refused here: ``realpath`` does not reject one, and no OS path can hold it.
    """
    if "\0" in str(path):
        raise ValueError("the path contains a NUL character")
    return {
        os.path.normcase(os.path.abspath(path)).casefold(),
        os.path.normcase(os.path.realpath(path)).casefold(),
    }


def _same_or_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


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


class AcpxDshDriver:
    """One implementation, one transport. It refuses rather than guessing."""

    driver_id = DRIVER_ID
    driver_version = DRIVER_VERSION
    #: True only where HFlow can actually reach a protocol cancel for its launch mode. The pinned
    #: ``exec`` has one (``session/cancel`` on SIGINT/SIGTERM/SIGHUP), but HFlow cannot deliver
    #: those signals to its ``CREATE_NEW_PROCESS_GROUP`` child on Windows.
    protocol_cancel_supported = False

    def __init__(
        self,
        *,
        data_dir: Path,
        acpx_cli: Path | None = None,
        dsh_executable: str | None = None,
        profile: str = "acp",
        dsh_home: Path | None = None,
        python_executable: str | None = None,
        extra_env: dict[str, str] | None = None,
        completion_timeout_seconds: int = 900,
        agent_argv_override: list[str] | None = None,
        max_raw_log_bytes: int = MAX_RAW_LOG_BYTES,
        launch: LaunchConfig | None = None,
        binding: AgentBinding | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        #: The resolved launch. Given one, this driver uses it verbatim - it does not read the
        #: environment again, which is what makes an approval of this configuration an approval
        #: of what actually runs. Constructed without one (a direct caller, tests), it resolves
        #: its own from the arguments below (``binding`` supplies the model to pass, if any).
        if launch is None:
            launch = resolve_launch_config(
                data_dir=self.data_dir,
                profile=profile,
                dsh_home=dsh_home,
                acpx_cli=acpx_cli,
                dsh_executable=dsh_executable,
                python_executable=python_executable,
                agent_argv_override=agent_argv_override,
                binding=binding,
            )
        if launch.driver_id != DRIVER_ID:
            raise DriverSetupError(
                f"launch config is for driver {launch.driver_id!r}, not {DRIVER_ID!r}"
            )
        if not launch.resolvable:
            raise DriverSetupError(launch.detail or "the launch could not be resolved")
        self.launch = launch
        self.acpx_cli = Path(launch.client_entry)
        self.dsh_executable = launch.dsh_executable
        self.profile = launch.profile
        self.dsh_home = Path(launch.dsh_home) if launch.dsh_home else None
        self.python_executable = launch.python
        self.node_executable = launch.node
        self.extra_env = dict(extra_env or {})
        self.completion_timeout_seconds = completion_timeout_seconds
        #: Retention cap for one invocation's raw logs. A parameter rather than a constant so a
        #: test can drive the overflow path without generating 32 MiB of output.
        self.max_raw_log_bytes = max(0, int(max_raw_log_bytes))
        #: Test seam, resolved into ``self.launch.agent_argv`` before construction: the agent
        #: launch argv that goes into the acpx config. It lives in the launch config rather
        #: than beside it, so an override is part of what an approval covers instead of a
        #: value that could differ from the recorded one. Nothing else about the launch path
        #: is injectable.
        self.agent_argv_override = (
            list(agent_argv_override) if agent_argv_override else self.launch.agent_argv
        )
        self._handles: dict[str, DriverHandle] = {}
        #: Serializes "publish the handle and create the child" against "a stop arrived". Held
        #: for the spawn only, never across the wait for a result. A ``Condition`` rather than a
        #: lock because a stop that finds a spawn already in flight must *wait for that spawn to
        #: publish* - "no handle yet" is not an answer while a child is being created.
        self._gate = threading.Condition()
        #: Invocations whose spawn is inside the gate right now. A stop that arrives for one of
        #: these waits for the publication instead of reporting that nothing was found.
        self._spawn_pending: set[str] = set()
        #: Invocations a stop has been requested for. A spawn that has not created its child yet
        #: refuses, so the stop wins the handoff; it says nothing about a child that exists.
        self._stop_requests: dict[str, bool] = {}
        #: The wall-clock instant this invocation must be finished by, from *its own* request
        #: deadline. The client is given the same number as a flag, but a client that hangs before
        #: protocol startup never enforces it, so the driver has to hold it too.
        self._invocation_deadlines: dict[str, float] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._boundaries: dict[str, ProcessBoundary] = {}
        #: One lock per invocation for terminating, querying and closing its boundary. A stop and
        #: ``collect`` can both reach the boundary from different threads; without this, one could
        #: close the job while the other was still asking it whether it is empty.
        self._teardown_locks: dict[str, threading.Lock] = {}
        #: What each boundary held when it was closed (see ``ExitBoundary``).
        self._exit_boundaries: dict[str, ExitBoundary] = {}
        self._streams: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stream_drained: dict[str, bool] = {}
        #: Why the protocol stream reader stopped before reading the stream to its end (the
        #: exception class), or why the reader could not be started at all. Written before the
        #: reader's drained flag, so ``collect`` reads it as final once the flag is set.
        self._reader_failures: dict[str, str] = {}
        #: What the stdin writer did with the prompt: ``(delivered, detail)``. ``delivered`` is True
        #: only when every byte was written and flushed into the pipe; ``detail`` names a failure.
        self._stdin_writes: dict[str, tuple[bool, str]] = {}
        self._receipts: dict[str, CancellationReceipt] = {}
        self._events: dict[str, list[NormalizedEvent]] = {}
        #: Assistant messages of each invocation, kept so a reviewer's final answer can be
        #: reassembled from the chunks that were actually observed (see ``collect``).
        self._transcripts: dict[str, AnswerTranscript] = {}
        #: Request ids of the ``session/prompt`` messages observed, in stream order.
        self._prompt_request_ids: dict[str, list[Any]] = {}
        #: What the stream showed about each invocation's model option (see ``_ModelWatch``).
        self._model_watches: dict[str, _ModelWatch] = {}
        #: Every response that carried a ``stopReason``, in stream order, whatever request it
        #: answered. Which of them settles the turn is decided in ``_bound_response``, not by the
        #: order they arrived in.
        self._terminal_responses: dict[str, list[_TerminalResponse]] = {}
        #: The first error response per observed prompt id, in stream order. Which one answers the
        #: turn is decided in ``_prompt_error``.
        self._prompt_errors: dict[str, list[PromptErrorResponse]] = {}
        #: Prompt ids that another request carried after the prompt was sent, with that request's
        #: method. JSON-RPC ids are per direction, and the stream carries both directions.
        self._prompt_id_reused_by: dict[str, dict[tuple[str, Any], str]] = {}
        #: Non-empty protocol lines read so far: the next line's stream index. ``_lines`` is a
        #: bounded deque and ``_events`` is capped, so neither can number a line once it is full;
        #: this never saturates. It is the 0-based non-empty-line ordinal, not a physical
        #: line number (blank lines remain in the retained log).
        self._line_counts: dict[str, int] = {}
        #: Framed raw bytes consumed, including blank and discarded line content. Every wire
        #: record must fit this byte budget and the record budget before it can grow any state.
        self._protocol_bytes: dict[str, int] = {}
        self._output_limit_reasons: dict[str, set[str]] = {}
        #: The session the latest ``session/prompt`` named, and ``[updates, agent_message_chunk
        #: updates]`` per session a prompt named, counted from that prompt on. Bounded by the
        #: number of prompt requests, which is one for ``exec``.
        self._prompt_sessions: dict[str, str] = {}
        self._prompt_session_updates: dict[str, dict[str, list[int]]] = {}
        self._lines: dict[str, list[str]] = {}
        self._unparsed: dict[str, int] = {}
        self._overflow: dict[str, bool] = {}
        #: Lines that never ended before the pending buffer's cap: counted separately from
        #: unparseable lines so a client stuck writing one enormous line is diagnosable.
        self._oversized: dict[str, int] = {}
        #: Retained stderr per invocation, so ``collect`` can report what was kept rather than
        #: implying the whole stream is on disk.
        self._stderr_captures: dict[str, StreamCapture] = {}
        #: What ``release`` could not do, per invocation (a pipe left to a reader that is still
        #: blocked on it). Recorded facts, not part of the already-returned result.
        self._release_notes: dict[str, list[str]] = {}
        #: Set once all protocol state stopped growing. Only constant-size dispatch and drain
        #: counters may change afterwards; the retained raw file has its independent byte cap.
        self._events_capped: dict[str, bool] = {}
        #: Highest byte offset ever read from a client's output file. Reported so the retention
        #: bound is a measured number rather than a claim: the file is trimmed back to the budget,
        #: but a client can write between two trims.
        self._peak_raw_bytes: dict[str, int] = {}
        #: Retained protocol stream per invocation, for the same reporting purpose as stderr.
        self._stdout_captures: dict[str, StreamCapture] = {}
        #: Fraction of the retention budget reserved for stderr. A *fraction* rather than a fixed
        #: number of bytes on purpose: with a fixed share, a small configured budget would leave
        #: the protocol stream a zero-byte share, and the cap would silently mean "keep nothing".
        self.stderr_share_fraction = 0.125
        #: Hard cap on all non-empty protocol records, including records with no neutral event.
        self.max_event_records = 20000
        self._results: dict[str, InvocationResult] = {}
        #: Digest of the prompt text each invocation was launched with, reported by ``collect``
        #: so the controller can tell "the model answered" from "the prompt arrived intact".
        self._prompt_digests: dict[str, str] = {}
        #: Invocations whose spawn report the callback refused. Recorded rather than raised, so a
        #: reader can see that the ledger entry for that invocation is conservative by accident
        #: instead of by fact.
        self._spawn_report_errors: list[str] = []
        #: What each invocation's launch showed of DSH's own inputs, taken just before the spawn
        #: gate; kept by ``release`` like the other recorded facts.
        self._launch_surfaces: dict[str, LaunchSurfaces | None] = {}
        #: Why an invocation's launch-surface record is missing.
        self._surface_notes: dict[str, str] = {}

    # -- configuration -------------------------------------------------------

    def _report_spawn(
        self,
        request: InvocationRequest,
        *,
        created: bool,
        pid: int | None,
        detail: str,
    ) -> None:
        """Report what this driver observed at its spawn decision.

        Called inside the spawn gate, once per invocation: either a stop won the gate and no
        process exists, or a child was created and its pid is known. The controller's ledger
        turns this into ``started`` or ``not_started``; a driver that never reports leaves its
        invocation recorded as a launch that was requested, which is the honest reading of "nobody
        said whether a process exists".

        A callback that raises must not break the launch - it is the *operator's* record, and the
        process is already created or already refused. The worst case of swallowing it is a
        conservative ledger entry ("requested and unconfirmed"), never a missing child.
        """
        if request.on_spawn is None:
            return
        try:
            request.on_spawn(
                SpawnFact(
                    invocation_id=request.invocation_id,
                    created=created,
                    pid=pid,
                    # This launch path creates a real operating-system child, so a reported
                    # creation is a reported process. That is a property of the transport, stated
                    # here rather than guessed from the fact that the driver is the real one.
                    spawn_kind=SpawnKind.PROCESS if created else SpawnKind.UNKNOWN,
                    detail=detail,
                )
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not fail the spawn path
            self._spawn_report_errors.append(request.invocation_id)

    def _agent_argv(self) -> list[str]:
        """The launcher command as a real argv, as resolved before any approval."""
        return list(self.launch.agent_argv)

    def _child_env(self, handle_workspace: Path) -> dict[str, str]:
        """The environment a child process is started with: :func:`child_environment` applied
        to this process's environment, this driver's ``extra_env`` and the bound DSH home.
        """
        return child_environment(
            os.environ, extra_env=self.extra_env, dsh_home=self.launch.dsh_home
        )

    # -- probe ---------------------------------------------------------------

    def probe(self, binding: AgentBinding) -> CapabilityReport:
        """Local capability record. Sends no task and calls no model."""
        notes = [
            "probe is static: no prompt, no session, no model request",
            f"acpx CLI: {self.acpx_cli}",
            f"agent argv: {self._agent_argv()}",
            "cooperative protocol cancel: unsupported for this launcher (acpx cancel targets "
            "a persisted session's queue owner; exec sends session/cancel only on "
            "SIGINT/SIGTERM/SIGHUP, which HFlow cannot deliver to its CREATE_NEW_PROCESS_GROUP "
            "child on Windows - Ctrl+Break arrives as SIGBREAK, which acpx does not handle)",
            "stopReason=end_turn is turn settlement, not success: acceptance is decided by "
            "checks and review",
            (
                f"model: --model {self.launch.model} on the client command line (acpx applies it "
                "with session/set_config_option before the prompt and refuses a value the agent "
                "did not advertise)"
                if self.launch.model
                else "model: native_profile - no --model flag; the launcher's DSH profile decides"
            ),
            "model_selection capability: documented only - no set_config_option round trip "
            "with a real DSH has been observed; each run records what its stream showed",
        ]
        notes.extend(self._content_notes())
        try:
            from .dsh_surfaces import observe_launch_surfaces, probe_notes

            kind, home = effective_dsh_home(
                self.launch,
                child_home=child_home_for(self.data_dir.resolve(), INVOCATION_ID_PLACEHOLDER),
            )
            notes.extend(
                probe_notes(
                    observe_launch_surfaces(
                        self.launch,
                        dsh_home=home,
                        dsh_home_kind=kind,
                        child_env=self._child_env(self.data_dir),
                        workspace=None,
                        look_in_home=kind == "bound",
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - a record, never a reason the probe fails
            notes.append(f"launch surfaces could not be examined: {type(exc).__name__}: {exc}")
        ambient = sorted(
            name for name in STRIPPED_DSH_ENV if name in os.environ or name in self.extra_env
        )
        notes.append(
            f"DSH mode variables ({', '.join(STRIPPED_DSH_ENV)}) are never set by this driver; "
            + (
                f"present here and removed from the child environment: {', '.join(ambient)}"
                if ambient
                else "none is present here, and an ambient one would be removed"
            )
        )
        notes.append(
            "a workspace containing .acpxrc.json is refused before spawn: acpx would let it "
            "override this driver's config, agent argv included; so is a workspace whose root "
            "holds a .env (DSH loads it at launch; listed only, never opened) and a bound "
            "DSH_HOME that is relative, inside the workspace or around it"
        )
        return CapabilityReport(
            driver_id=DRIVER_ID,
            driver_version=DRIVER_VERSION,
            harness=binding.harness,
            harness_version=None,
            os=f"{os.name}",
            arch=os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"),
            probe_only=True,
            live_tested=False,
            capabilities={
                "fresh_session": CapabilityState.PROBED,
                "session_open_close": CapabilityState.PROBED,
                "prompt_turn": CapabilityState.PROBED,
                "streamed_updates": CapabilityState.PROBED,
                "structured_output": CapabilityState.PROBED,
                "cancel": CapabilityState.UNSUPPORTED,
                "process_boundary_teardown": CapabilityState.PROBED,
                "session_list": CapabilityState.DOCUMENTED,
                "session_resume": CapabilityState.DOCUMENTED,
                "model_selection": CapabilityState.DOCUMENTED,
                "billing_usage": CapabilityState.UNKNOWN,
                "readonly_enforcement": CapabilityState.UNSUPPORTED,
                "native_subagents": CapabilityState.UNSUPPORTED,
            },
            notes=notes,
        )

    # -- start / observe -----------------------------------------------------

    def _client_argv(self, invocation_dir: Path, workspace: Path, deadline_seconds: int) -> list[str]:
        """The client command line, with the right interpreter for the entry point.

        The published acpx CLI is a Node program (``dist/cli.js``), so it must run under
        Node; a Python entry point (the test stand-in) runs under Python. Feeding a
        JavaScript file to the Python interpreter fails immediately, which is a defect this
        driver must not have.

        A bound model is the one optional flag: acpx's *global* ``--model``, so it precedes the
        subcommand. Its value is the launch's validated, approval-bound value - a fixed
        configuration flag, never task text (that still travels on stdin).
        """
        model = ["--model", self.launch.model] if self.launch.model else []
        return [
            *self._client_prefix(),
            str(self.acpx_cli),
            "--cwd",
            str(workspace),
            "--format",
            "json",
            "--timeout",
            str(deadline_seconds),
            *model,
            "exec",
            "-f",
            "-",
        ]

    # -- read-only launch check ---------------------------------------------

    def readonly_client_check(
        self, args: list[str] | None = None, *, timeout_seconds: int = 60
    ) -> dict[str, Any]:
        """Run the real client with a read-only metadata argument, through this launcher.

        This exists to prove that *this* code can actually start the installed client - the
        interpreter choice, the boundary, the stream drain - without sending a task. It is
        the same code path a real invocation uses (same argv construction, same Job Object
        launch, same reader), with a metadata argument instead of ``exec``.

        No session is created, no prompt is sent, no model is reachable from here. Callers
        must be explicit that they are running a metadata probe, never a task.
        """
        argv = [*self._client_prefix(), str(self.acpx_cli), *(args or ["--version"])]
        content_refusal = self._launch_content_refusal()
        if content_refusal:
            raise RefusedError(RefusalCode.LAUNCH_CONTENT_CHANGED, content_refusal)
        work_dir = self.data_dir / "readonly-check"
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = work_dir / "stdout.txt"
        stderr_path = work_dir / "stderr.txt"
        boundary = ProcessBoundary().open()
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                child = popen_in_boundary(
                    argv,
                    cwd=str(work_dir),
                    env=self._child_env(work_dir),
                    boundary=boundary,
                    stdout_handle=stdout_handle,
                    stderr_handle=stderr_handle,
                )
                # A metadata probe takes no stdin: close it so the client cannot wait on us.
                if child.stdin is not None:
                    child.stdin.close()
                try:
                    returncode = child.wait(timeout=timeout_seconds)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    boundary.terminate()
                    returncode = child.wait(timeout=10)
                    timed_out = True
            emptied = boundary.wait_empty(10.0)
            # True only for an observed exit. None (unanswered) fails the preflight exactly as
            # False does.
            gone = process_gone(child.pid, 3.0)
        finally:
            boundary.close()
        return {
            "argv": argv,
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout": stdout_path.read_text(encoding="utf-8", errors="replace")[:2000],
            "stderr": stderr_path.read_text(encoding="utf-8", errors="replace")[:2000],
            "boundary_kind": boundary.kind,
            "boundary_empty": emptied,
            "process_gone": gone,
        }

    def _client_prefix(self) -> list[str]:
        """Interpreter prefix for the client entry point, as resolved before any approval."""
        return list(self.launch.client_argv_prefix)

    def _content_notes(self) -> list[str]:
        """The probe's (and so doctor's) lines for the launch content binding."""
        from .launch_content import CONTENT_BINDING_LABEL, content_lines

        if not self.launch.content_digests:
            return [
                "launch content: not bound - this launch recorded no entry-file digests (it was "
                "not resolved by resolve_launch_config); it is bound by path only"
            ]
        return [
            f"launch content: {CONTENT_BINDING_LABEL} (SHA-256 taken at resolution, checked "
            "again just before each spawn; the window between that check and process creation "
            "stays open on Windows, which has no exec-by-handle)",
            *(f"launch content: {line}" for line in content_lines(self.launch)),
            *(f"launch content: {note}" for note in self.launch.content_notes),
        ]

    def _launch_content_refusal(self) -> str:
        """Why the launch entry files no longer match the launch's recorded content, or ``""``.

        Hashes again (node.exe is ~90 MB, ~60 ms warm). Anything unexpected while checking is a
        refusal too: the check fails closed. A file swapped after this check and before
        ``CreateProcess`` is not caught - Windows offers no exec-by-handle.
        """
        from .launch_content import launch_content_changes

        try:
            changes = launch_content_changes(self.launch)
        except Exception as exc:  # noqa: BLE001 - an unchecked launch is not started
            changes = [f"the content check itself failed: {type(exc).__name__}: {exc}"]
        if not changes:
            return ""
        return (
            "the launch entry files no longer match the content bound when the launch was "
            "resolved: " + "; ".join(changes) + "."
        )

    def _observe_launch(
        self, invocation_id: str, *, workspace: Path, env: Mapping[str, str], child_home: Path
    ) -> None:
        """Record what DSH reads on its own at this launch (see ``dsh_surfaces``).

        Never raises, never changes ``env``, never decides anything: an observation that fails
        leaves the record missing, with the reason kept for the result's limitations.
        """
        try:
            from .dsh_surfaces import observe_launch_surfaces

            kind, home = effective_dsh_home(self.launch, child_home=child_home)
            self._launch_surfaces[invocation_id] = observe_launch_surfaces(
                self.launch, dsh_home=home, dsh_home_kind=kind, child_env=env, workspace=workspace
            )
        except Exception as exc:  # noqa: BLE001 - an observation must never stop a launch
            self._launch_surfaces[invocation_id] = None
            self._surface_notes[invocation_id] = (
                f"launch surfaces not recorded: {type(exc).__name__}: {exc}"
            )

    def start_handle(self, request: InvocationRequest) -> DriverHandle:
        """Launch one invocation and return immediately with an observable handle."""
        if request.invocation_id in self._handles:
            raise DriverSetupError(
                f"invocation {request.invocation_id} was already started; refusing to start it twice"
            )
        invocation_dir = self.data_dir / INVOCATIONS_DIR_NAME / request.invocation_id
        invocation_dir.mkdir(parents=True, exist_ok=True)
        event_log = invocation_dir / "events.ndjson"
        stdout_path = invocation_dir / "stdout.ndjson"
        stderr_path = invocation_dir / "stderr.txt"
        task_file = invocation_dir / "task.txt"

        workspace = Path(request.workspace)
        # Task body travels as a file, not as a command-line string. The file holds the whole
        # prompt (packet + envelope) so what was sent is auditable after the fact, and the
        # digest of exactly those bytes is what ``collect`` reports back to the controller.
        prompt = effective_prompt(request)
        prompt_bytes = prompt.encode("utf-8")
        task_file.write_bytes(prompt_bytes)
        prompt_digest = packet_digest(prompt)
        # Recorded before the spawn decision: a stop that wins the handoff produces a cancelled
        # invocation, and its result must still carry the digest of the prompt it would have been
        # given rather than looking like an invocation that had no input.
        self._prompt_digests[request.invocation_id] = prompt_digest
        config_path = self._write_config(invocation_dir, writes_allowed=request.writes_allowed)

        boundary = ProcessBoundary().open()
        argv = self._client_argv(invocation_dir, workspace, request.deadline_seconds)
        # Recorded before the spawn gate: the deadline belongs to the invocation, and a process
        # that is created must already be bounded by it.
        self._invocation_deadlines[request.invocation_id] = (
            time.monotonic() + float(request.deadline_seconds)
        )
        handle = DriverHandle(
            invocation_id=request.invocation_id,
            attempt_id=request.attempt_id,
            run_id=request.run_id,
            role=request.role,
            workspace=str(workspace),
            started_at=utc_now(),
            process_identity=f"{request.invocation_id}:{os.getpid()}:{utc_now()}",
            boundary_kind=boundary.kind,
            event_log=str(event_log),
        )
        env = self._child_env(workspace)
        # Absolute, resolved paths only: a relative home would resolve against the child's
        # cwd and silently point the client at the wrong config directory.
        child_home = (invocation_dir / CHILD_HOME_DIR_NAME).resolve()
        child_home.mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(child_home)
        env["HOME"] = str(child_home)
        env["APPDATA"] = str((child_home / "AppData").resolve())
        Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)
        # acpx reads its global config from the OS home; point it at the per-invocation one.
        acpx_home = child_home / ".acpx"
        acpx_home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config_path, acpx_home / "config.json")
        # Outside the gate, so a concurrent stop is not delayed by it; it records only.
        self._observe_launch(
            request.invocation_id, workspace=workspace, env=env, child_home=child_home
        )

        # The child is created *inside* this gate, and both cancel entry points take the same
        # gate to record their request. That is what makes the handoff decidable: either the stop
        # is recorded first and no process is created at all, or the process is published first
        # and the stop then finds a handle to act on. A read-then-spawn cannot give either
        # guarantee - the stop lands between them.
        #
        # The gate is held for the spawn only, never for the wait for a model answer: holding it
        # across ``collect`` would make cancellation block on the whole invocation, which is the
        # opposite of what a stop is for.
        with self._gate:
            # Claimed before the spawn decision, so a stop that takes the gate during this spawn
            # knows to wait for the publication below instead of finding "no handle yet".
            self._spawn_pending.add(request.invocation_id)
            try:
                if self._stop_requests.get(request.invocation_id) or (
                    request.stop_requested is not None and request.stop_requested()
                ):
                    # Nothing was created and nothing will be. The invocation is reported as
                    # cancelled without a process, so the controller's stop wins the handoff.
                    self._handles[request.invocation_id] = handle
                    handle.start_cancelled = True
                    handle.finished = True
                    boundary.close()
                    self._report_spawn(
                        request,
                        created=False,
                        pid=None,
                        detail=(
                            "a recorded stop won the spawn gate: no client process was created"
                        ),
                    )
                    return handle
                client_config = _workspace_client_config(workspace)
                if client_config is not None:
                    # Checked here, at the last moment before the process exists, for every
                    # invocation and both roles: the reviewer runs on the worktree the implementer
                    # wrote. Nothing is created and no handle is published, so a later stop or
                    # reconcile of this id truthfully finds that nothing was started. A file that
                    # is already in the starting workspace is refused earlier, by admission
                    # (``prepare.start_workspace_client_config``), while nothing is spent; one that
                    # appears later is caught here, when this dispatch is already reserved - so the
                    # message names the step that works then, not "submit again".
                    boundary.close()
                    message = (
                        f"{client_config} exists in the workspace. acpx always loads it and lets "
                        "it override HFlow's launch, including the agent command, so no client "
                        "process was started. (A run that starts with one is refused at admission, "
                        "before anything is reserved; this one was found at launch, when the "
                        "dispatch was already reserved, so the run stays blocked.) Remove "
                        "the file (or directory), then submit a new revision of the task: an "
                        "identical TaskSpec returns this blocked run, and under a root budget the "
                        "new revision's first implementer counts as a repair, so it needs a "
                        "repair attempt left on the root."
                    )
                    self._report_spawn(request, created=False, pid=None, detail=message)
                    raise RefusedError(RefusalCode.WORKSPACE_CLIENT_CONFIG, message)
                env_file = _workspace_env_file(workspace)
                if env_file is not None:
                    # The same gate and the same reasoning as the client config above: DSH loads
                    # ``<cwd>/.env`` at launch. Listed and lstat'ed only - never opened, so what
                    # it sets is unknown and any entry by that name refuses. One already in the
                    # starting workspace is refused at admission
                    # (``prepare.start_workspace_env_file``).
                    boundary.close()
                    message = (
                        f"{env_file} exists in the workspace. DSH loads <cwd>/.env at launch "
                        "into its own environment and its tool processes (documented upstream, "
                        "not observed); HFlow never opens it, so no client process was started. "
                        "(A run that starts with one is refused at admission, before anything is "
                        "reserved; this one was found at launch, when the dispatch was already "
                        "reserved, so the run stays blocked.) Remove or rename the file (or "
                        "directory), then submit a new revision of the task: an identical "
                        "TaskSpec returns this blocked run, and under a root budget the new "
                        "revision's first implementer counts as a repair, so it needs a repair "
                        "attempt left on the root."
                    )
                    self._report_spawn(request, created=False, pid=None, detail=message)
                    raise RefusedError(RefusalCode.WORKSPACE_ENV_FILE, message)
                home_problem = dsh_home_workspace_problem(
                    self.launch.dsh_home, spawn_workspaces(workspace)
                )
                if home_problem:
                    # Admission refuses this against the project root and the worktree directory
                    # (``prepare.launch_dsh_home_problem``); checked again here against the
                    # role's actual cwd, for every invocation, before any process exists.
                    boundary.close()
                    message = (
                        f"{home_problem}. No client process was started. Point DSH_HOME at an "
                        "absolute directory outside the project, its worktrees and this "
                        "workspace (or unset it, so each invocation gets its own home), then "
                        "prepare again: DSH_HOME is part of the approved launch, so a real run "
                        "needs an authorization for the new binding, and since this dispatch "
                        "was already reserved the run stays blocked - submit a new revision."
                    )
                    self._report_spawn(request, created=False, pid=None, detail=message)
                    raise RefusedError(RefusalCode.DSH_HOME_IN_WORKSPACE, message)
                content_refusal = self._launch_content_refusal()
                if content_refusal:
                    # The last check before the process exists: the entry files are hashed again
                    # and compared with what the launch recorded. Nothing is created and no handle
                    # is published, as for the workspace config above.
                    boundary.close()
                    message = (
                        f"{content_refusal} No client process was started; the run stays "
                        "blocked. Run `hflow prepare` again to see the files' current digests and "
                        "re-issue the authorization for them."
                    )
                    self._report_spawn(request, created=False, pid=None, detail=message)
                    raise RefusedError(RefusalCode.LAUNCH_CONTENT_CHANGED, message)
                try:
                    stdout_handle = stdout_path.open("wb")
                    child = popen_in_boundary(
                        argv,
                        cwd=str(invocation_dir),
                        env=env,
                        boundary=boundary,
                        stdout_handle=stdout_handle,
                        # stderr travels on a pipe this driver owns, so what is retained is genuinely
                        # bounded and the reader has a stream to read. Giving the child a file instead
                        # meant the reader had nothing to drain (`process.stderr` was ``None``) while the
                        # child wrote 2 MiB HFlow never noticed.
                        stderr_handle=subprocess.PIPE,
                    )
                except BaseException:
                    boundary.close()
                    raise
                handle.pid = child.pid
                # Published while the gate is still held: a stop waiting on this publication wakes
                # up to a handle it can act on, not to an empty map.
                self._handles[request.invocation_id] = handle
                self._processes[request.invocation_id] = child
                self._boundaries[request.invocation_id] = boundary
                # Reported inside the same gate, right after the process exists: the controller's
                # ledger learns "a child was created" as an observation, not as an assumption made
                # from the fact that it asked for a launch.
                self._report_spawn(
                    request,
                    created=True,
                    pid=child.pid,
                    detail=f"client process {child.pid} created inside the managed boundary",
                )
            finally:
                self._spawn_pending.discard(request.invocation_id)
                self._gate.notify_all()

        # The child is published and reported. From here on start_handle never raises: a failure
        # would leave a published process, its boundary and the parent's file handles with nobody
        # to collect or release them (the controller's generic error path does neither). Anything
        # that fails is recorded, the tree is torn down through the boundary and released, and
        # ``collect`` reports the invocation as unknown (``reader_failed``).
        try:
            self._prepare_stream_state(request)
            self._start_io_threads(request.invocation_id, child, prompt_bytes, stdout_path, stderr_path)
        except BaseException as exc:
            self._abandon_after_spawn(request.invocation_id, child, handle, exc)
            if not isinstance(exc, Exception):
                raise
        return handle

    def _prepare_stream_state(self, request: InvocationRequest) -> None:
        """Per-invocation stream state, set before any thread that reads or writes it starts."""
        # One declared retention budget per invocation, split between the protocol stream and
        # stderr. The readers retain only each share; the protocol reader also freezes all
        # accumulated wire state after its byte or record cap. The client's own output file is
        # measured, not trimmed while another process owns it.
        self._events[request.invocation_id] = []
        # Bound in ``_note_message`` to the session the first ``session/prompt`` names: no message
        # chunk is read before that, and another session's are excluded and counted.
        self._transcripts[request.invocation_id] = AnswerTranscript(
            role=request.role, require_session=True
        )
        self._prompt_request_ids[request.invocation_id] = []
        self._model_watches[request.invocation_id] = _ModelWatch(self.launch.model or None)
        self._terminal_responses[request.invocation_id] = []
        self._prompt_errors[request.invocation_id] = []
        self._prompt_id_reused_by[request.invocation_id] = {}
        self._line_counts[request.invocation_id] = 0
        self._protocol_bytes[request.invocation_id] = 0
        self._output_limit_reasons[request.invocation_id] = set()
        self._prompt_session_updates[request.invocation_id] = {}
        self._lines[request.invocation_id] = deque(maxlen=MAX_BUFFERED_LINES)
        self._unparsed[request.invocation_id] = 0
        self._overflow[request.invocation_id] = False
        self._oversized[request.invocation_id] = 0
        self._events_capped[request.invocation_id] = False
        self._peak_raw_bytes[request.invocation_id] = 0

    def _start_io_threads(
        self,
        invocation_id: str,
        child: subprocess.Popen,
        prompt_bytes: bytes,
        stdout_path: Path,
        stderr_path: Path,
    ) -> None:
        """Start both readers, then the stdin writer. Returns without waiting for any of them.

        The readers come first: a client that writes a pipe's worth of stderr before it reads its
        stdin would otherwise deadlock with a driver blocked writing the prompt. The prompt is
        written from its own thread, so ``start_handle`` returns at once and ``collect``'s
        deadline-bounded wait applies from the spawn: a client that never reads its stdin is torn
        down at the invocation deadline, and the teardown is what unblocks the writer.
        """
        thread = threading.Thread(
            target=self._consume_stream,
            args=(invocation_id, stdout_path),
            daemon=True,
        )
        self._threads[invocation_id] = thread
        thread.start()
        stderr_thread = threading.Thread(
            target=self._consume_stderr,
            args=(invocation_id, stderr_path),
            daemon=True,
        )
        self._threads[f"{invocation_id}:stderr"] = stderr_thread
        stderr_thread.start()
        writer = threading.Thread(
            target=self._feed_stdin,
            args=(invocation_id, child, prompt_bytes),
            daemon=True,
        )
        self._threads[f"{invocation_id}:stdin"] = writer
        writer.start()

    def _feed_stdin(self, invocation_id: str, child: subprocess.Popen, prompt_bytes: bytes) -> None:
        """Write the task to the client's stdin, then close it: HFlow -> acpx input is complete.

        The ACP pipe between acpx and DSH is acpx's own and is not touched here. A client that
        exits (or is torn down) before reading everything breaks the pipe; that is recorded, not
        raised, and ``collect`` reports it. ``close`` is guarded too: it flushes what the buffer
        still holds, and on a broken pipe that raises (``EINVAL`` on Windows). Only this thread
        closes stdin while it runs - closing a buffered writer from another thread would wait on
        the lock a blocked write holds.
        """
        stream = child.stdin
        total = len(prompt_bytes)
        accepted = 0
        delivered = False
        detail = ""
        try:
            if stream is None:
                detail = "the client has no stdin pipe"
                return
            try:
                view = memoryview(prompt_bytes)
                for start in range(0, total, STDIN_WRITE_CHUNK):
                    accepted += stream.write(view[start : start + STDIN_WRITE_CHUNK]) or 0
                stream.flush()
                delivered = True
            except (OSError, ValueError) as exc:
                detail = (
                    f"{type(exc).__name__} after about {accepted} of {total} prompt bytes were "
                    "handed to the pipe"
                )
            try:
                stream.close()
            except (OSError, ValueError) as exc:
                if delivered:
                    detail = f"every prompt byte was written; closing stdin then raised {type(exc).__name__}"
                elif not detail:
                    detail = f"closing stdin raised {type(exc).__name__}"
        except BaseException as exc:  # noqa: BLE001 - a writer must report, never die silently
            delivered = False
            detail = detail or f"the stdin writer failed: {type(exc).__name__}"
        finally:
            self._stdin_writes[invocation_id] = (delivered, detail)

    def _stdin_state(self, invocation_id: str, *, join_seconds: float) -> tuple[bool, str]:
        """``(delivered, note)`` for the prompt write; ``note`` is empty for a clean write.

        Waits up to ``join_seconds`` for a writer that is still running. A writer still blocked
        after that has not delivered the prompt.
        """
        writer = self._threads.get(f"{invocation_id}:stdin")
        if writer is not None and writer.is_alive() and join_seconds > 0:
            writer.join(timeout=join_seconds)
        if writer is not None and writer.is_alive():
            return False, (
                "prompt_write_incomplete: the stdin writer was still blocked writing the prompt "
                "when the result was folded; the client did not read its whole input"
            )
        recorded = self._stdin_writes.get(invocation_id)
        if recorded is None:
            if writer is None:
                # No writer was started (``start_handle`` was abandoned after the spawn).
                return False, "prompt_write_incomplete: the prompt was never written to the client"
            return False, "prompt_write_incomplete: the stdin writer recorded nothing"
        delivered, detail = recorded
        if delivered:
            return True, (f"prompt_stdin_close: {detail}" if detail else "")
        return False, (
            f"prompt_write_incomplete: the prompt was not fully written to the client's stdin "
            f"({detail}); the reported prompt digest is of the prompt HFlow meant to send"
        )

    def _abandon_after_spawn(
        self, invocation_id: str, child: subprocess.Popen, handle: DriverHandle, exc: BaseException
    ) -> None:
        """``start_handle`` failed after the child was published: tear down and release.

        Best effort and never raising. The failure is recorded as a reader failure, so ``collect``
        folds the invocation as unknown instead of judging a stream nobody read; the tree is
        stopped through the managed boundary, which is closed with what it held recorded; and
        the normal release path closes the parent's pipes and files.
        """
        self._reader_failures.setdefault(
            invocation_id,
            f"start_handle failed after the client process was created ({type(exc).__name__})",
        )
        try:
            with self._teardown_lock(invocation_id):
                _stopped, emptied, detail = self._force_stop_client(child, handle)
                self._close_boundary(
                    invocation_id, ExitBoundary(emptied=emptied, left_behind=None, detail=detail)
                )
        except Exception:  # noqa: BLE001 - release below still closes the boundary
            pass
        reader = self._threads.get(invocation_id)
        if reader is None or not reader.is_alive():
            # No reader will ever set the flag; ``collect`` must not wait for one.
            self._stream_drained[invocation_id] = True
        try:
            self.release(invocation_id)
        except Exception:  # noqa: BLE001 - nothing further can be done here
            pass

    def _write_config(self, invocation_dir: Path, *, writes_allowed: bool) -> Path:
        """Per-invocation acpx config: structured argv, explicit agent name, explicit policy.

        Permission policy is derived from the **request** (role + approved mode), not from a
        driver default and not from an ambient environment variable, so a reviewer cannot
        inherit an implementer's write permission.

        The keys are taken from the installed client, not invented. ``nonInteractivePermissions``
        accepts only ``deny`` or ``fail``; the read/write decision is ``defaultPermissions`` with
        ``approve-all`` / ``approve-reads`` / ``deny-all``. ``approve-all`` means *all* tool
        permission requests are auto-approved - not only file writes - which is why it is
        disclosed and limited to a write-capable implementer inside a disposable worktree.

        Unknown keys are not harmless: the client ignored an invented ``permissionPolicy`` key
        silently (so a run proceeded at the client default). The generated key set is therefore
        asserted against a strict allowlist in the offline checks.
        """
        mode = "approve-all" if writes_allowed else "approve-reads"
        config = {
            "defaultAgent": DRIVER_ID,
            "authPolicy": "skip",
            # Only ever "deny": this client has no approved "allow" value here.
            "nonInteractivePermissions": "deny",
            "defaultPermissions": mode,
            "ttl": 30,
            "format": "json",
            "agents": {DRIVER_ID: {"argv": self._agent_argv()}},
        }
        path = invocation_dir / "acpx-config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def _consume_stream(self, invocation_id: str, stdout_path: Path) -> None:
        """The reader thread's target: ``_read_stream`` with a guard that cannot be skipped.

        A reader that died silently used to leave the stream undrained and uncounted, and
        ``collect`` then folded the partial stream as if it were whole. Whatever escapes is
        recorded (the exception class) before the drained flag is set, so ``collect`` neither
        waits for a reader that is gone nor judges what it did not read: it reports
        ``reader_failed``.
        """
        try:
            self._read_stream(invocation_id, stdout_path)
        except BaseException as exc:  # noqa: BLE001 - recorded; collect reports it
            self._reader_failures.setdefault(
                invocation_id, f"the stream reader stopped with {type(exc).__name__}"
            )
            if not isinstance(exc, Exception):
                raise
        finally:
            self._stream_drained[invocation_id] = True

    def _read_stream(self, invocation_id: str, stdout_path: Path) -> None:
        """Tail the client's protocol stream into a bounded retained log, projecting each line.

        What is bounded here is what HFlow **keeps**: the retained log stops at the protocol share
        of the retention budget, and the in-memory event list stops growing with it. What is *not*
        claimed is a bound on the client's own file (``stdout.ndjson``): the client owns that file
        and keeps writing to it, and HFlow does not truncate a file another process is writing -
        that removes bytes nobody has read and can leave a hole. Its measured size is reported
        instead (``peak_raw_bytes``), so the difference between "bounded" and "measured" is
        visible rather than implied.

        Raw chunks are retained before decoding or framing. An over-long line is discarded
        through its next newline: resetting the buffer alone would turn its tail into a new
        protocol record.
        """
        handle = self._handles[invocation_id]
        process = self._processes[invocation_id]
        cap = self.protocol_share_bytes
        with BoundedTextSink(Path(handle.event_log), limit=cap) as sink:
            pending = b""
            discarding = False
            offset = 0
            while True:
                # The file was created before spawn. A failed open/read is not EOF, even after
                # the client exited: the unread suffix may invalidate an otherwise settled turn.
                with stdout_path.open("rb") as source:
                    source.seek(offset)
                    chunk = source.read(CAPTURE_READ_CHUNK)
                if chunk:
                    offset += len(chunk)
                    if offset > self._peak_raw_bytes[invocation_id]:
                        self._peak_raw_bytes[invocation_id] = offset
                    sink.write(chunk)
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        ended = index < len(parts) - 1
                        self._protocol_bytes[invocation_id] += len(part) + int(ended)
                        if not discarding:
                            pending += part
                            if len(pending) > MAX_PENDING_LINE_BYTES:
                                self._unparsed[invocation_id] += 1
                                self._oversized[invocation_id] += 1
                                self._line_counts[invocation_id] += 1
                                if self._line_counts[invocation_id] > self.max_event_records:
                                    self._limit_output(invocation_id, "protocol_record_limit")
                                pending = b""
                                discarding = True
                            elif ended:
                                self._project_line(invocation_id, sink, pending)
                                pending = b""
                        if ended:
                            discarding = False
                    if sink.truncated:
                        self._limit_output(invocation_id, "protocol_byte_limit")
                    continue
                if process.poll() is not None:
                    with stdout_path.open("rb") as source:
                        source.seek(offset)
                        tail = source.read(CAPTURE_READ_CHUNK)
                    if not tail:
                        break
                    continue
                time.sleep(STREAM_POLL_SECONDS)
            if pending and not discarding:
                self._project_line(invocation_id, sink, pending)
            self._overflow[invocation_id] = self._overflow[invocation_id] or sink.truncated
            final_capture = sink.capture()
        self._stdout_captures[invocation_id] = final_capture
        # The drained flag is not set here: ``_consume_stream`` sets it in its ``finally``, after
        # any failure raised below has been recorded, so the flag is always the reader's last write.
        if os.environ.get("HFLOW_DRIVER_DEBUG"):
            print(
                f"driver-debug: {invocation_id} lines={len(self._lines[invocation_id])} "
                f"events={len(self._events[invocation_id])} "
                f"unparsed={self._unparsed[invocation_id]} overflow={self._overflow[invocation_id]} "
                f"oversized_lines={self._oversized[invocation_id]}",
                file=sys.stderr,
                flush=True,
            )

    @property
    def stderr_share_bytes(self) -> int:
        """Bytes of the retention budget HFlow keeps for the client's stderr.

        stderr goes into a pipe this driver owns, so this is a real limit on what is *retained*.
        The client's own protocol file (``stdout.ndjson``) is deliberately **not** trimmed:
        truncating a file another process is writing removes bytes that were never read and can
        leave a hole, so it is not a bound HFlow can honestly enforce. What is bounded is
        everything HFlow keeps; the client's file size is reported as the measured number it is.
        """
        return int(self.max_raw_log_bytes * self.stderr_share_fraction)

    @property
    def protocol_share_bytes(self) -> int:
        """Budget for the retained protocol log (``events.ndjson``)."""
        return max(0, self.max_raw_log_bytes - self.stderr_share_bytes)

    def _project_line(self, invocation_id: str, sink: BoundedTextSink, raw: bytes) -> None:
        """Project a framed line. Raw retention belongs to the reader, before decoding.

        After either protocol limit is crossed, only a constant-size dispatch observation may
        change. Prompt ids, terminals, model state, transcript and cached lines all stop growing.
        """
        if not raw:
            return
        # Numbered before parsing, so unparseable and message-less lines take a number too.
        line_index = self._line_counts[invocation_id]
        self._line_counts[invocation_id] = line_index + 1
        if self._protocol_bytes[invocation_id] > self.protocol_share_bytes:
            self._limit_output(invocation_id, "protocol_byte_limit")
        if line_index >= self.max_event_records:
            self._limit_output(invocation_id, "protocol_record_limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Keep the original bytes, but never turn an invalid wire string into a valid
            # message by replacing bytes with U+FFFD.
            self._unparsed[invocation_id] += 1
            return
        if self._events_capped[invocation_id]:
            # Parse at most the bounded line, without constructing events or retaining its
            # contents. A prompt observed in the discarded tail still proves dispatch.
            if not self._handles[invocation_id].dispatched:
                try:
                    message = json.loads(text)
                except (ValueError, TypeError, RecursionError):
                    message = None
                if isinstance(message, dict) and message.get("method") == "session/prompt":
                    self._note_dispatch(invocation_id)
            return
        self._lines[invocation_id].append(text)
        try:
            observed = project_line(text, len(self._events[invocation_id]), utc_now())
        except Exception:  # noqa: BLE001 - a line the projection cannot read is unparseable
            observed = None
        if observed is None or not observed.parsed:
            self._unparsed[invocation_id] += 1
            return
        if observed.message is not None:
            try:
                self._note_message(invocation_id, observed.message, line_index=line_index)
            except Exception as exc:  # noqa: BLE001 - never kill the reader, never drop the line
                # A message this driver cannot interpret is counted like an unparseable line, so
                # the outcome cannot look clean, and the turn's answer is no longer trusted.
                self._unparsed[invocation_id] += 1
                transcript = self._transcripts.get(invocation_id)
                if transcript is not None and not transcript.rejected:
                    transcript.reject(
                        f"stream line {line_index} could not be interpreted by the driver "
                        f"({type(exc).__name__}), so the turn's answer is not identified"
                    )
                return
        if observed.event is not None:
            self._events[invocation_id].append(observed.event)

    def _limit_output(self, invocation_id: str, reason: str) -> None:
        self._overflow[invocation_id] = True
        self._events_capped[invocation_id] = True
        self._output_limit_reasons[invocation_id].add(reason)

    def _note_dispatch(self, invocation_id: str) -> None:
        handle = self._handles[invocation_id]
        handle.dispatched = True
        handle.dispatched_at = handle.dispatched_at or utc_now()

    def _consume_stderr(self, invocation_id: str, stderr_path: Path) -> None:
        """Drain the client's stderr from its pipe into a bounded sink.

        The child writes into a pipe, so this is an entry-point limit on what HFlow retains and on
        what the client can push: whatever exceeds the stderr share is read, counted and digested
        but not written, and the client cannot fill the disk with it.

        The reader closes the pipe itself once its read loop ends. ``release`` never closes it
        under a reader that is still blocked (see there), so this is where a pipe whose last
        writer outlived the invocation is finally closed.
        """
        process = self._processes[invocation_id]
        cap = self.stderr_share_bytes
        with BoundedTextSink(stderr_path, limit=cap) as sink:
            stream = process.stderr
            status = "no_stream"
            if stream is not None:
                try:
                    while True:
                        try:
                            chunk = stream.read(CAPTURE_READ_CHUNK)
                        except (OSError, ValueError) as exc:
                            status = f"read_failed: {type(exc).__name__}: {exc}"
                            break
                        if not chunk:
                            status = "eof"
                            break
                        sink.write(chunk)
                finally:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            capture = sink.capture()
        if status != "eof" and not capture.failure_reason:
            capture.failure_reason = status
            if status.startswith("read_failed"):
                capture.failed = True
        self._stderr_captures[invocation_id] = capture

    def _note_message(
        self, invocation_id: str, message: dict[str, Any], *, line_index: int = -1
    ) -> None:
        """Record what one wire message means for this invocation.

        Neutral facts, no policy: the dispatch marker and the prompt's request id, the session
        identity (the first prompt's session binds the turn's transcript), the assistant text of
        the turn (which is where a reviewer's verdict actually travels), each terminal response
        together with the request id it answered and where in the stream it arrived, each error
        response that carries an observed prompt id, and what the stream says about the
        session's model option.
        """
        handle = self._handles[invocation_id]
        watch = self._model_watches.get(invocation_id)
        if watch is not None:
            watch.observe(message)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("method") == "session/prompt":
            self._note_dispatch(invocation_id)
            session = _session_of(params)
            if watch is not None:
                watch.bind_prompt(session)
            request_id = message.get("id")
            if request_id is not None:
                prompt_ids = self._prompt_request_ids.setdefault(invocation_id, [])
                prompt_ids.append(request_id)
                if len(prompt_ids) == 1:
                    transcript = self._transcripts.get(invocation_id)
                    if transcript is not None:
                        # The turn's answer is attributed to the session its first prompt named
                        # (an empty name leaves nothing attributable; see ``bind_session``).
                        transcript.bind_session(session)
                self._prompt_sessions[invocation_id] = session
                self._prompt_session_updates.setdefault(invocation_id, {}).setdefault(
                    session, [0, 0]
                )
        elif isinstance(message.get("method"), str):
            # Another request carrying an observed prompt's id: the agent numbers its own requests
            # (permission) from 0 independently of the client, so an error with that id may
            # answer it.
            key = _rpc_id(message.get("id"))
            if key is not None and key in self._prompt_keys(invocation_id):
                self._prompt_id_reused_by.setdefault(invocation_id, {}).setdefault(
                    key, str(message["method"])
                )
        if message.get("method") == "session/update":
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
            counts = self._prompt_session_updates.get(invocation_id, {}).get(_session_of(params))
            if counts is not None:
                counts[0] += 1
                if update.get("sessionUpdate") == "agent_message_chunk":
                    counts[1] += 1
            transcript = self._transcripts.get(invocation_id)
            if transcript is not None:
                # A shape this build cannot read must not kill the reader thread: it is
                # recorded as unusable answer text, so the review stays absent (fail closed).
                try:
                    transcript.observe_update(
                        update,
                        params=params,
                        sequence=len(self._events.get(invocation_id, [])),
                        line_index=line_index,
                    )
                except ReviewDecodeError as exc:
                    transcript.reject(exc.detail)
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("sessionId"), str):
            handle.session_id = result["sessionId"]
        if isinstance(result, dict) and "stopReason" in result:
            session = self._prompt_sessions.get(invocation_id)
            before = (
                self._prompt_session_updates.get(invocation_id, {}).get(session, [0, 0])
                if session is not None
                else [0, 0]
            )
            self._terminal_responses.setdefault(invocation_id, []).append(
                _TerminalResponse(
                    request_id=message.get("id"),
                    stop_reason=result.get("stopReason"),
                    line_index=line_index,
                    session_id=session,
                    updates_before=before[0],
                    message_chunks_before=before[1],
                )
            )
        if "error" in message and not isinstance(message.get("method"), str):
            self._note_prompt_error(invocation_id, message)

    def _prompt_keys(self, invocation_id: str) -> set[tuple[str, Any]]:
        """The observed ``session/prompt`` ids as type-keeping keys (see ``_rpc_id``)."""
        keys = (_rpc_id(value) for value in self._prompt_request_ids.get(invocation_id) or [])
        return {key for key in keys if key is not None}

    def _note_prompt_error(self, invocation_id: str, message: dict[str, Any]) -> None:
        """Keep an error response that carries an observed prompt id.

        ACP v1 reports a failed prompt as an error answering it. Only an id echoed exactly (type
        included, as in ``_prompt_response``) is kept. An error for any other request, including
        the client's own null-id line, says nothing about the turn. JSON-RPC answers a request
        once, so only the first error per id is kept.
        """
        key = _rpc_id(message.get("id"))
        if key is None or key not in self._prompt_keys(invocation_id):
            return
        records = self._prompt_errors.setdefault(invocation_id, [])
        if any(_rpc_id(record.request_id) == key for record in records):
            return
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        code = error.get("code")
        text = error.get("message")
        records.append(
            PromptErrorResponse(
                request_id=message.get("id"),
                code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                message=text[:MAX_PROMPT_ERROR_MESSAGE_CHARS] if isinstance(text, str) else "",
                reused_by=self._prompt_id_reused_by.get(invocation_id, {}).get(key, ""),
            )
        )

    def observe(self, handle: DriverHandle, *, poll_seconds: float = 0.1) -> Iterator[NormalizedEvent]:
        """Yield events as they arrive, until the invocation reaches a terminal state."""
        invocation_id = handle.invocation_id
        index = 0
        deadline = time.monotonic() + self.completion_timeout_seconds
        while True:
            events = self._events.get(invocation_id, [])
            while index < len(events):
                yield events[index]
                index += 1
            if self._is_terminal(invocation_id):
                for event in self._events.get(invocation_id, [])[index:]:
                    yield event
                return
            if time.monotonic() > deadline:
                yield NormalizedEvent(
                    kind=EventKind.OUTCOME_UNKNOWN,
                    sequence=index,
                    at=utc_now(),
                    message="observation deadline exceeded",
                )
                return
            time.sleep(poll_seconds)

    def _is_terminal(self, invocation_id: str) -> bool:
        """Terminal only when the process has exited *and* its output has been drained.

        Without the drain condition a fast-exiting client races the reader thread, and
        observing could return an empty event list for a run that actually settled.
        """
        if invocation_id in self._results:
            return True
        process = self._processes.get(invocation_id)
        if process is None or process.poll() is None:
            return False
        return bool(self._stream_drained.get(invocation_id))

    # -- result collection ---------------------------------------------------

    def collect(self, handle: DriverHandle) -> InvocationResult:
        """Fold the invocation into one result. Never invents success.

        Every path that had a process ends with its boundary emptied and closed and the parent's
        copy of the output file closed: a result is not returned while the invocation's process
        tree is still running, and nothing it opened outlives it.
        """
        invocation_id = handle.invocation_id
        if invocation_id in self._results:
            return self._results[invocation_id]
        if handle.start_cancelled:
            # A stop won the handoff, so no process was created. There is nothing to wait for,
            # and this is terminal: a later start of the same invocation is refused.
            result = InvocationResult(
                invocation_id=invocation_id,
                outcome=InvocationOutcome.CANCELLED,
                prompt_digest=self._prompt_digests.get(invocation_id, ""),
                agent_turns=0,
                limitations=[
                    "stopped before the process was created; no client was launched and no "
                    "model request was made"
                ],
                error_code="cancelled",
                error_message="the invocation was stopped before its process was created",
                raw_ref=str(handle.event_log),
            )
            handle.finished = True
            self._results[invocation_id] = result
            return result
        process = self._processes[invocation_id]
        # The wait is bounded by the invocation's *own* deadline as well as the driver's completion
        # timeout: whichever comes first is when this invocation stops being allowed to run. A
        # client that hangs before it can read its `--timeout` flag is exactly the case the flag
        # cannot cover, and without this the run would keep waiting past the deadline it recorded.
        wait_seconds = float(self.completion_timeout_seconds)
        invocation_deadline = self._invocation_deadlines.get(invocation_id)
        if invocation_deadline is not None:
            wait_seconds = min(wait_seconds, max(0.0, invocation_deadline - time.monotonic()))
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            # The deadline is what ran out, so the client is stopped through the managed process
            # boundary rather than left running: the allowance is already spent, and an orphaned
            # client would keep working on a run that has stopped waiting for it. The result stays
            # OUTCOME_UNKNOWN - what the client might yet have produced is not observed, and calling
            # it a failure would be as invented as calling it a success. The job is closed after the
            # teardown whatever it reported: what it held is recorded first, and a job left open
            # would only keep its handle - and anything the teardown missed - for the controller's
            # whole lifetime.
            with self._teardown_lock(invocation_id):
                stopped, emptied, stop_detail = self._force_stop_client(process, handle)
                self._close_boundary(
                    invocation_id, ExitBoundary(emptied=emptied, left_behind=None, detail=stop_detail)
                )
            close_output_handles(process)
            observation, applied = self._model_facts(invocation_id, rejected=False, complete=False)
            timeout_limitations = [
                "the invocation deadline was reached while waiting for the client; the "
                "managed process boundary was used to stop it",
                stop_detail,
            ]
            if self._surface_notes.get(invocation_id):
                timeout_limitations.append(self._surface_notes[invocation_id])
            _delivered, stdin_note = self._stdin_state(
                invocation_id, join_seconds=STDIN_WRITER_JOIN_SECONDS
            )
            if stdin_note:
                timeout_limitations.append(stdin_note)
            result = InvocationResult(
                invocation_id=invocation_id,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                prompt_digest=self._prompt_digests.get(invocation_id, ""),
                agent_turns=None,
                limitations=timeout_limitations,
                error_code="completion_timeout",
                error_message=(
                    "the client did not exit before the invocation deadline "
                    f"({wait_seconds:.3f}s of waiting); stopped={stopped}"
                ),
                raw_ref=str(handle.event_log),
                model_observation=observation,
                model_applied=applied,
                launch_surfaces=self._launch_surfaces.get(invocation_id),
            )
            self._results[invocation_id] = result
            return result

        # The process can exit a moment before the reader thread has consumed its final
        # output. Judging the run at that instant would drop the stop reason and misreport a
        # completed turn as unknown, so wait for the drain - bounded, never unbounded.
        drain_deadline = time.monotonic() + STREAM_DRAIN_TIMEOUT_SECONDS
        while not self._stream_drained.get(invocation_id) and time.monotonic() < drain_deadline:
            time.sleep(STREAM_POLL_SECONDS)

        # The client exiting is not the invocation being over: anything it started may still be
        # running in the boundary, and the controller is about to fingerprint, freeze, check and
        # review the tree that process can still change. So the boundary is emptied - and closed,
        # with what it held recorded - before a result is returned.
        with self._teardown_lock(invocation_id):
            exit_boundary = self._empty_boundary_after_exit(process, handle)
            self._close_boundary(invocation_id, exit_boundary)
        close_output_handles(process)

        # Read once, before anything derived from the stream. Setting the flag is the reader's last
        # write, so once this is True every value read below is final; while it is False the reader
        # may still be adding lines, and every decision that needs a whole stream (the stream order,
        # the verdict, the model refusal) uses this one answer instead of asking again later.
        #
        # A reader that failed set the flag as well, from its guard: the stream it leaves behind is
        # partial, so every whole-stream decision below treats it as not read to its end.
        #
        # The order of these two reads matters. The reader records its failure *before* it sets the
        # flag, so the flag is read first and the failure second: a failure recorded between the two
        # reads is then seen, while the other order could read "no failure" and then "finished" and
        # judge a partial stream as whole. The failure read here is the only one; nothing below
        # (``_review_output`` included) asks ``_reader_failures`` again.
        reader_finished = bool(self._stream_drained.get(invocation_id))
        reader_failure = self._reader_failures.get(invocation_id)
        drained = reader_finished and reader_failure is None
        # The tree is gone, so a writer still blocked on the pipe fails at once; the wait is short.
        prompt_delivered, stdin_note = self._stdin_state(
            invocation_id, join_seconds=STDIN_WRITER_JOIN_SECONDS
        )
        unparsed = self._unparsed.get(invocation_id, 0)
        oversized = self._oversized.get(invocation_id, 0)
        overflowed = self._overflow.get(invocation_id, False)
        # The stop reason is the one the prompt's own response carries, never "the last stop
        # reason in the stream": a response to some other request settles that request, not this
        # turn. A settled response that answers nothing observed is an unbound completion.
        answered, stop_reason = self._prompt_response(invocation_id)
        unbound = bool(self._terminal_responses.get(invocation_id)) and not answered
        prompt_error = self._prompt_error(invocation_id)
        prompt_error_detail = (
            self._prompt_error_detail(prompt_error, process.returncode, answered, stop_reason)
            if prompt_error is not None
            else ""
        )
        stream_order = self._stream_order(invocation_id, drained and not overflowed)
        receipt = self._receipts.get(invocation_id)

        if receipt is not None and receipt.status == "confirmed_stopped":
            outcome = InvocationOutcome.CANCELLED
            error_code, error_message = "cancelled", receipt.detail
        elif not exit_boundary.emptied:
            # Whatever the stream says, work this invocation started may still be running: a turn
            # that settled is not a result while its process tree has not been seen to stop.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "boundary_not_empty"
            error_message = (
                f"the client exited {process.returncode}, but its managed boundary could not be "
                f"confirmed empty afterwards ({exit_boundary.detail}); work it started may still "
                "be running"
            )
        elif reader_failure is not None:
            # The driver did not read the stream to its end, so nothing below can be judged: the
            # stop reason, the unparseable count and the prompt binding all come from a prefix.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "reader_failed"
            error_message = (
                f"{reader_failure}; the client's output was not read to its end (client exited "
                f"{process.returncode}), so the result cannot be judged from it"
            )
        elif not reader_finished and handle.role != "reviewer":
            # The reader had not finished within STREAM_DRAIN_TIMEOUT_SECONDS of the exit. A
            # settled turn in the prefix read so far says nothing about the lines not yet read.
            # (A reviewer keeps its documented path: COMPLETED with no verdict, below.)
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "stream_not_drained"
            error_message = (
                f"the client's output was not read to its end within "
                f"{STREAM_DRAIN_TIMEOUT_SECONDS}s of the client's exit ({process.returncode}); "
                "a partial stream is not a settled turn"
            )
        elif overflowed:
            # Reported before "unparseable lines": cutting the stream is what makes the tail
            # unreadable, so the cause is named rather than one of its symptoms.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "output_limit_exceeded"
            error_message = (
                "client protocol output exceeded "
                + ", ".join(sorted(self._output_limit_reasons.get(invocation_id) or {"protocol_byte_limit"}))
                + f" (byte budget={self.protocol_share_bytes}, record budget={self.max_event_records}); "
                "protocol state is incomplete, so the result cannot be trusted"
            )
        elif unparsed:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "unparseable_output"
            error_message = f"{unparsed} unparseable line(s) in the client output stream"
            if oversized:
                error_message += f" ({oversized} of them exceeded the {MAX_PENDING_LINE_BYTES} byte line cap)"
        elif prompt_error is not None:
            # ACP v1 reports a failed prompt as a JSON-RPC error answering it (DSH: -32603 "Internal
            # error: turn failed: ..." after the turn ran; "prompt was not queued: ...", invalid
            # params or a content-admission error before any model work). The code and message are
            # recorded, but the outcome stays unknown: whether a model call was made is not
            # observable, and an error is not a stop reason; calling it FAILED is a separate
            # decision (rule 5). A prompt answered with an error and a stop reason was answered
            # twice, so neither answer is taken.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "prompt_error_response"
            error_message = prompt_error_detail
        elif unbound:
            # Something settled, but not this invocation's prompt: whatever the turn did, the
            # stream does not say it finished. Unknown for every role - an implementer's unbound
            # "completion" is no more a candidate than a reviewer's is a verdict.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "unbound_completion"
            error_message = self._unbound_detail(invocation_id)
        elif self._model_rejected_before_prompt(invocation_id, handle, process, drained):
            # Narrow on purpose: a model was passed, the client refused it (or relayed the agent's
            # refusal) and exited with its error, no session/prompt left the client in a complete
            # stream, and the process tree is gone. Nothing reached a model, so this is a definite
            # failure; every other missing stop reason stays unknown below.
            outcome = InvocationOutcome.FAILED
            error_code = "model_rejected_before_prompt"
            error_message = (
                f"the client refused --model {self.launch.model} before sending the prompt "
                f"(exit {process.returncode}): {self._model_watches[invocation_id].client_error}"
            )
        elif stop_reason in {None, ""}:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "no_stop_reason"
            error_message = (
                f"client exited {process.returncode} without a prompt stop reason"
                if process.returncode == 0
                else f"client exited {process.returncode} before the turn settled"
            )
        elif stop_reason not in V1_STOP_REASONS:
            # Outside ACP v1's closed StopReason set (a custom, later-protocol or malformed value):
            # the response does not say how the turn ended in terms this build reads. That is
            # unknown, as the event projection already says - like an unparseable line - not a
            # failure whose meaning is known.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "unknown_stop_reason"
            error_message = (
                f"the prompt's own response settled with stopReason={stop_reason[:80]!r}, which "
                "is not an ACP v1 stop reason"
            )
        elif stop_reason == "end_turn":
            # Turn settlement, not success: DSH settles blocked and aborted turns as end_turn too.
            # COMPLETED says only that the turn ended; checks and review decide acceptance.
            outcome = InvocationOutcome.COMPLETED
            error_code, error_message = None, None
        elif stop_reason == "cancelled" and self._stop_requests.get(invocation_id):
            outcome = InvocationOutcome.CANCELLED
            error_code, error_message = "cancelled", "the harness reported stopReason=cancelled"
        elif stop_reason == "cancelled":
            # Nobody asked this invocation to stop. DSH also settles a prompt as cancelled when it
            # disposes of a session on its own, so this is the harness ending the turn - a failure,
            # not an operator's cancellation.
            outcome = InvocationOutcome.FAILED
            error_code = "cancelled_unrequested"
            error_message = (
                "the harness settled the prompt as cancelled, but no stop was requested for this "
                "invocation"
            )
        else:
            # max_tokens, max_turn_requests, refusal: v1 reasons for a turn that ended without
            # finishing.
            outcome = InvocationOutcome.FAILED
            error_code, error_message = f"stop_reason_{stop_reason}", f"turn settled as {stop_reason}"

        if outcome is InvocationOutcome.COMPLETED and not prompt_delivered:
            # The turn settled, but the client did not receive the whole prompt: what it settled
            # is not the task whose digest is reported. Never a success.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "prompt_write_incomplete"
            error_message = stdin_note

        limitations = [
            "candidate content is not extracted here; verification runs on the workspace",
            "billed usage is not observable on this transport",
        ]
        stderr_capture = self._stderr_captures.get(invocation_id)
        if stderr_capture is not None:
            limitations.append(
                f"client stderr retained {stderr_capture.retained_bytes} of "
                f"{stderr_capture.total_bytes} bytes at {stderr_capture.path}"
                + (" (truncated: the retained file is the head only)" if stderr_capture.truncated else "")
            )
        if overflowed:
            limitations.append(
                "protocol state stopped accumulating after "
                + ", ".join(sorted(self._output_limit_reasons.get(invocation_id) or {"protocol_byte_limit"}))
                + "; model and stream-order observations are incomplete"
            )
        if not exit_boundary.emptied:
            limitations.append(f"boundary_not_empty: {exit_boundary.detail}")
        elif exit_boundary.left_behind:
            limitations.append(
                f"descendants_terminated_after_client_exit: {exit_boundary.left_behind} (still in "
                "the managed boundary after the client exited; terminated through it before this "
                "result was returned)"
            )
        if reader_failure is not None:
            limitations.append(
                f"reader_failed: {reader_failure}; the protocol stream was not read to its end"
            )
        elif not reader_finished:
            limitations.append(
                f"stream_not_drained: the client's output was not read to its end within "
                f"{STREAM_DRAIN_TIMEOUT_SECONDS}s of the client's exit"
            )
        if stdin_note:
            limitations.append(stdin_note)
        if not handle.dispatched:
            limitations.append(
                "no session/prompt was observed; dispatch is unknown in the incomplete protocol state"
                if overflowed
                else "no session/prompt was observed; the harness never received the task"
            )
        if unbound:
            limitations.append(
                f"unbound_completion: {self._unbound_detail(invocation_id)}; the turn's completion "
                "is not bound to this invocation's prompt"
            )
        if prompt_error is not None:
            limitations.append(
                f"prompt_error_response: {prompt_error_detail}; ACP v1 reports a failed prompt this "
                "way, but whether a model call was made before it failed is not observable here, so "
                "the outcome is unknown rather than failed"
            )
        for record in self._prompt_errors.get(invocation_id) or []:
            if record.reused_by:
                limitations.append(
                    f"prompt_error_unattributed: an error response ({record.code_text}: "
                    f"{record.message!r}) carries the session/prompt request id "
                    f"{record.request_id!r}, which a {record.reused_by} request also used after the "
                    "prompt was sent; JSON-RPC ids are per direction, so it may answer that request "
                    "and is not attributed to the prompt"
                )
        if stream_order is not None and stream_order.updates_after_prompt_response:
            # Recorded, never judged here: only a reviewer's after-response message changes what
            # the turn yields (see ``_review_output``). Not prefixed ``review_``, which
            # ``review_input_error`` reads as a wire failure.
            limitations.append(
                f"updates_after_prompt_response={stream_order.updates_after_prompt_response}: "
                f"{stream_order.updates_after_prompt_response} session/update notification(s) "
                f"({stream_order.message_chunks_after_prompt_response} agent_message_chunk) for "
                "the prompt's session arrived after the session/prompt response on stream line "
                f"{stream_order.prompt_response_line}; stable ACP v1 sends a turn's updates before "
                "its response, so these are outside the settled turn"
            )
        transcript = self._transcripts.get(invocation_id)
        if transcript is not None and transcript.skipped_other_session:
            # Recorded for either role. Not prefixed ``review_``: the transcript's own rejection
            # carries the reviewer's ``review_invalid`` note, and ``review_input_error`` would read
            # this prefix as a second wire failure.
            limitations.append(
                f"agent_message_chunk_other_session={transcript.skipped_other_session}: "
                f"{transcript.skipped_other_session} agent_message_chunk update(s) named a session "
                f"other than {transcript.session_id!r}, the one the first session/prompt request "
                "named, or no session; none is the turn's answer, and a reviewer's final answer "
                "is then not identified"
            )
        review, note = self._review_output(handle, outcome, drained, reader_failure)
        if note:
            limitations.append(note)
        if self._surface_notes.get(invocation_id):
            limitations.append(self._surface_notes[invocation_id])
        watch = self._model_watches.get(invocation_id)
        if watch is not None:
            if watch.skipped_updates or watch.skipped_sets:
                limitations.append(
                    f"model_other_session: ignored {watch.skipped_updates} configuration update(s) "
                    f"and {watch.skipped_sets} model set request(s) without the bound session"
                )
            if watch._ambiguous:
                limitations.append("model_binding_unknown: session or request attribution is ambiguous")
        observation, applied = self._model_facts(
            invocation_id, rejected=error_code == "model_rejected_before_prompt", complete=drained
        )
        result = InvocationResult(
            invocation_id=invocation_id,
            outcome=outcome,
            candidate=None,
            review=review,
            prompt_digest=self._prompt_digests.get(invocation_id, ""),
            agent_turns=1 if handle.dispatched else (None if overflowed else 0),
            provider_billed_tokens=None,
            reported_cost=None,
            limitations=limitations,
            raw_ref=str(handle.event_log),
            error_code=error_code,
            error_message=error_message,
            model_observation=observation,
            model_applied=applied,
            stream_order=stream_order,
            launch_surfaces=self._launch_surfaces.get(invocation_id),
        )
        handle.finished = True
        self._results[invocation_id] = result
        return result

    def _model_rejected_before_prompt(
        self, invocation_id: str, handle: DriverHandle, process: subprocess.Popen, drained: bool
    ) -> bool:
        """The one no-stop-reason case that is a definite failure. All conditions must hold.

        Called after the boundary, overflow, unparseable-line and unbound-completion checks, so
        the boundary is known empty and the retained stream is whole and parsed. On top of that:
        a model was passed; the stream was drained (``collect``'s one reading of the flag); the
        session was created; no ``session/prompt`` left the client; the model was refused - by
        the client before any change request (no model option advertised, or the value not among
        the advertised ones), or by the agent answering that request with an error; the client
        printed its own JSON-RPC error line (null id); and it exited non-zero.
        """
        watch = self._model_watches.get(invocation_id)
        return bool(
            self.launch.model
            and watch is not None
            and drained
            and process.returncode not in (None, 0)
            and not handle.dispatched
            and not self._prompt_request_ids.get(invocation_id)
            and watch.session_created
            and watch.refused()
            and watch.client_error
        )

    def _model_facts(
        self, invocation_id: str, *, rejected: bool, complete: bool = True
    ) -> tuple[ModelObservation | None, ModelApplied | None]:
        """The invocation's model observation and what became of the requested model."""
        if not complete or self._overflow.get(invocation_id):
            return None, ModelApplied.UNKNOWN if self.launch.model else ModelApplied.NOT_PASSED
        watch = self._model_watches.get(invocation_id)
        if watch is None:
            return None, None
        return watch.observation(), watch.applied(rejected=rejected)

    def _prompt_response(self, invocation_id: str) -> tuple[bool, str | None]:
        """``(answered, stop_reason)`` of the response that answers this invocation's prompt.

        A JSON-RPC response is not task completion by itself; it is completion of one request.
        The turn's request is the observed ``session/prompt`` - the last one, if the stream
        carried more than one - and only the response with that request's id settles it. The
        recorded runtime answers the prompt with the same id; this keeps that association rather
        than trusting "the stream ended" or "some stop reason arrived". JSON-RPC answers a request
        once, so the first response with the id is its answer.

        ``answered`` is False when no response carries that id, including when no prompt with an
        id was observed at all: then there is nothing a completion could be bound to.
        """
        response = self._bound_response(invocation_id)
        if response is None:
            return False, None
        return True, None if response.stop_reason is None else str(response.stop_reason)

    def _bound_response(self, invocation_id: str) -> _TerminalResponse | None:
        """The response ``_prompt_response`` binds the turn to, or ``None``."""
        prompt_ids = self._prompt_request_ids.get(invocation_id) or []
        if not prompt_ids:
            return None
        prompt_id = prompt_ids[-1]
        for response in self._terminal_responses.get(invocation_id) or []:
            # The type is compared too: ``2``, ``2.0`` and ``True`` are equal in Python, and an id
            # that is not echoed exactly does not answer the request.
            if type(response.request_id) is type(prompt_id) and response.request_id == prompt_id:
                return response
        return None

    def _stream_order(self, invocation_id: str, drained: bool) -> StreamOrder | None:
        """Where the bound prompt response fell in the stream, and what came after it.

        Counted for the session the latest ``session/prompt`` named when that response arrived -
        for ``exec``, the one session of its one prompt - from the response to the end of the
        stream. ``None`` when no response answers the prompt, or none had been preceded by a
        prompt: there is nothing to count from. ``None`` too when the reader had not finished the
        stream when ``collect`` read the flag (``drained``): "nothing followed the response" is a
        fact only about a stream read to its end, and ``collect`` can fold a result after
        STREAM_DRAIN_TIMEOUT_SECONDS without that.
        """
        if not drained:
            return None
        response = self._bound_response(invocation_id)
        if response is None or response.session_id is None:
            return None
        updates, chunks = self._prompt_session_updates[invocation_id][response.session_id]
        return StreamOrder(
            prompt_response_line=response.line_index,
            updates_after_prompt_response=updates - response.updates_before,
            message_chunks_after_prompt_response=chunks - response.message_chunks_before,
        )

    def _unbound_detail(self, invocation_id: str) -> str:
        """Which ids were seen, for a completion that answers no observed prompt."""
        prompt_ids = self._prompt_request_ids.get(invocation_id) or []
        responses = self._terminal_responses.get(invocation_id) or []
        response_ids = [response.request_id for response in responses]
        if not prompt_ids:
            return (
                f"terminal response id(s) {response_ids!r} arrived, but no session/prompt request "
                "with an id was observed"
            )
        return (
            f"no response answers the observed session/prompt request id {prompt_ids[-1]!r}; "
            f"terminal response id(s) {response_ids!r}"
        )

    def _prompt_error(self, invocation_id: str) -> PromptErrorResponse | None:
        """The attributable error answering this invocation's prompt, or ``None``.

        The prompt is the same request ``_prompt_response`` binds to (the last observed
        ``session/prompt``). An error whose id a peer request reused is not returned: it may answer
        that request instead.
        """
        prompt_ids = self._prompt_request_ids.get(invocation_id) or []
        if not prompt_ids:
            return None
        key = _rpc_id(prompt_ids[-1])
        for record in self._prompt_errors.get(invocation_id) or []:
            if _rpc_id(record.request_id) == key and not record.reused_by:
                return record
        return None

    @staticmethod
    def _prompt_error_detail(
        record: PromptErrorResponse, returncode: int | None, answered: bool, stop_reason: str | None
    ) -> str:
        """What answered the prompt with an error.

        The agent's message is repr-quoted, so it cannot carry control or ANSI sequences into
        ``status``.
        """
        detail = (
            f"session/prompt request {record.request_id!r} was answered with JSON-RPC error "
            f"{record.code_text}: {record.message!r}; client exited {returncode}"
        )
        if answered:
            detail += (
                f"; the same request id was also answered with stopReason={stop_reason!r}, and a "
                "request is answered once, so neither answer is taken"
            )
        return detail

    def _review_output(
        self,
        handle: DriverHandle,
        outcome: InvocationOutcome,
        drained: bool,
        reader_failure: str | None = None,
    ) -> tuple[ReviewOutput | None, str]:
        """Decode a verdict from a reviewer's *final answer*, or explain why there is none.

        This is the role-specific adaptation boundary, and the only place a review can come
        from. It grants no authority: a decoded verdict is validated against the canonical
        model and handed to the controller, which decides what it means. Anything else -
        wrong role, unfinished turn, unreadable answer - yields ``None`` plus a machine
        readable reason, so the controller never has to read a model's prose to find out
        whether the wire was intact.

        ``drained`` is ``collect``'s one reading of the reader's flag, the same one the stream
        order was decided from, and ``reader_failure`` is its one reading of the reader's failure
        (read after the flag). Neither is read again here, so the verdict cannot be decided from a
        different answer than the outcome was.
        """
        transcript = self._transcripts.get(handle.invocation_id)
        if transcript is None:
            return None, ""
        if handle.role != "reviewer":
            # A verdict-shaped object in an implementer's output is not a review: only the
            # review invocation may produce review evidence. Nothing to explain here - the
            # absent review is the expected result for this role.
            return None, ""
        if not drained:
            # Asked before the transcript is read at all: while the reader may still be adding
            # lines, the transcript can still change (a later chunk can reject it, which also
            # clears what the after-response check counts), and what followed the response is not
            # known. Once drained, nothing changes it any more.
            if reader_failure is not None:
                return None, (
                    f"review_{REVIEW_AMBIGUOUS}: the client's output was not read to its end "
                    f"({reader_failure}), so what followed the session/prompt response is not known and "
                    "no verdict is decoded"
                )
            return None, (
                f"review_{REVIEW_AMBIGUOUS}: the client's output was not read to its end within "
                f"{STREAM_DRAIN_TIMEOUT_SECONDS}s of the client's exit, so what followed the "
                "session/prompt response is not known and no verdict is decoded"
            )
        if transcript.truncated:
            return None, (
                f"review_{REVIEW_MISSING}: the reviewer's assistant output exceeded "
                f"{MAX_ANSWER_BYTES} bytes and was not retained"
            )
        if transcript.rejected:
            return None, f"review_{REVIEW_INVALID}: {transcript.rejected}"
        answer = transcript.final_answer()
        if answer is None:
            return None, (
                f"review_{REVIEW_MISSING}: no agent_message_chunk was observed for this "
                "invocation, so there is no reviewer answer to decode"
            )

        # Transport validity is judged first and separately: accepted-looking text in an
        # unfinished, cancelled or truncated turn can never authorize acceptance.
        if outcome is not InvocationOutcome.COMPLETED:
            return None, (
                f"review_{REVIEW_MISSING}: the reviewer turn did not complete "
                f"({outcome.value}); its text is not a verdict"
            )
        response = self._bound_response(handle.invocation_id)
        if response is None:
            # A completed outcome is only ever taken from the prompt's own response, so this holds
            # already; it is asked again because this is the one place a verdict can come from.
            return None, (
                f"review_{REVIEW_MISSING}: the terminal response was not matched to an observed "
                "session/prompt request for this invocation"
            )
        trailing = transcript.chunks_after(response.line_index)
        if trailing:
            # ACP v1 sends a turn's updates before its response. Text that arrived after it is
            # outside the settled turn, and it can replace, extend or be the final message, so the
            # turn's final answer is not identified: no verdict, rather than a guess either way.
            # The check reads the transcript, so it catches every chunk the transcript kept; a chunk
            # for another session was excluded by its session binding and is noted in ``collect``.
            return None, (
                f"review_{REVIEW_AMBIGUOUS}: {trailing} agent_message_chunk update(s) arrived "
                f"after the session/prompt response on stream line {response.line_index}, so the "
                "turn's final answer is not identified and no verdict is decoded"
            )
        try:
            review = decode_review(answer.text)
        except ReviewDecodeError as exc:
            return None, f"review_{exc.kind}: {exc.detail} (answer {len(answer.text)} chars)"
        return review, f"review_decoded: {review.verdict} from the final agent message"

    # -- stop ----------------------------------------------------------------

    def _stop_state(self, invocation_id: str) -> str:
        """Where this invocation is, as one answer taken under the gate.

        ``"live"`` - a process exists (published) and the caller may act on it outside the gate;
        ``"pending"`` - a spawn holds the gate right now, so "no handle" is not yet an answer;
        ``"stopped_before_start"`` - no process exists and the stop is recorded, so none will be
        created.

        The process is checked **first**: once a child exists, a recorded stop does not make it
        disappear - it is the thing the stop still has to terminate. Asking about the stop flag
        first would report a confirmed stop of an invocation that is in fact running.
        """
        if invocation_id in self._processes:
            return "live"
        if invocation_id in self._spawn_pending:
            return "pending"
        if self._stop_requests.get(invocation_id):
            return "stopped_before_start"
        return "absent"

    def _await_spawn_publication(self, invocation_id: str) -> None:
        """Wait, outside the held gate, for an in-flight spawn to publish or to be cancelled.

        Called with the gate held and returns with it held. ``Condition.wait`` releases the gate
        while waiting, so the spawning thread finishes its publication and wakes this thread up;
        there is no path here that holds the gate across the wait, and no re-entrant acquisition
        that could deadlock on it.
        """
        deadline = time.monotonic() + BOUNDARY_EMPTY_TIMEOUT_SECONDS
        while self._stop_state(invocation_id) == "pending":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._gate.wait(timeout=remaining)

    def cancel_handle(self, handle: DriverHandle) -> CancellationReceipt:
        """Request a stop and report facts. Idempotent; never sends another prompt.

        The request is recorded under the same gate the spawn uses, and a stop that arrives while
        a spawn is in flight waits for that spawn to publish instead of concluding that there is
        nothing to stop. Whichever side takes the gate first decides:

        * the stop first - ``_stop_requests`` is set, the spawn refuses, and this reports a
          confirmed stop of an invocation that never had a process;
        * the spawn first - this waits outside the held lock for the publication, then terminates
          the process it finds, reporting ``mechanism=forced``.

        A client that had already exited is not a stopped invocation either: what it started can
        still be running in the boundary, so the boundary is asked, not the client (see
        ``_stop_exited_client``).

        The wait happens with the gate released and never covers the invocation's result: a stop
        is not blocked by the work it is stopping.
        """
        invocation_id = handle.invocation_id
        previous = self._receipts.get(invocation_id)
        if previous is not None:
            return previous

        # Declared before taking the gate: a spawn that is already inside it re-reads this and
        # refuses to create a process, which is the whole point of recording it first.
        with self._gate:
            self._stop_requests[invocation_id] = True
            self._await_spawn_publication(invocation_id)
            state = self._stop_state(invocation_id)
            if state != "live":
                receipt = CancellationReceipt(
                    invocation_id=invocation_id,
                    status="confirmed_stopped",
                    mechanism="none",
                    local_process_stopped=True,
                    detail=(
                        "the invocation was stopped before its process was created; no child ran "
                        "and none will be started"
                    ),
                )
                self._receipts[invocation_id] = receipt
                return receipt
            process = self._processes[invocation_id]

        # Decided under the teardown lock rather than the gate: ``collect`` may be emptying and
        # closing this boundary right now, and "has the client exited?" is only a stable question
        # once neither side is halfway through that. The receipt is recorded before the lock is
        # released, so a ``collect`` waiting on it reads the stop it waited for.
        with self._teardown_lock(invocation_id):
            previous = self._receipts.get(invocation_id)
            if previous is not None:
                return previous  # a concurrent stop finished while this one waited
            if process.poll() is not None:
                receipt = self._stop_exited_client(process, handle)
            else:
                receipt = self._stop_live_client(process, handle)
            self._receipts[invocation_id] = receipt
        return receipt

    def _stop_live_client(
        self, process: subprocess.Popen, handle: DriverHandle
    ) -> CancellationReceipt:
        """Stop a running client through its boundary. Called with the teardown lock held."""
        invocation_id = handle.invocation_id
        # The pinned exec sends session/cancel only on SIGINT/SIGTERM/SIGHUP, none of which HFlow
        # can deliver to this client, so the only mechanism available is the managed process
        # boundary. Stated, not implied.
        detail_prefix = (
            "cooperative cancel is unavailable for this launcher (acpx cancel targets a "
            "persisted session's queue owner; exec cancels only on SIGINT/SIGTERM/SIGHUP, which "
            "HFlow cannot deliver to its CREATE_NEW_PROCESS_GROUP child - Ctrl+Break arrives as "
            "SIGBREAK, which acpx does not handle); the managed process boundary was terminated"
        )
        stopped, emptied, detail = self._force_stop_client(process, handle)
        # The three-way status is the *boundary's* answer, not a simplification of it: an emptied
        # boundary means the managed process tree is gone, `still_running` means it is not, and
        # `unknown` means the boundary emptied but the process could not be confirmed gone. The
        # forced-stop helper returns both facts so this stays exactly what it was.
        if emptied and stopped:
            status = "confirmed_stopped"
        elif not emptied:
            status = "still_running"
        else:
            status = "unknown"

        receipt = CancellationReceipt(
            invocation_id=invocation_id,
            status=status,  # type: ignore[arg-type]
            mechanism="forced" if status == "confirmed_stopped" else "none",
            local_process_stopped=stopped,
            detail=(
                f"{detail_prefix}; boundary={handle.boundary_kind}, "
                f"dispatched={handle.dispatched}, boundary_empty={emptied}"
            ),
        )
        if status == "confirmed_stopped":
            # Keep exit and event evidence, then release the boundary so nothing lingers. The
            # client went down with its tree, so nothing was left behind after it exited.
            self._close_boundary(
                invocation_id, ExitBoundary(emptied=True, left_behind=0, detail=detail)
            )
        return receipt

    def _stop_exited_client(
        self, process: subprocess.Popen, handle: DriverHandle
    ) -> CancellationReceipt:
        """A stop for an invocation whose client had already exited. Teardown lock held.

        The parent exiting is not the boundary being empty, so this asks the boundary: whatever the
        client left running is terminated through it, and the stop is confirmed only once the job
        is empty. ``mechanism`` says what *this* stop did - ``forced`` when it had to kill something,
        ``none`` when the tree was already empty, including when ``collect`` emptied it first. A
        boundary that cannot be emptied stays open, so it can still be observed and torn down.
        """
        invocation_id = handle.invocation_id
        boundary = self._boundaries.get(invocation_id)
        was_open = boundary is not None and boundary.handle is not None
        exit_boundary = self._empty_boundary_after_exit(process, handle)
        if exit_boundary.emptied:
            status = "confirmed_stopped"
            mechanism = "forced" if was_open and exit_boundary.left_behind else "none"
            local_process_stopped: bool | None = True
            self._close_boundary(invocation_id, exit_boundary)
        elif was_open:
            status, mechanism, local_process_stopped = "still_running", "none", False
        else:
            # Closed earlier without being confirmed empty: kill-on-close was the last action
            # available, and nothing observed whether it worked.
            status, mechanism, local_process_stopped = "unknown", "none", None
        return CancellationReceipt(
            invocation_id=invocation_id,
            status=status,  # type: ignore[arg-type]
            mechanism=mechanism,  # type: ignore[arg-type]
            local_process_stopped=local_process_stopped,
            detail=(
                "the invocation had already exited when the stop was requested; "
                f"{exit_boundary.detail}; boundary={handle.boundary_kind}, "
                f"dispatched={handle.dispatched}, boundary_empty={exit_boundary.emptied}"
            ),
        )

    def _empty_boundary_after_exit(
        self, process: subprocess.Popen, handle: DriverHandle
    ) -> ExitBoundary:
        """The client has exited: terminate what it left in the boundary and report the result.

        A client that exits cleanly can still leave a tool process, a dev server or part of its
        own bridge running in the job. This counts what is left, terminates it through the
        boundary (the same teardown as a forced stop), and reports whether the job is now empty.
        It does not close the boundary; the caller decides that. Called with the teardown lock
        held.

        A boundary that is already closed cannot be asked again, so the answer recorded when it
        was closed is returned instead - and a closed boundary with no record is not reported
        empty.
        """
        invocation_id = handle.invocation_id
        boundary = self._boundaries.get(invocation_id)
        if boundary is None or boundary.handle is None:
            recorded = self._exit_boundaries.get(invocation_id)
            if recorded is not None:
                return recorded
            if boundary is not None and boundary.kind == "direct_child_only":
                # No Job Object on this platform: the boundary *is* the direct child, and its exit
                # is all it can observe. The kind is stated; nothing is claimed about descendants.
                return ExitBoundary(
                    emptied=True,
                    left_behind=0,
                    detail=f"boundary={boundary.kind}: the client exited; descendants are not tracked",
                )
            return ExitBoundary(
                emptied=False,
                left_behind=None,
                detail=(
                    "the boundary was closed before anything confirmed it empty; whether the "
                    "client left a process running is unknown"
                ),
            )
        left_behind = boundary.active_processes()
        if left_behind == 0:
            return ExitBoundary(
                emptied=True,
                left_behind=0,
                detail=f"boundary={boundary.kind} was empty after the client exited",
            )
        _stopped, emptied, stop_detail = self._force_stop_client(process, handle)
        counted = (
            f"{left_behind} process(es) were still in the boundary after the client exited"
            if left_behind is not None
            else "the boundary could not report how many processes it held after the client exited"
        )
        return ExitBoundary(
            emptied=bool(emptied), left_behind=left_behind, detail=f"{counted}; {stop_detail}"
        )

    def _force_stop_client(
        self, process: subprocess.Popen, handle: DriverHandle
    ) -> tuple[bool, bool, str]:
        """Stop a client's process tree through the managed boundary. One implementation.

        Used by an operator-requested stop, by an expired invocation deadline, and when a client
        exited but left processes in its boundary. The mechanism is identical and the *meaning* is
        not, which is why the caller records the meaning: a stop is "a human ended this", a
        deadline is "we stopped waiting", the last is "the invocation is over, its leftovers are
        not". None is a cooperative protocol cancel: the pinned ``exec`` cancels only on
        SIGINT/SIGTERM/SIGHUP, which HFlow cannot deliver to this client (see the module
        docstring).

        Returns ``(stopped, emptied, detail)``. ``stopped`` is true only when the boundary was
        emptied *and* the process is gone; ``emptied`` is reported separately because
        ``cancel_handle``'s three-way status distinguishes "the tree is gone" from "it is not" from
        "emptied but unconfirmed", and collapsing those would change a stop report.

        **The parent exiting is not the boundary being empty.** The whole point of the boundary is
        that the client may have started descendants of its own, and they stay in the job after
        their parent goes. An earlier version returned early when the parent had exited - which
        reported ``emptied=True`` while the job still held a live child, and skipped the one action
        that would have stopped it. Every path below therefore ends at the boundary: whether the
        client is still running, has just exited, or had already exited before this was called, the
        question "is anything left in the tree?" is answered by the job and by nothing else.
        """
        invocation_id = handle.invocation_id
        boundary = self._boundaries.get(invocation_id)
        if boundary is None:
            # No boundary to consult. That is only conclusive when the process is gone as well:
            # reporting an empty tree on the strength of a missing handle would be a guess.
            if process.poll() is not None:
                return (
                    True,
                    True,
                    "the client had already exited and no managed boundary was recorded",
                )
            return False, False, "no managed process boundary was recorded for this invocation"

        if handle.dispatched and process.poll() is None:
            # A bounded wait, not a request: the stdin writer closes the client's stdin once the
            # task is written (``_feed_stdin``), so there is no EOF left to send and nothing here
            # asks it to stop. A client that is about to exit on its own gets this long before the
            # boundary is terminated. The close is a defensive no-op for that already-closed pipe,
            # and is skipped while the writer still owns it.
            self._close_client_stdin(process, invocation_id)
            try:
                process.wait(timeout=FORCE_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass

        # Always ask the boundary, even when the parent is already gone: a descendant can outlive
        # it, and the job is the only thing that knows.
        boundary.terminate()
        emptied = boundary.wait_empty(BOUNDARY_EMPTY_TIMEOUT_SECONDS)
        parent_gone = self._await_process_gone(process.pid, process=process)
        stopped = bool(emptied and parent_gone)
        exit_detail = (
            f"client exit={process.poll()}"
            if process.poll() is not None
            else "client still running when the boundary was terminated"
        )
        return (
            stopped,
            bool(emptied),
            f"boundary={handle.boundary_kind}, boundary_empty={emptied}, stopped={stopped}, "
            f"{exit_detail}",
        )

    @staticmethod
    def _await_process_gone(
        pid: int,
        settle_seconds: float = FORCE_STOP_GRACE_SECONDS,
        *,
        process: subprocess.Popen | None = None,
    ) -> bool:
        """Has this process reached its signalled state? Polled, not waited once.

        ``process_gone`` asks whether the process *object is signalled*, which is not the same
        question as whether the object still exists: Windows keeps a process object alive while any
        handle to it is open, so a terminated process can still be opened and still read as alive.
        The two are therefore stated apart rather than conflated - this method is about the signal,
        and nothing here claims a cause for the delay.

        What was measured on this machine, for a client terminated through the managed boundary: at
        the instant the teardown returned, the job held no processes and the client's exit code
        already read its real code (1) instead of ``STILL_ACTIVE``; an immediate un-timed check of
        the signal said "alive"; the same check with 500 ms of patience said "gone"; and the process
        object was no longer openable about 50 ms later. The delay is a window in which the signal
        has not yet arrived, so a single immediate check reports a teardown that has already
        happened as if it had not - the one direction of error this path exists to avoid: an
        operator deciding whether to re-dispatch needs "the tree is gone", not "the tree was gone a
        moment after I asked".

        Only an observed exit counts. ``process_gone`` answers ``None`` when Windows will not say
        (the process exists but cannot be opened, or the wait failed). That answer returns at
        once, so it is asked again once per poll step up to the same bound, and is then reported
        as not gone. It is never rounded up to gone. When the driver holds the client's ``Popen``,
        ``process.poll()`` reads the same signal through the handle ``Popen`` already owns, so an
        exit it reports is an observed exit even when re-opening the pid is refused.

        Bounded by the same grace the forced stop already uses, so an unconfirmable process keeps
        reporting unknown rather than being waited on forever.
        """
        deadline = time.monotonic() + settle_seconds
        while True:
            if process is not None and process.poll() is not None:
                return True  # observed through our own handle, no re-open needed
            gone = process_gone(pid, 0.05)
            if gone is True:
                return True
            if time.monotonic() >= deadline:
                return False
            if gone is None:
                # An unanswered check returns immediately: wait out the step a timed check takes.
                time.sleep(0.05)

    def _close_client_stdin(self, process: subprocess.Popen, invocation_id: str | None = None) -> None:
        writer = self._threads.get(f"{invocation_id}:stdin") if invocation_id else None
        if writer is not None and writer.is_alive():
            # The writer owns stdin while it runs: closing a buffered writer here would wait on
            # the lock its blocked write holds. The boundary teardown is what unblocks it.
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass

    def _teardown_lock(self, invocation_id: str) -> threading.Lock:
        """The lock that serializes this invocation's boundary teardown (see ``__init__``)."""
        return self._teardown_locks.setdefault(invocation_id, threading.Lock())

    def _close_boundary(self, invocation_id: str, record: ExitBoundary | None = None) -> None:
        """Close the boundary, keeping ``record`` as what it held when it was closed.

        Only the first close of an open boundary records: once closed it cannot be asked again,
        so a later caller must not overwrite what was actually observed. Closing an already
        closed boundary does nothing.
        """
        boundary = self._boundaries.get(invocation_id)
        if boundary is None:
            return
        if record is not None and boundary.handle is not None:
            self._exit_boundaries.setdefault(invocation_id, record)
        boundary.close()

    # -- reconcile -----------------------------------------------------------

    def reconcile_handle(self, handle: DriverHandle) -> ReconcileResult:
        """Conservative: reads recorded facts only, starts nothing, sends nothing."""
        invocation_id = handle.invocation_id
        process = self._processes.get(invocation_id)
        boundary = self._boundaries.get(invocation_id)
        if process is None:
            return ReconcileResult(
                invocation_id=invocation_id,
                outcome=ReconcileOutcome.NOT_STARTED,
                detail="no process was ever recorded for this invocation",
                protocol_cancel_supported=self.protocol_cancel_supported,
            )
        # ``process_gone`` is True only for an observed exit. An unanswered query (None) leaves a
        # process that its own handle still sees running reported as running.
        alive = process.poll() is None and process_gone(process.pid) is not True
        recorded = self._results.get(invocation_id)
        if alive:
            return ReconcileResult(
                invocation_id=invocation_id,
                outcome=ReconcileOutcome.STILL_RUNNING,
                detail="the managed process is still running",
                local_process_alive=True,
                protocol_cancel_supported=self.protocol_cancel_supported,
            )
        if recorded is not None:
            outcome = (
                ReconcileOutcome.FINISHED_RESULT_UNPROCESSED
                if recorded.outcome is InvocationOutcome.COMPLETED
                else ReconcileOutcome.UNKNOWN
            )
            return ReconcileResult(
                invocation_id=invocation_id,
                outcome=outcome,
                detail=f"process exited {process.returncode}; recorded outcome {recorded.outcome.value}",
                local_process_alive=False,
                protocol_cancel_supported=self.protocol_cancel_supported,
            )
        active = boundary.active_processes() if boundary is not None else None
        return ReconcileResult(
            invocation_id=invocation_id,
            outcome=ReconcileOutcome.UNKNOWN,
            detail=(
                f"process exited {process.returncode} with no recorded result; "
                f"boundary active processes={active}; a gone process does not imply success"
            ),
            local_process_alive=False,
            protocol_cancel_supported=self.protocol_cancel_supported,
        )

    # -- HarnessDriver compatibility ----------------------------------------

    def start(self, request: InvocationRequest) -> InvocationResult:
        """Blocking form for callers that do not need the live handle."""
        handle = self.start_handle(request)
        return self.collect(handle)

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        """Stop by invocation id, through the same gate as ``cancel_handle``.

        A missing handle is **not** an answer here: the invocation may be inside the spawn gate
        right now, with its process about to exist. This used to return ``unknown`` immediately
        - bypassing the gate and forgetting the stop - which let a child be created after the
        controller had already recorded that the run was stopped.

        So the request is recorded first, under the gate, and only then is the handle looked up.
        No handle *and* no spawn in flight means nothing was ever started for this id, which is
        still reported as ``unknown``: that is a real fact about an unknown invocation, not a
        stop that was dropped.
        """
        with self._gate:
            self._stop_requests[invocation_id] = True
            self._await_spawn_publication(invocation_id)
            handle = self._handles.get(invocation_id)
        if handle is None:
            return CancellationReceipt(
                invocation_id=invocation_id,
                status="unknown",
                mechanism="none",
                detail=(
                    "no handle for this invocation and no spawn in flight; nothing was stopped"
                ),
            )
        return self.cancel_handle(handle)

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        handle = self._handles.get(invocation_id)
        if handle is None:
            return ReconcileResult(
                invocation_id=invocation_id,
                outcome=ReconcileOutcome.NOT_STARTED,
                detail="no handle for this invocation",
                protocol_cancel_supported=self.protocol_cancel_supported,
            )
        return self.reconcile_handle(handle)

    # -- shutdown ------------------------------------------------------------

    def release(self, invocation_id: str) -> None:
        """Drop an invocation: close its boundary, pipes and log handles. Safe to call again.

        If the process is somehow still alive this terminates it through the boundary. That
        is a cleanup of a *managed* process, not a cancellation claim, so it never writes a
        receipt - a stop that was not confirmed stays unconfirmed. Recorded facts (result,
        receipt, events, what the boundary held) are kept, so a later stop or reconcile still
        answers from them.

        It never blocks on the client's pipes. Every reader join is bounded, and a stream whose
        reader is still alive afterwards is not closed here: ``BufferedReader.close`` takes the
        lock the blocked ``read`` holds, so it would wait until every holder of the pipe's write
        end exits - a client the boundary could not run down, or any holder outside it, would hang
        the controller after it already recorded the outcome. That stream is left to its (daemon)
        reader, which closes it on EOF or error, and the fact is kept in ``release_notes``.
        """
        process = self._processes.get(invocation_id)
        boundary = self._boundaries.get(invocation_id)
        with self._teardown_lock(invocation_id):
            if process is not None and process.poll() is None and boundary is not None:
                boundary.terminate()
            self._close_boundary(invocation_id)
        stderr_reader = self._threads.get(f"{invocation_id}:stderr")
        stdin_writer = self._threads.get(f"{invocation_id}:stdin")
        for name in (invocation_id, f"{invocation_id}:stderr", f"{invocation_id}:stdin"):
            thread = self._threads.get(name)
            if thread is not None and thread.is_alive():
                thread.join(timeout=RELEASE_JOIN_SECONDS)
        if process is not None:
            close_output_handles(process)
            # stdin first: closing a writer never waits on a reader. Then the output pipes, but
            # only those no live reader is blocked on.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or getattr(stream, "closed", False):
                    continue
                if stream is process.stdin and stdin_writer is not None and stdin_writer.is_alive():
                    # Closing it would wait on the lock the writer's blocked write holds; the
                    # writer closes it itself once the write fails or completes.
                    note = (
                        "stdin writer still blocked at release; the pipe is left to it and is "
                        "closed when its write ends"
                    )
                    notes = self._release_notes.setdefault(invocation_id, [])
                    if note not in notes:
                        notes.append(note)
                    continue
                if (
                    stream is process.stderr
                    and stderr_reader is not None
                    and stderr_reader.is_alive()
                ):
                    note = (
                        "stderr reader still blocked at release; the pipe is left to it and is "
                        "closed when its last writer exits"
                    )
                    notes = self._release_notes.setdefault(invocation_id, [])
                    if note not in notes:
                        notes.append(note)
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def release_notes(self, invocation_id: str) -> list[str]:
        """What ``release`` left undone for this invocation (empty when it closed everything)."""
        return list(self._release_notes.get(invocation_id, []))

    def events(self, invocation_id: str) -> list[NormalizedEvent]:
        return list(self._events.get(invocation_id, []))

    def raw_lines(self, invocation_id: str) -> list[str]:
        return list(self._lines.get(invocation_id, []))

    def unparsed_line_count(self, invocation_id: str) -> int:
        return self._unparsed.get(invocation_id, 0)
