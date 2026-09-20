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
)
from ..ids import utc_now
from .acp_events import project_line, summarize
from .winjob import ProcessBoundary, popen_in_boundary, process_gone

DRIVER_ID = "acpx-dsh-acp"
DRIVER_VERSION = "0.1.0"

ENV_ACPX_CLI = "HFLOW_ACPX_CLI"
ENV_ACPX_NODE = "HFLOW_ACPX_NODE"
#: Resolution order for the acpx entry point, most explicit first:
#:   1. ``$HFLOW_ACPX_CLI`` (operator intent; wins over everything);
#:   2. ``~/.hflow/`` sibling layout used by the repo-local development install;
#:   3. the M0 probe install inside this checkout.
#: No step installs, upgrades, or downloads anything.
DEV_ACPX_RELATIVE = Path("m0/acpx/node_modules/acpx/dist/cli.js")
PROBE_ACPX_RELATIVE = Path(".probe/acpx/node_modules/acpx/dist/cli.js")
#: Cap on the raw event log so a chatty or looping agent cannot fill the disk.
MAX_RAW_LOG_BYTES = 4 * 1024 * 1024
#: Cap on retained lines: the log file is the full record, memory only needs recent context.
MAX_BUFFERED_LINES = 5000
#: How often the stream follower re-checks a file that has not grown yet.
STREAM_POLL_SECONDS = 0.05
#: How long ``collect`` waits for the reader to finish after the client exits.
STREAM_DRAIN_TIMEOUT_SECONDS = 5.0
#: Grace period between "please stop" and "the boundary is closed anyway".
FORCE_STOP_GRACE_SECONDS = 2.0
#: How long to wait for the boundary to report itself empty after termination.
BOUNDARY_EMPTY_TIMEOUT_SECONDS = 10.0


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
        self._lines: dict[str, list[str]] = {}
        self._unparsed: dict[str, int] = {}
        self._overflow: dict[str, bool] = {}
        self._results: dict[str, InvocationResult] = {}

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
        # Task body travels as a file, not as a command-line string.
        task_file.write_text(request.goal, encoding="utf-8")
        config_path = self._write_config(invocation_dir)

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
            stderr_handle = stderr_path.open("wb")
            child = popen_in_boundary(
                argv,
                cwd=str(invocation_dir),
                env=env,
                boundary=boundary,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
            )
        except BaseException:
            boundary.close()
            raise
        handle.pid = child.pid
        self._handles[request.invocation_id] = handle
        self._processes[request.invocation_id] = child
        self._boundaries[request.invocation_id] = boundary
        # Bounded: the raw log file is the full record, memory only needs recent context.
        self._events[request.invocation_id] = []
        self._lines[request.invocation_id] = deque(maxlen=MAX_BUFFERED_LINES)
        self._unparsed[request.invocation_id] = 0
        self._overflow[request.invocation_id] = False

        # Task text via stdin, then close it: HFlow -> acpx input is complete. The ACP pipe
        # between acpx and DSH is acpx's own and is not touched here.
        assert child.stdin is not None
        try:
            child.stdin.write(task_file.read_text(encoding="utf-8").encode("utf-8"))
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # the process may already have failed; collect() reports the real reason
        finally:
            child.stdin.close()

        thread = threading.Thread(
            target=self._consume_stream,
            args=(request.invocation_id, stdout_path, stderr_path),
            daemon=True,
        )
        self._threads[request.invocation_id] = thread
        thread.start()
        return handle

    def _write_config(self, invocation_dir: Path) -> Path:
        """Per-invocation acpx config: structured argv, explicit agent name, no defaults."""
        config = {
            "defaultAgent": DRIVER_ID,
            "authPolicy": "skip",
            "permissionPolicy": {"defaultAction": "deny"},
            "nonInteractivePermissions": "deny",
            "ttl": 30,
            "format": "json",
            "agents": {DRIVER_ID: {"argv": self._agent_argv()}},
        }
        path = invocation_dir / "acpx-config.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def _consume_stream(
        self, invocation_id: str, stdout_path: Path, stderr_path: Path
    ) -> None:
        """Follow the client's stdout file and project each complete line to an event.

        Reads by explicit byte offset instead of a persistent buffered reader: a reader that
        latches EOF at the end of the currently written data stops delivering later output,
        which silently loses everything after the first line. Runs on its own thread so a
        live turn never blocks the caller and the stop path stays reachable.
        """
        log_path = Path(self._handles[invocation_id].event_log)
        process = self._processes[invocation_id]
        offset = 0
        pending = b""
        written = 0
        with log_path.open("a", encoding="utf-8") as log:
            while True:
                chunk = b""
                try:
                    with stdout_path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                except OSError:
                    chunk = b""
                if chunk:
                    offset += len(chunk)
                    pending += chunk
                    *complete, pending = pending.split(b"\n")
                    for raw in complete:
                        text = raw.decode("utf-8", errors="replace")
                        if written < MAX_RAW_LOG_BYTES:
                            log.write(text + "\n")
                            written += len(raw) + 1
                        else:
                            self._overflow[invocation_id] = True
                        self._lines[invocation_id].append(text)
                        observed = project_line(text, len(self._events[invocation_id]), utc_now())
                        if not observed.parsed:
                            self._unparsed[invocation_id] += 1
                            continue
                        if observed.message is not None:
                            self._note_message(invocation_id, observed.message)
                        if observed.event is not None:
                            self._events[invocation_id].append(observed.event)
                    continue
                if process.poll() is not None:
                    # The child is gone: one last read catches anything written on exit.
                    try:
                        with stdout_path.open("rb") as handle:
                            handle.seek(offset)
                            tail = handle.read()
                    except OSError:
                        tail = b""
                    if not tail:
                        break
                    continue
                time.sleep(STREAM_POLL_SECONDS)
        self._stream_drained[invocation_id] = True
        if os.environ.get("HFLOW_DRIVER_DEBUG"):
            print(
                f"driver-debug: {invocation_id} lines={len(self._lines[invocation_id])} "
                f"events={len(self._events[invocation_id])} "
                f"unparsed={self._unparsed[invocation_id]} overflow={self._overflow[invocation_id]}",
                file=sys.stderr,
                flush=True,
            )

    def _note_message(self, invocation_id: str, message: dict[str, Any]) -> None:
        handle = self._handles[invocation_id]
        if message.get("method") == "session/prompt":
            handle.dispatched = True
            handle.dispatched_at = handle.dispatched_at or utc_now()
        result = message.get("result")
        if isinstance(result, dict) and isinstance(result.get("sessionId"), str):
            handle.session_id = result["sessionId"]

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
        overflowed = self._overflow.get(invocation_id, False)
        stop_reason = summary["stop_reason"]
        receipt = self._receipts.get(invocation_id)

        if receipt is not None and receipt.status == "confirmed_stopped":
            outcome = InvocationOutcome.CANCELLED
            error_code, error_message = "cancelled", receipt.detail
        elif unparsed:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "unparseable_output"
            error_message = f"{unparsed} unparseable line(s) in the client output stream"
        elif overflowed:
            outcome = InvocationOutcome.OUTCOME_UNKNOWN
            error_code = "output_limit_exceeded"
            error_message = "client output exceeded the raw log cap; truncation makes the result untrustworthy"
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
        if not handle.dispatched:
            limitations.append("no session/prompt was observed; the harness never received the task")
        result = InvocationResult(
            invocation_id=invocation_id,
            outcome=outcome,
            candidate=None,
            review=None,
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
        """Drop an invocation: close its boundary and file handles.

        If the process is somehow still alive this terminates it through the boundary. That
        is a cleanup of a *managed* process, not a cancellation claim, so it never writes a
        receipt - a stop that was not confirmed stays unconfirmed.
        """
        process = self._processes.get(invocation_id)
        boundary = self._boundaries.get(invocation_id)
        if process is not None and process.poll() is None and boundary is not None:
            boundary.terminate()
        self._close_boundary(invocation_id)
        if process is not None:
            for stream in (
                getattr(process, "_hflow_stdout", None),
                getattr(process, "_hflow_stderr", None),
            ):
                if stream is not None and not getattr(stream, "closed", False):
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
