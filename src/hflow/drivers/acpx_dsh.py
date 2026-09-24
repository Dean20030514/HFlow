"""The single thin production Driver: acpx -> official DSH ACP.

Scope of this module: launch, stdin/stdout framing, neutral event projection, process
boundary, stop, and conservative reconcile. It does **not** decide whether a task is
accepted - admission, budget, attempt, evidence, review and ``ResultReceipt`` stay in the
controller.

Facts baked in from the M0 probe and the installed acpx 0.17.1 bundle (not assumptions):

* On Windows a raw agent command *string* is rejected; the agent must be given as a
  structured argv in acpx's config file, so the driver writes a per-invocation config
  instead of passing ``--agent``.
* ``dsh`` resolves to a ``.CMD`` shim, which ``CreateProcess`` cannot launch; the child is
  spawned as ``cmd.exe /c cmd.exe /c <dsh> --profile acp``. The wrapper never carries task
  text, nonce, prompt or credentials - only the fixed launcher path and profile flag.
* The task body goes through acpx's documented stdin path (``-f -``), then the child's
  stdin is closed so input is complete. The ACP stdin between acpx and DSH is acpx's own
  pipe and is never touched from here.
* The body is the controller's rendered input packet, verbatim (``hflow.packet``). This
  driver adds no task facts of its own: it does not read the repository to fill a gap, and
  when the request carries no packet it transports the bare ``goal`` inside a digest
  envelope. The digest of the text it was handed is reported back in the invocation result,
  so a driver that sends something other than the packet is caught. That digest is a local
  record of the input, not an acknowledgement from the agent or the model.
* ``acpx cancel`` reaches a *queue owner* for a persisted session. One-shot ``exec`` runs
  without a saved session, so no protocol-cancel entry point exists on this path; the
  driver records that as ``cooperative_cancel=unsupported`` rather than pretending.

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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
    NormalizedEvent,
    ReconcileOutcome,
    ReconcileResult,
    ReviewOutput,
)
from ..ids import utc_now
from ..artifacts import BoundedTextSink, StreamCapture
from ..packet import packet_digest
from ..review import (
    MAX_ANSWER_BYTES,
    REVIEW_INVALID,
    REVIEW_MISSING,
    AnswerTranscript,
    ReviewDecodeError,
    decode_review,
)
from .acp_events import project_line, summarize
from .winjob import ProcessBoundary, popen_in_boundary, process_gone

DRIVER_ID = "acpx-dsh-acp"
DRIVER_VERSION = "0.1.0"

ENV_ACPX_CLI = "HFLOW_ACPX_CLI"
ENV_ACPX_NODE = "HFLOW_ACPX_NODE"
#: Opt-in to file writes for a real invocation. Off by default; the controller sets it only
#: for runs whose workspace is a disposable worktree created from a fixed base commit.
ENV_ALLOW_WRITES = "HFLOW_ALLOW_WRITES"
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
#: Grace period between "please stop" and "the boundary is closed anyway".
FORCE_STOP_GRACE_SECONDS = 2.0
#: How long to wait for the boundary to report itself empty after termination.
BOUNDARY_EMPTY_TIMEOUT_SECONDS = 10.0
#: Written around a packet so the prompt text a driver actually sent is recoverable, and so a
#: bare ``goal`` (a direct driver call) is still traceable to the request it came from. The
#: digest covers the prompt text alone, so this envelope cannot change what the digest means.
PROMPT_ENVELOPE = "HFLOW-PROMPT-DIGEST"


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


class AcpxDshDriver:
    """One implementation, one transport. It refuses rather than guessing."""

    driver_id = DRIVER_ID
    driver_version = DRIVER_VERSION
    #: True only where a real protocol-cancel entry point exists for our launch mode.
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
    ) -> None:
        self.data_dir = Path(data_dir)
        self.acpx_cli = Path(acpx_cli) if acpx_cli else self._resolve_acpx_cli()
        self.dsh_executable = dsh_executable or shutil.which("dsh") or "dsh"
        self.profile = profile
        self.dsh_home = Path(dsh_home) if dsh_home else None
        self.python_executable = python_executable or shutil.which("python") or "python"
        self.node_executable = os.environ.get(ENV_ACPX_NODE) or shutil.which("node") or "node"
        self.extra_env = dict(extra_env or {})
        self.completion_timeout_seconds = completion_timeout_seconds
        #: Retention cap for one invocation's raw logs. A parameter rather than a constant so a
        #: test can drive the overflow path without generating 32 MiB of output.
        self.max_raw_log_bytes = max(0, int(max_raw_log_bytes))
        #: Test seam: the agent launch argv that goes into the acpx config. ``None`` means
        #: the real DSH launcher argv. Nothing else about the launch path is injectable.
        self.agent_argv_override = list(agent_argv_override) if agent_argv_override else None
        self._handles: dict[str, DriverHandle] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._boundaries: dict[str, ProcessBoundary] = {}
        self._streams: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stream_drained: dict[str, bool] = {}
        self._receipts: dict[str, CancellationReceipt] = {}
        self._events: dict[str, list[NormalizedEvent]] = {}
        #: Assistant messages of each invocation, kept so a reviewer's final answer can be
        #: reassembled from the chunks that were actually observed (see ``collect``).
        self._transcripts: dict[str, AnswerTranscript] = {}
        self._prompt_request_ids: dict[str, set[Any]] = {}
        self._terminal_prompt_ids: dict[str, list[Any]] = {}
        self._lines: dict[str, list[str]] = {}
        self._unparsed: dict[str, int] = {}
        self._overflow: dict[str, bool] = {}
        #: Lines that never ended before the pending buffer's cap: counted separately from
        #: unparseable lines so a client stuck writing one enormous line is diagnosable.
        self._oversized: dict[str, int] = {}
        #: Retained stderr per invocation, so ``collect`` can report what was kept rather than
        #: implying the whole stream is on disk.
        self._stderr_captures: dict[str, StreamCapture] = {}
        #: Set once the in-memory event list stopped growing: the container is bounded too, not
        #: only the retained file.
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
        #: Hard cap on retained events for one invocation, independent of their byte size.
        self.max_event_records = 20000
        self._results: dict[str, InvocationResult] = {}
        #: Digest of the prompt text each invocation was launched with, reported by ``collect``
        #: so the controller can tell "the model answered" from "the prompt arrived intact".
        self._prompt_digests: dict[str, str] = {}

    # -- configuration -------------------------------------------------------

    def _resolve_acpx_cli(self) -> Path:
        override = os.environ.get(ENV_ACPX_CLI)
        if override:
            path = Path(override)
            if not path.exists():
                raise DriverSetupError(f"{ENV_ACPX_CLI} points at a missing file: {path}")
            return path
        repo_root = Path(__file__).resolve().parents[3]
        for candidate in (self.data_dir / DEV_ACPX_RELATIVE, repo_root / PROBE_ACPX_RELATIVE):
            if candidate.exists():
                return candidate
        raise DriverSetupError(
            "acpx CLI not found. Set HFLOW_ACPX_CLI to the acpx entry point, or install the "
            "project-local copy the M0 probe uses "
            f"({repo_root / PROBE_ACPX_RELATIVE}). This driver never installs or upgrades it "
            "silently."
        )

    def _agent_argv(self) -> list[str]:
        """The launch command as a real argv, wrapped for the Windows batch shim.

        Only the launcher path and the fixed profile flag live here: no task text, no nonce,
        no user content, no credentials.
        """
        if self.agent_argv_override is not None:
            return list(self.agent_argv_override)
        argv = [self.dsh_executable, "--profile", self.profile]
        if os.name == "nt" and self.dsh_executable.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", *argv]
        return argv

    def _child_env(self, handle_workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.extra_env)
        if self.dsh_home is not None:
            env["DSH_HOME"] = str(self.dsh_home)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    # -- probe ---------------------------------------------------------------

    def probe(self, binding: AgentBinding) -> CapabilityReport:
        """Local capability record. Sends no task and calls no model."""
        notes = [
            "probe is static: no prompt, no session, no model request",
            f"acpx CLI: {self.acpx_cli}",
            f"agent argv: {self._agent_argv()}",
            "cooperative protocol cancel: unsupported on the one-shot exec path "
            "(acpx cancel targets a persisted session's queue owner)",
        ]
        if self.dsh_home is not None:
            notes.append(f"probe DSH_HOME: {self.dsh_home}")
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
        """
        return [
            *self._client_prefix(),
            str(self.acpx_cli),
            "--cwd",
            str(workspace),
            "--format",
            "json",
            "--timeout",
            str(deadline_seconds),
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
        """Interpreter prefix for the client entry point, chosen by its kind."""
        suffix = self.acpx_cli.suffix.lower()
        if suffix in {".js", ".mjs", ".cjs"}:
            return [self.node_executable]
        if suffix == ".py":
            return [self.python_executable, "-u"]
        return []

    def start_handle(self, request: InvocationRequest) -> DriverHandle:
        """Launch one invocation and return immediately with an observable handle."""
        if request.invocation_id in self._handles:
            raise DriverSetupError(
                f"invocation {request.invocation_id} was already started; refusing to start it twice"
            )
        invocation_dir = self.data_dir / "invocations" / request.invocation_id
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
        config_path = self._write_config(invocation_dir, writes_allowed=request.writes_allowed)

        boundary = ProcessBoundary().open()
        argv = self._client_argv(invocation_dir, workspace, request.deadline_seconds)
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
        child_home = (invocation_dir / "home").resolve()
        child_home.mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(child_home)
        env["HOME"] = str(child_home)
        env["APPDATA"] = str((child_home / "AppData").resolve())
        Path(env["APPDATA"]).mkdir(parents=True, exist_ok=True)
        # acpx reads its global config from the OS home; point it at the per-invocation one.
        acpx_home = child_home / ".acpx"
        acpx_home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config_path, acpx_home / "config.json")

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
        self._handles[request.invocation_id] = handle
        self._processes[request.invocation_id] = child
        self._boundaries[request.invocation_id] = boundary
        # One declared retention budget per invocation, split between the protocol stream and
        # stderr. The reader enforces it by writing only up to its share and repeatedly trimming
        # the client's own file back to that share: a looping client keeps writing, and the file
        # keeps being returned to the budget instead of growing with it.
        self._events[request.invocation_id] = []
        self._transcripts[request.invocation_id] = AnswerTranscript(role=request.role)
        self._prompt_request_ids[request.invocation_id] = set()
        self._terminal_prompt_ids[request.invocation_id] = []
        self._lines[request.invocation_id] = deque(maxlen=MAX_BUFFERED_LINES)
        self._unparsed[request.invocation_id] = 0
        self._overflow[request.invocation_id] = False
        self._oversized[request.invocation_id] = 0
        self._prompt_digests[request.invocation_id] = prompt_digest
        self._events_capped[request.invocation_id] = False
        self._peak_raw_bytes[request.invocation_id] = 0

        # Task text via stdin, then close it: HFlow -> acpx input is complete. The ACP pipe
        # between acpx and DSH is acpx's own and is not touched here.
        assert child.stdin is not None
        try:
            child.stdin.write(prompt_bytes)
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # the process may already have failed; collect() reports the real reason
        finally:
            child.stdin.close()

        thread = threading.Thread(
            target=self._consume_stream,
            args=(request.invocation_id, stdout_path),
            daemon=True,
        )
        self._threads[request.invocation_id] = thread
        thread.start()
        stderr_thread = threading.Thread(
            target=self._consume_stderr,
            args=(request.invocation_id, stderr_path),
            daemon=True,
        )
        self._threads[request.invocation_id + ":stderr"] = stderr_thread
        stderr_thread.start()
        return handle

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
        """Tail the client's protocol stream into a bounded retained log, projecting each line.

        What is bounded here is what HFlow **keeps**: the retained log stops at the protocol share
        of the retention budget, and the in-memory event list stops growing with it. What is *not*
        claimed is a bound on the client's own file (``stdout.ndjson``): the client owns that file
        and keeps writing to it, and HFlow does not truncate a file another process is writing -
        that removes bytes nobody has read and can leave a hole. Its measured size is reported
        instead (``peak_raw_bytes``), so the difference between "bounded" and "measured" is
        visible rather than implied.

        A line that never ends is bounded too. Without that, a client writing a gigabyte with no
        newline would grow ``pending`` in memory exactly as fast as the file - the bound would be
        nominal. An over-long line is reported as an unusable line (which makes the turn's result
        unknown) and the buffer is reset.
        """
        handle = self._handles[invocation_id]
        process = self._processes[invocation_id]
        cap = self.protocol_share_bytes
        with BoundedTextSink(Path(handle.event_log), limit=cap) as sink:
            pending = b""
            offset = 0
            while True:
                chunk = b""
                try:
                    with stdout_path.open("rb") as source:
                        source.seek(offset)
                        chunk = source.read(CAPTURE_READ_CHUNK)
                except OSError:
                    chunk = b""
                if chunk:
                    offset += len(chunk)
                    if offset > self._peak_raw_bytes[invocation_id]:
                        self._peak_raw_bytes[invocation_id] = offset
                    previous_total = sink.total_bytes
                    pending += chunk
                    *complete, pending = pending.split(b"\n")
                    for raw in complete:
                        self._project_line(invocation_id, sink, raw)
                    if sink.total_bytes > sink.retained_bytes and (
                        sink.total_bytes > previous_total or not self._overflow[invocation_id]
                    ):
                        # The budget ran out on this read: recorded now rather than on the next
                        # line, so a stream whose *last* line crosses the cap is still reported.
                        self._overflow[invocation_id] = True
                    if len(pending) > MAX_PENDING_LINE_BYTES:
                        # Unusable as a message; counted so the outcome cannot look clean. The
                        # pending buffer is reset because an endless line is not a message.
                        self._unparsed[invocation_id] += 1
                        self._oversized[invocation_id] += 1
                        pending = b""
                    continue
                if process.poll() is not None:
                    try:
                        with stdout_path.open("rb") as source:
                            source.seek(offset)
                            tail = source.read(CAPTURE_READ_CHUNK)
                    except OSError:
                        tail = b""
                    if not tail:
                        break
                    continue
                time.sleep(STREAM_POLL_SECONDS)
            if pending:
                self._project_line(invocation_id, sink, pending)
            self._overflow[invocation_id] = self._overflow[invocation_id] or sink.truncated
            final_capture = sink.capture()
        self._stdout_captures[invocation_id] = final_capture
        self._stream_drained[invocation_id] = True
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
        """Retain one output line within the budget and project it to an event.

        Two things stay bounded together. The retained file stops at the sink's limit, and the
        in-memory event list stops growing once that budget is spent: parsing may continue (it is
        how a stop reason is recognised), but nothing further is accumulated, so "the events are
        capped" is a property of the container and not only of the file.
        """
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace")
        # Always written: the sink retains only up to its limit, but it *counts and digests* every
        # byte it is given. Skipping the call once the limit was reached is what made the reported
        # total describe HFlow's copy instead of the stream HFlow actually read.
        sink.write(text.encode("utf-8") + b"\n")
        self._lines[invocation_id].append(text)
        observed = project_line(text, len(self._events[invocation_id]), utc_now())
        if not observed.parsed:
            self._unparsed[invocation_id] += 1
            return
        if observed.message is not None:
            self._note_message(
                invocation_id, observed.message, line_index=len(self._lines[invocation_id]) - 1
            )
        if observed.event is not None:
            if self._events_capped[invocation_id]:
                return
            if sink.truncated or len(self._events[invocation_id]) >= self.max_event_records:
                self._events_capped[invocation_id] = True
                return
            self._events[invocation_id].append(observed.event)

    def _consume_stderr(self, invocation_id: str, stderr_path: Path) -> None:
        """Drain the client's stderr from its pipe into a bounded sink.

        The child writes into a pipe, so this is an entry-point limit on what HFlow retains and on
        what the client can push: whatever exceeds the stderr share is read, counted and digested
        but not written, and the client cannot fill the disk with it.
        """
        process = self._processes[invocation_id]
        cap = self.stderr_share_bytes
        with BoundedTextSink(stderr_path, limit=cap) as sink:
            stream = process.stderr
            status = "no_stream"
            if stream is not None:
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

        Three neutral facts, no policy: the dispatch marker, the session identity, and the
        assistant text of the turn (which is where a reviewer's verdict actually travels).
        """
        handle = self._handles[invocation_id]
        if message.get("method") == "session/prompt":
            handle.dispatched = True
            handle.dispatched_at = handle.dispatched_at or utc_now()
            request_id = message.get("id")
            if request_id is not None:
                self._prompt_request_ids.setdefault(invocation_id, set()).add(request_id)
        if message.get("method") == "session/update":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
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
            self._terminal_prompt_ids.setdefault(invocation_id, []).append(message.get("id"))

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
        """Fold the invocation into one result. Never invents success."""
        invocation_id = handle.invocation_id
        if invocation_id in self._results:
            return self._results[invocation_id]
        process = self._processes[invocation_id]
        try:
            process.wait(timeout=self.completion_timeout_seconds)
        except subprocess.TimeoutExpired:
            result = InvocationResult(
                invocation_id=invocation_id,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                prompt_digest=self._prompt_digests.get(invocation_id, ""),
                agent_turns=None,
                limitations=["completion deadline exceeded while waiting for the client"],
                error_code="completion_timeout",
                error_message="the client did not exit before the completion deadline",
                raw_ref=str(handle.event_log),
            )
            self._results[invocation_id] = result
            return result

        # The process can exit a moment before the reader thread has consumed its final
        # output. Judging the run at that instant would drop the stop reason and misreport a
        # completed turn as unknown, so wait for the drain - bounded, never unbounded.
        drain_deadline = time.monotonic() + STREAM_DRAIN_TIMEOUT_SECONDS
        while not self._stream_drained.get(invocation_id) and time.monotonic() < drain_deadline:
            time.sleep(STREAM_POLL_SECONDS)

        events = self._events.get(invocation_id, [])
        summary = summarize(events)
        unparsed = self._unparsed.get(invocation_id, 0)
        oversized = self._oversized.get(invocation_id, 0)
        overflowed = self._overflow.get(invocation_id, False)
        stop_reason = summary["stop_reason"]
        receipt = self._receipts.get(invocation_id)

        if receipt is not None and receipt.status == "confirmed_stopped":
            outcome = InvocationOutcome.CANCELLED
            error_code, error_message = "cancelled", receipt.detail
        elif overflowed:
            # Reported before "unparseable lines": cutting the stream is what makes the tail
            # unreadable, so the cause is named rather than one of its symptoms.
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "output_limit_exceeded"
            error_message = (
                f"client output exceeded the {self.max_raw_log_bytes} byte retention budget; the "
                "protocol stream was cut, so the result cannot be trusted"
            )
        elif unparsed:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "unparseable_output"
            error_message = f"{unparsed} unparseable line(s) in the client output stream"
            if oversized:
                error_message += f" ({oversized} of them exceeded the {MAX_PENDING_LINE_BYTES} byte line cap)"
        elif stop_reason in {None, ""}:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "no_stop_reason"
            error_message = (
                f"client exited {process.returncode} without a prompt stop reason"
                if process.returncode == 0
                else f"client exited {process.returncode} before the turn settled"
            )
        elif stop_reason == "end_turn":
            outcome = InvocationOutcome.COMPLETED
            error_code, error_message = None, None
        elif stop_reason == "cancelled":
            outcome = InvocationOutcome.CANCELLED
            error_code, error_message = "cancelled", "the harness reported stopReason=cancelled"
        else:
            outcome = InvocationOutcome.FAILED
            error_code, error_message = f"stop_reason_{stop_reason}", f"turn settled as {stop_reason}"

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
                f"client stdout exceeded the {self.max_raw_log_bytes} byte retention cap; the retained "
                "log is a prefix, not the whole stream"
            )
        if not handle.dispatched:
            limitations.append("no session/prompt was observed; the harness never received the task")
        if self._terminal_response_matches_prompt(invocation_id) is False:
            limitations.append(
                "review_unbound: the terminal response does not answer the observed session/prompt "
                "request; the turn's completion is not bound to this invocation's prompt"
            )
        review, note = self._review_output(handle, outcome)
        if note:
            limitations.append(note)
        result = InvocationResult(
            invocation_id=invocation_id,
            outcome=outcome,
            candidate=None,
            review=review,
            prompt_digest=self._prompt_digests.get(invocation_id, ""),
            agent_turns=1 if handle.dispatched else 0,
            provider_billed_tokens=None,
            reported_cost=None,
            limitations=limitations,
            raw_ref=str(handle.event_log),
            error_code=error_code,
            error_message=error_message,
        )
        handle.finished = True
        self._results[invocation_id] = result
        return result

    def _terminal_response_matches_prompt(self, invocation_id: str) -> bool:
        """Does the terminal prompt response answer a ``session/prompt`` we observed?

        A JSON-RPC response is not task completion by itself; it is completion of one
        request. The recorded runtime answers the prompt with the same id, and this keeps
        that association rather than trusting "the stream ended".

        ``None`` means no prompt was observed at all, so there is nothing to bind a
        completion to and no mismatch to report.
        """
        prompt_ids = self._prompt_request_ids.get(invocation_id)
        if not prompt_ids:
            return None
        terminal_ids = self._terminal_prompt_ids.get(invocation_id) or []
        return any(request_id in prompt_ids for request_id in terminal_ids)

    def _review_output(
        self, handle: DriverHandle, outcome: InvocationOutcome
    ) -> tuple[ReviewOutput | None, str]:
        """Decode a verdict from a reviewer's *final answer*, or explain why there is none.

        This is the role-specific adaptation boundary, and the only place a review can come
        from. It grants no authority: a decoded verdict is validated against the canonical
        model and handed to the controller, which decides what it means. Anything else -
        wrong role, unfinished turn, unreadable answer - yields ``None`` plus a machine
        readable reason, so the controller never has to read a model's prose to find out
        whether the wire was intact.
        """
        transcript = self._transcripts.get(handle.invocation_id)
        if transcript is None:
            return None, ""
        if handle.role != "reviewer":
            # A verdict-shaped object in an implementer's output is not a review: only the
            # review invocation may produce review evidence. Nothing to explain here - the
            # absent review is the expected result for this role.
            return None, ""
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
        if self._terminal_response_matches_prompt(handle.invocation_id) is False:
            # True means matched; None means no prompt was observed, which the limitation
            # above already states. Only a real mismatch is called out here.
            return None, (
                f"review_{REVIEW_MISSING}: the terminal response was not matched to an observed "
                "session/prompt request for this invocation"
            )
        try:
            review = decode_review(answer.text)
        except ReviewDecodeError as exc:
            return None, f"review_{exc.kind}: {exc.detail} (answer {len(answer.text)} chars)"
        return review, f"review_decoded: {review.verdict} from the final agent message"

    # -- stop ----------------------------------------------------------------

    def cancel_handle(self, handle: DriverHandle) -> CancellationReceipt:
        """Request a stop and report facts. Idempotent; never sends another prompt."""
        invocation_id = handle.invocation_id
        previous = self._receipts.get(invocation_id)
        if previous is not None:
            return previous

        process = self._processes[invocation_id]
        boundary = self._boundaries[invocation_id]
        if process.poll() is not None:
            receipt = CancellationReceipt(
                invocation_id=invocation_id,
                status="confirmed_stopped",
                mechanism="none",
                local_process_stopped=True,
                detail="the invocation had already exited when the stop was requested",
            )
            self._receipts[invocation_id] = receipt
            return receipt

        # No protocol-cancel entry point exists on the one-shot exec path, so the only
        # mechanism available is the managed process boundary. Stated, not implied.
        detail_prefix = (
            "cooperative cancel is unavailable on this launch path (acpx cancel targets a "
            "persisted session's queue owner); the managed process boundary was terminated"
        )
        if handle.dispatched:
            # Let the client notice EOF on its own before the boundary is closed.
            self._close_client_stdin(process)
            try:
                process.wait(timeout=FORCE_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            boundary.terminate()
            emptied = boundary.wait_empty(BOUNDARY_EMPTY_TIMEOUT_SECONDS)
            stopped = emptied and process_gone(process.pid, 2.0)
            status = "confirmed_stopped" if stopped else ("still_running" if not emptied else "unknown")
        else:
            emptied = boundary.wait_empty(1.0)
            stopped = process_gone(process.pid, 1.0)
            status = "confirmed_stopped" if stopped else "unknown"

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
        self._receipts[invocation_id] = receipt
        if status == "confirmed_stopped":
            # Keep exit and event evidence, then release the boundary so nothing lingers.
            self._close_boundary(invocation_id)
        return receipt

    def _close_client_stdin(self, process: subprocess.Popen) -> None:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass

    def _close_boundary(self, invocation_id: str) -> None:
        boundary = self._boundaries.get(invocation_id)
        if boundary is not None:
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
        alive = process.poll() is None and not process_gone(process.pid)
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
        handle = self._handles.get(invocation_id)
        if handle is None:
            return CancellationReceipt(
                invocation_id=invocation_id,
                status="unknown",
                mechanism="none",
                detail="no handle for this invocation; nothing was stopped",
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
        """Drop an invocation: close its boundary, pipes and log handles.

        If the process is somehow still alive this terminates it through the boundary. That
        is a cleanup of a *managed* process, not a cancellation claim, so it never writes a
        receipt - a stop that was not confirmed stays unconfirmed.
        """
        process = self._processes.get(invocation_id)
        boundary = self._boundaries.get(invocation_id)
        if process is not None and process.poll() is None and boundary is not None:
            boundary.terminate()
        self._close_boundary(invocation_id)
        for name in (invocation_id, f"{invocation_id}:stderr"):
            thread = self._threads.get(name)
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        if process is not None:
            for stream in (process.stdout, process.stderr, process.stdin):
                if stream is None or getattr(stream, "closed", False):
                    continue
                try:
                    stream.close()
                except OSError:
                    pass

    def events(self, invocation_id: str) -> list[NormalizedEvent]:
        return list(self._events.get(invocation_id, []))

    def raw_lines(self, invocation_id: str) -> list[str]:
        return list(self._lines.get(invocation_id, []))

    def unparsed_line_count(self, invocation_id: str) -> int:
        return self._unparsed.get(invocation_id, 0)
