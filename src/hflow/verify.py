"""Program verification over a frozen candidate (plan 12).

Scope of M1: run the *approved* checks referenced by the acceptance criteria and
record evidence against a candidate fingerprint. Two properties matter more than
coverage here:

* checks are looked up in the project contract, so a TaskSpec can never smuggle in
  its own "verification" command;
* evidence is keyed by ``(candidate fingerprint, check id, checks digest)``, so a
  candidate that changes after verification invalidates the evidence instead of
  inheriting an old pass (acceptance A09/A10/A11).

Real sandboxing is *not* implemented. Command checks run as ordinary subprocesses
with a timeout; that limits blast radius but is not a security boundary.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from .artifacts import (
    DEFAULT_EXCERPT_BYTES,
    DEFAULT_STREAM_LIMIT_BYTES,
    BoundedTextSink,
    StreamCapture,
    child_environment,
    drain,
    environment_summary,
    write_artifact_manifest,
)
from .contracts import (
    CheckDef,
    EvidenceStatus,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    TaskSpec,
    VerificationResult,
    digest_of,
)
from .drivers.winjob import (
    JobBoundaryError,
    ProcessBoundary,
    popen_in_boundary,
    process_gone,
)
from .ids import new_evidence_id
from .store import Store

#: How long a finished check's boundary is given to settle before its remaining processes
#: are treated as a lifecycle defect rather than a clean pass. Bounded, and taken from the
#: check's own deadline, so a check never overruns because of this wait.
DESCENDANT_SETTLE_SECONDS = 2.0
#: How long a boundary tear-down is given to report itself empty before it is called
#: unconfirmed. A tear-down that cannot confirm must not be reported as a success.
BOUNDARY_EMPTY_SECONDS = 5.0
#: How long the direct child is given to die after the boundary killed it.
CHILD_REAP_SECONDS = 5.0
#: The structured reason a worktree run's command check was not started because bytecode the
#: worker may have left in the candidate worktree could not be removed safely (see
#: :func:`remove_worker_bytecode`). It is not a clean completion, so it can never buy a repair.
REASON_BYTECODE_NOT_CLEARED = "bytecode_not_cleared"
#: The run note naming how much worker-left bytecode a verification removed.
NOTE_BYTECODE_REMOVED = "bytecode_removed"


class BytecodeRemoval:
    """What :func:`remove_worker_bytecode` removed, and what it refused to touch."""

    def __init__(
        self, files: int = 0, directories: int = 0, refused: Iterable[str] = ()
    ) -> None:
        self.files = files
        self.directories = directories
        #: Bytecode-shaped entries that were left in place and that a check could still load: a
        #: ``__pycache__`` that is a link or junction, a ``.pyc`` link, or a file that could not be
        #: removed. Any entry here means the check must not run.
        self.refused = tuple(refused)

    def note(self) -> str:
        return (
            f"worker-left bytecode removed before this check: {self.files} ignored .pyc file(s), "
            f"{self.directories} emptied __pycache__ director(ies)"
        )


def _is_link(entry: os.DirEntry[str]) -> bool:
    """Is this entry a symbolic link or a junction? Neither is ever followed or removed."""
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def remove_worker_bytecode(root: Path, *, keep: Iterable[str] = ()) -> BytecodeRemoval:
    """Remove the ``.pyc`` files directly inside every real ``__pycache__`` under ``root``.

    Run before every command check of a worktree run. Python loads a ``__pycache__/*.pyc`` in
    place of its source (an ``UNCHECKED_HASH`` one without even looking at the source), and that
    bytecode is never committed and never fingerprinted - so a cache the worker left could make a
    check verify code no candidate commit holds. Removing it changes neither the candidate commit
    nor its fingerprint, and it holds whatever flags the check passes to Python (``-I``, ``-E``).

    The walk follows no link and no junction and never enters ``.git``. Only regular files whose
    name ends in ``.pyc`` and that sit directly in a ``__pycache__`` directory are removed, and the
    directory is removed afterwards only if that left it empty. ``keep`` names relative paths
    (POSIX spelling) that are tracked in the candidate commit; those are committed content and
    stay. Anything else is never touched. A ``__pycache__`` that is a link or junction, or a
    ``.pyc`` link inside a real one, is left alone and reported in ``refused``: Python would follow
    it, so the caller must not run a check while it is there.
    """
    fold = os.name == "nt"

    def key(relative: str) -> str:
        return relative.casefold() if fold else relative

    kept = {key(path) for path in keep}
    base = Path(os.path.realpath(root))
    files = 0
    directories = 0
    refused: list[str] = []

    def relative(path: str) -> str:
        return Path(path).relative_to(base).as_posix()

    pending: list[str] = [str(base)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            # A directory nothing can list cannot hand Python a cache either.
            continue
        is_cache = Path(directory).name.casefold() == "__pycache__" and directory != str(base)
        removed_here = False
        for entry in entries:
            name = entry.name.casefold()
            if name == ".git":
                continue
            if _is_link(entry):
                if name == "__pycache__" or (is_cache and name.endswith(".pyc")):
                    refused.append(f"{relative(entry.path)} (a link or junction; not followed)")
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                if is_cache and name.endswith(".pyc"):
                    refused.append(f"{relative(entry.path)} (unreadable: {exc})")
                continue
            if is_dir:
                pending.append(entry.path)
                continue
            if not (is_cache and is_file and name.endswith(".pyc")):
                continue
            if key(relative(entry.path)) in kept:
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                refused.append(f"{relative(entry.path)} (not removed: {exc})")
                continue
            files += 1
            removed_here = True
        if is_cache and removed_here:
            with contextlib.suppress(OSError):
                os.rmdir(directory)
                directories += 1
    return BytecodeRemoval(files=files, directories=directories, refused=refused)


# --------------------------------------------------------------------------
# Exit reasons: the structured execution fact a business failure is classified from
# --------------------------------------------------------------------------
#
# Batch E2 may repair a candidate only after a check that "ran to completion, with a clean process
# boundary and a trustworthy capture" failed its business assertion (plan §5.2). Which of those
# happened is recorded as one structured string, never guessed from the ``verification_failed``
# text or a log keyword - so the runner, the stored row and the repair gate must measure the *same*
# strings. They are named here for exactly that reason: a rename in the runner cannot leave the
# gate comparing against a value the runner never produces.

REASON_COMPLETED = "completed"
REASON_NONZERO_EXIT = "nonzero_exit"
REASON_TIMED_OUT = "timed_out"
REASON_SETTLEMENT_FORCED = "settlement_forced"
REASON_SETTLEMENT_UNKNOWN = "settlement_unknown"
REASON_OUTPUT_CAPTURE_ERROR = "output_capture_error"
#: Nothing was executed, so there is no process observation at all. The offline runners report
#: this by default, which is what keeps a fake result from ever being classified as a real
#: business failure.
REASON_NOT_LAUNCHED = "not_launched"

#: The only reasons a business check failure may be classified from: the check ran to completion
#: and reported an exit code. Everything else - a launch failure, a timeout, a forced or
#: unconfirmed settlement, an incomplete capture, an empty reason on a pre-E2 row - is not an
#: answer about the candidate, whatever exit code happens to sit beside it. The set is deliberately
#: small: an undeclared member would let an unobserved ending buy an implementer attempt.
CLEAN_EXIT_REASONS = frozenset({REASON_COMPLETED, REASON_NONZERO_EXIT})


class CheckOutcome:
    """Result of one check execution, before it becomes stored evidence.

    ``status`` is the verdict; ``exit_reason`` says *why* it ended the way it did, so a timeout, a
    forced settlement, a boundary that could not be observed and a plain non-zero exit are not all
    folded into one word. ``artifacts``/``artifact_path``/``environment`` are the references that
    make the run's own record readable after the fact.
    """

    __slots__ = (
        "artifact_path",
        "artifacts",
        "command",
        "detail",
        "environment",
        "exit_code",
        "exit_reason",
        "stderr_digest",
        "stdout_digest",
        "status",
        "timed_out",
    )

    def __init__(
        self,
        status: EvidenceStatus,
        *,
        exit_code: int | None = None,
        stdout_digest: str = "",
        stderr_digest: str = "",
        detail: str = "",
        command: list[str] | None = None,
        exit_reason: str = "",
        timed_out: bool = False,
        artifacts: dict[str, dict[str, object]] | None = None,
        artifact_path: str = "",
        environment: str = "",
    ) -> None:
        self.status = status
        self.exit_code = exit_code
        self.stdout_digest = stdout_digest
        self.stderr_digest = stderr_digest
        self.detail = detail
        self.command = command or []
        self.exit_reason = exit_reason
        self.timed_out = timed_out
        self.artifacts = artifacts or {}
        self.artifact_path = artifact_path
        self.environment = environment


class CheckRunner(Protocol):
    """A way to execute one approved check kind. Deliberately tiny."""

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome: ...


class FakeCheckRunner:
    """Offline runner for kind=fake. Its verdicts come from the caller, not the model.

    What it does *not* claim: no process is started, so there is no execution observation to
    report. ``exit_reason`` is therefore ``not_launched`` by default, and a fake failure - however
    it is declared - is ineligible to trigger an automatic repair. A test that wants to exercise
    the repair gate must *declare* the execution fact it is modelling, through ``exit_reasons``
    (``{"unit": REASON_NONZERO_EXIT}`` for a check that ran and exited non-zero). That is a
    declared offline scenario, never a claim that a real harness produced it (AGENTS rule 7).
    """

    def __init__(
        self,
        verdicts: dict[str, EvidenceStatus] | None = None,
        *,
        exit_reasons: dict[str, str] | None = None,
    ) -> None:
        self.verdicts = verdicts or {}
        self.exit_reasons = dict(exit_reasons or {})
        self.calls: list[str] = []
        self.cache_hits = 0
        #: Optional side effect applied the first time each check runs. Tests use it to
        #: model "the candidate changed while we were verifying" without a real harness.
        self.after_run: dict[str, object] | None = None

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome:
        self.calls.append(check.id)
        status = self.verdicts.get(check.id, EvidenceStatus.PASSED)
        detail = f"fake check {check.id}: {status.value}"
        if self.after_run:
            for relative, text in self.after_run.items():  # type: ignore[union-attr]
                target = Path(cwd) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(text), encoding="utf-8")
            self.after_run = None
        return CheckOutcome(
            status,
            exit_code=0 if status is EvidenceStatus.PASSED else 1,
            stdout_digest=digest_of({"check": check.id, "status": status.value}),
            detail=detail,
            exit_reason=self.exit_reasons.get(check.id, REASON_NOT_LAUNCHED),
        )


class CommandCheckRunner:
    """Runs an approved argv as a subprocess inside an owned process boundary.

    Output is captured through temporary files rather than pipes: it is portable,
    avoids deadlocks on large output, and keeps the controller free of a reader
    thread. Only digests and a short excerpt reach the database.

    The process itself is owned rather than merely started. On Windows the check is
    launched *inside* a Job Object (``drivers/winjob.py``), so a check that leaves
    descendants behind can be settled as one unit; the child is created suspended and
    assigned before it runs, so there is no window in which it is unowned. If that
    boundary cannot be established, nothing is executed at all: falling back to an
    unmanaged launch would mean the runner could no longer stop what it started.

    Three outcomes are deliberately *not* passes: a check whose deadline expired, a check
    whose direct child exited 0 while its descendants had to be terminated, and a check that
    could not be run inside a boundary at all. Each is an ``ERROR`` carrying the lifecycle
    observation in its detail text, so an unsettled process tree stays visible in the stored
    evidence instead of being rounded up to success. Where the platform's boundary covers only
    the direct child, the detail says so rather than implying whole-tree termination.

    What the linger check cannot see: a process that enters the job *after* the boundary was
    last polled. The boundary is sampled when the direct child has finished, and again after
    the settle window, so a descendant started later than that is reported by nothing. Closing
    the boundary at the end of every call terminates whatever is still in the job at that
    moment, but that termination is not itself observed and does not change an outcome that was
    already computed, so this remains a gap rather than a guarantee. A process that was never
    in the job at all - work delegated outside the owned boundary - is outside this slice
    entirely and is not reclaimed by ``close()``.

    An observation that could not be made is not a clean boundary either. ``None`` from the
    boundary means "unknown" - a failed query behind a Windows Job Object looks exactly like a
    boundary that does not exist - so the two are told apart by ``ProcessBoundary.kind`` and
    only a boundary that is *known* not to own a tree ("direct_child_only") may pass without a
    settlement observation.

    Output is captured through pipes drained by two reader threads into bounded sinks, and the
    environment a check sees is built from an allowlist. What that means, and what it does not:

    * a check that writes gigabytes is read to the end, digested in full, and *retained* only up
      to the stream limit - so neither controller memory nor disk grows without bound, and the
      difference between "this is all of it" and "this is the head of it" is recorded rather
      than hidden. Draining continuously is also what keeps the child from blocking on a full
      pipe, which capturing to a file never had to worry about;
    * the retained bytes, their sizes, digests and truncation state are written as artifacts
      under the run's data directory, and the evidence row names them - so "the log is
      available" is a file a reader can actually open;
    * the environment is an allowlist of what a process needs to start plus the variables the
      caller declares, not the controller's environment with a few names deleted. A variable
      whose *name* looks like a credential is dropped even if it was declared, and the fact that
      it was dropped is recorded.

    The runner itself does nothing about Python bytecode. For a worktree run,
    :func:`verify_candidate` removes the worker's ``__pycache__`` bytecode before every command
    check (:func:`remove_worker_bytecode`), and the freeze refuses sourceless ``.pyc`` files.

    None of this is a sandbox: the check still runs with the current user's rights.
    """

    def __init__(
        self,
        excerpt_limit: int = DEFAULT_EXCERPT_BYTES,
        extra_env: dict[str, str] | None = None,
        *,
        stream_limit_bytes: int = DEFAULT_STREAM_LIMIT_BYTES,
        artifact_factory: Callable[[str, str], Path] | None = None,
        reader_timeout_seconds: float = 10.0,
        _observation_override: Callable[[ProcessBoundary], int | None] | None = None,
    ) -> None:
        self.excerpt_limit = excerpt_limit
        self.extra_env = dict(extra_env or {})
        self.stream_limit_bytes = max(0, int(stream_limit_bytes))
        #: Where this check's captured output and manifest go. ``None`` means a private temporary
        #: directory that is cleaned up afterwards - the offline/default behaviour. The controller
        #: supplies a run-scoped factory so a real run keeps a readable artifact.
        self._artifact_factory = artifact_factory
        #: How long the reader threads are given to finish after the child is gone. A descendant
        #: that inherited the pipe can hold it open; that is reported, not waited on forever.
        self.reader_timeout_seconds = reader_timeout_seconds
        # Test-only: supplies the boundary observation instead of querying it, so a lifecycle
        # test can reproduce a query that failed on a real Windows Job Object (the helper returns
        # ``None`` for that, exactly as it does where no job exists). Production code never
        # passes it.
        self._observation_override = _observation_override

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome:
        if not check.argv:
            return CheckOutcome(
                EvidenceStatus.ERROR, detail=f"check {check.id}: empty argv", exit_reason="empty_argv"
            )
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout_seconds))
        env, withheld = child_environment(extra=self.extra_env)
        argv = list(check.argv)

        scratch, artifact_dir, keep_artifacts = self._resolve_artifact_dir(check)
        if artifact_dir is None:
            # Refusing to run without somewhere to keep the captured output is the honest answer:
            # the alternative is a check whose evidence cannot be read after the fact.
            return CheckOutcome(
                EvidenceStatus.ERROR,
                detail=(
                    f"check {check.id}: no writable scratch directory is available for its output "
                    f"({scratch})"
                ),
                command=argv,
                exit_reason="no_artifact_dir",
                environment=environment_summary(env, withheld=withheld),
            )
        summary = environment_summary(env, withheld=withheld)
        return self._run_in_environment(
            check, cwd, argv, env, summary, started, deadline, artifact_dir, keep_artifacts
        )

    def _run_in_environment(
        self,
        check: CheckDef,
        cwd: Path,
        argv: list[str],
        env: dict[str, str],
        summary: str,
        started: float,
        deadline: float,
        artifact_dir: Path,
        keep_artifacts: bool,
    ) -> CheckOutcome:
        """One check's execution and evidence, in the environment :meth:`run` prepared."""
        out_sink = BoundedTextSink(artifact_dir / "stdout.txt", limit=self.stream_limit_bytes)
        err_sink = BoundedTextSink(artifact_dir / "stderr.txt", limit=self.stream_limit_bytes)
        try:
            result = self._execute(argv, check, cwd, env, out_sink, err_sink, deadline)
        except OSError as exc:
            # The artifact files themselves could not be opened: nothing ran.
            return CheckOutcome(
                EvidenceStatus.ERROR,
                detail=f"check {check.id}: its output could not be captured ({exc})",
                command=argv,
                exit_reason=REASON_OUTPUT_CAPTURE_ERROR,
                environment=summary,
            )
        stdout_capture = out_sink.capture()
        stderr_capture = err_sink.capture()
        elapsed = round(time.monotonic() - started, 3)
        outcome = self._outcome(
            check, argv, result, stdout_capture, stderr_capture, elapsed, summary
        )
        outcome.artifact_path = str(artifact_dir)
        outcome.artifacts = {
            "stdout": stdout_capture.as_dict(),
            "stderr": stderr_capture.as_dict(),
        }
        # A capture that did not finish cleanly is never a pass, and it is decided here - after
        # ``_outcome`` - so no other branch can return PASSED for a stream the runner could not
        # read. Two distinct endings both count: a reader still running when the wait expired, and
        # a reader that ended on an error (a failed read, a failed write to the artifact, a failed
        # flush/close). The second is the one a "did the thread exit?" test misses: the thread
        # exits, so the run looks complete.
        capture_failure = ""
        if result.get("readers_incomplete"):
            capture_failure = (
                "the check's output pipes were still held open after it exited, so its captured "
                "output is incomplete"
            )
        else:
            for name, status in dict(result.get("reader_status") or {}).items():
                if status != "eof":
                    capture_failure = (
                        f"the {name} capture did not finish cleanly ({status}), so its output is "
                        "incomplete"
                    )
                    break
            if not capture_failure:
                for name, capture in (("stdout", stdout_capture), ("stderr", stderr_capture)):
                    if capture.failed:
                        capture_failure = (
                            f"the {name} capture ended with an error ({capture.failure_reason}), "
                            "so its output is incomplete"
                        )
                        break
        if capture_failure:
            outcome.status = EvidenceStatus.ERROR
            outcome.exit_reason = REASON_OUTPUT_CAPTURE_ERROR
            outcome.detail += f" {capture_failure} and this is not a clean result"
        if truncated := (stdout_capture.truncated or stderr_capture.truncated):
            outcome.detail += (
                f" retained output was truncated at {self.stream_limit_bytes} byte(s) per stream;"
                " the digest covers the retained head, the artifact holds exactly that head"
            )
        manifest = write_artifact_manifest(
            artifact_dir,
            {
                "check_id": check.id,
                "argv": argv,
                "retained": outcome.artifacts,
                "truncated": bool(truncated),
                "capture_failure": capture_failure,
                "exit_code": outcome.exit_code,
                "exit_reason": outcome.exit_reason,
                "status": outcome.status.value,
                "elapsed_seconds": elapsed,
                "environment": summary,
            },
        )
        outcome.artifact_path = str(manifest)
        if not keep_artifacts:
            self._discard_artifacts(artifact_dir)
        return outcome

    def _resolve_artifact_dir(self, check: CheckDef) -> tuple[str, Path | None, bool]:
        """``(description, directory, keep)`` for this check's artifacts."""
        if self._artifact_factory is not None:
            try:
                directory = Path(self._artifact_factory(check.id, ""))
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return f"could not create {directory}: {exc}", None, True
            return str(directory), directory, True
        try:
            directory = Path(tempfile.mkdtemp(prefix=f"hflow-check-{check.id}-"))
        except OSError as exc:
            return f"could not create a temporary directory: {exc}", None, False
        return str(directory), directory, False

    @staticmethod
    def _discard_artifacts(directory: Path) -> None:
        """Remove a private temporary artifact directory; a failure here is not a check failure."""
        for child in sorted(directory.glob("**/*"), reverse=True):
            with contextlib.suppress(OSError):
                if child.is_file():
                    child.unlink()
                else:
                    child.rmdir()
        with contextlib.suppress(OSError):
            directory.rmdir()

    # -- one execution, fully owned ------------------------------------------

    def _execute(
        self,
        argv: list[str],
        check: CheckDef,
        cwd: Path,
        env: dict[str, str],
        out_sink: BoundedTextSink,
        err_sink: BoundedTextSink,
        deadline: float,
    ) -> dict[str, object]:
        """Launch, observe and settle one check. Never raises for a check-level failure."""
        try:
            boundary = ProcessBoundary().open()
        except (JobBoundaryError, OSError) as exc:
            # No boundary, no execution: an unmanaged launch could not be stopped afterwards.
            return {
                "phase": "boundary",
                "error": f"the check process boundary could not be established ({exc})",
            }

        child: subprocess.Popen | None = None
        readers: list[threading.Thread] = []
        details: list[str] = [f"boundary={boundary.kind}"]
        try:
            out_sink.__enter__()
            err_sink.__enter__()
            try:
                child = popen_in_boundary(
                    argv,
                    cwd=str(cwd),
                    env=env,
                    boundary=boundary,
                    stdout_handle=subprocess.PIPE,
                    stderr_handle=subprocess.PIPE,
                )
            except (JobBoundaryError, OSError, ValueError) as exc:
                # Covers "could not start" and "started but could not be assigned/resumed".
                # Nothing here ran unmanaged, so there is nothing to tear down.
                return {"phase": "startup", "error": f"the check could not be started ({exc})"}
            # The output pipes are drained continuously, on their own threads, so a check that
            # writes more than a pipe buffer holds cannot block waiting for a reader.
            readers = self._start_readers(child, out_sink, err_sink)
            # A check receives no interactive input. The helper creates a stdin pipe, so it is
            # closed here: a check that waits for EOF would otherwise hang until its deadline.
            self._close_stdin(child)

            timed_out = False
            try:
                returncode: int | None = child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = None
                # Deadline reached: stop what this runner owns, before asking whether the
                # boundary is quiet. Reaping the direct child is left to the finally block.
                terminated = boundary.terminate()
                details.append(
                    "timeout: the owned boundary was terminated "
                    f"({'the OS accepted it' if terminated else 'the request was refused'})"
                )

            settled = self._settle_descendants(boundary, details, deadline)
            if timed_out:
                # Reap first, then ask whether the direct child is really gone: the question
                # is answerable only once nothing is holding the process object open.
                self._reap_direct_child(child)
                gone = process_gone(child.pid, 0.0)
                # None is an unanswered query (the pid could not be opened), not an exit.
                details.append(f"direct_child_gone={'unknown' if gone is None else gone}")
            readers_incomplete = not self._join_readers(readers)
            reader_status = {
                str(getattr(thread, "_hflow_name", index)): str(
                    getattr(thread, "_hflow_status", "unknown")
                )
                for index, thread in enumerate(readers)
            }
            if readers_incomplete:
                details.append("output_pipes_still_open=True")
            if any(status != "eof" for status in reader_status.values()):
                details.append(
                    "reader_status="
                    + ",".join(f"{name}:{status}" for name, status in reader_status.items())
                )
            read_seconds = round(
                sum(getattr(thread, "_hflow_finished_at", 0.0) for thread in readers), 3
            )
            return {
                "phase": "completed",
                "returncode": returncode,
                "timed_out": timed_out,
                # ``settled`` is "settled", "forced" or "unknown"; only "settled" may pass.
                "settled": settled,
                "details": details,
                "readers_incomplete": readers_incomplete,
                "reader_status": reader_status,
                "read_seconds": read_seconds,
            }
        finally:
            # Everything that holds a pipe or a boundary handle is released here, on every path:
            # a reader left running would keep a pipe open, and on Windows a process still inside
            # the boundary holds its stdout/stderr handles. Closing the boundary also settles
            # whatever is left in it (kill-on-close).
            self._join_readers(readers)
            with contextlib.suppress(Exception):
                out_sink.close()
            with contextlib.suppress(Exception):
                err_sink.close()
            if child is not None:
                self._reap_direct_child(child)
            with contextlib.suppress(Exception):
                boundary.close()

    def _start_readers(
        self, child: subprocess.Popen, out_sink: BoundedTextSink, err_sink: BoundedTextSink
    ) -> list[threading.Thread]:
        """One drain thread per stream: bounded memory, no pipe back-pressure deadlock."""
        readers: list[threading.Thread] = []
        for name, stream, sink in (
            ("stdout", child.stdout, out_sink),
            ("stderr", child.stderr, err_sink),
        ):
            thread = threading.Thread(
                target=self._drain_stream, args=(stream, sink), daemon=True
            )
            thread._hflow_name = name  # type: ignore[attr-defined]
            thread.start()
            readers.append(thread)
        return readers

    @staticmethod
    def _drain_stream(stream: object, sink: BoundedTextSink) -> None:
        """Drain one stream, recording *how* it ended instead of discarding the reason.

        The status is kept on the thread and read by the caller: a thread that exits after a
        failed write is not a finished capture, and "the thread is gone" must never be read as
        "the output is complete".
        """
        status = "unknown"
        try:
            status = drain(stream, sink)  # type: ignore[arg-type]
        except (OSError, ValueError) as exc:  # defensive: drain reports instead of raising
            status = f"read_failed: {type(exc).__name__}: {exc}"
        finally:
            thread = threading.current_thread()
            thread._hflow_status = status  # type: ignore[attr-defined]
            thread._hflow_finished_at = time.monotonic()  # type: ignore[attr-defined]

    def _join_readers(self, readers: list[threading.Thread]) -> bool:
        """Wait a bounded time for the drain threads. ``False`` means a pipe is still held open."""
        deadline = time.monotonic() + self.reader_timeout_seconds
        for thread in readers:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        return all(not thread.is_alive() for thread in readers)

    def _reap_direct_child(self, child: subprocess.Popen) -> None:
        """Wait a bounded time for the direct child, then make sure it is gone.

        Called on every path, including the ones that already failed: a launched child must
        not be left behind because an earlier step went wrong.
        """
        try:
            child.wait(timeout=CHILD_REAP_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(Exception):
            child.kill()
        with contextlib.suppress(Exception):
            child.wait(timeout=CHILD_REAP_SECONDS)

    def _settle_descendants(
        self, boundary: ProcessBoundary, details: list[str], deadline: float
    ) -> str:
        """Let a finished check's boundary go quiet.

        Returns ``"settled"`` only when the boundary was *observed* to own no process, which is
        the one state a pass is allowed to rest on. ``"forced"`` means processes were seen and
        had to be terminated; ``"unknown"`` means the observation itself failed - a query that
        did not answer on a boundary that owns a tree, which must not be read as an empty tree.

        A direct child exiting is not by itself completion: a check that spawned helpers can
        leave them running. A boundary that is *known* not to own a tree (``direct_child_only``,
        i.e. no Job Object was created) keeps its documented weaker behaviour: nothing about the
        process tree is claimed there, including by a pass.
        """
        active = self._observation(boundary)
        if active is None:
            if boundary.kind == "direct_child_only":
                details.append("boundary_ownership=single_process_only_on_this_platform")
                return "settled"
            # The query did not answer although a boundary exists. Unknown is not empty.
            details.append("boundary_observation=unknown")
            return "unknown"

        settle_budget = min(DESCENDANT_SETTLE_SECONDS, max(0.0, deadline - time.monotonic()))
        if active > 0 and settle_budget > 0:
            boundary.wait_empty(settle_budget)
            active = self._observation(boundary)
            if active is None:
                details.append("boundary_observation=unknown")
                return "unknown"
        if active == 0:
            details.append("boundary_empty=True")
            return "settled"

        # Descendants outlived the check and the settle window: settle them by force, and say
        # so. The caller reports this as a lifecycle error, never as a clean pass.
        details.append(f"descendants_alive_after_settle={active}")
        boundary.terminate()
        emptied = boundary.wait_empty(
            min(BOUNDARY_EMPTY_SECONDS, max(0.0, deadline - time.monotonic()))
        )
        # An unconfirmed tear-down stays visible: it is never rounded up to "terminated".
        details.append(f"boundary_terminated_by_this_runner=True boundary_empty={emptied}")
        return "forced"

    def _observation(self, boundary: ProcessBoundary) -> int | None:
        """How many processes the boundary owns right now, or ``None`` when that is unknown."""
        if self._observation_override is not None:
            return self._observation_override(boundary)
        return boundary.active_processes()

    @staticmethod
    def _close_stdin(child: subprocess.Popen) -> None:
        if child.stdin is not None and not child.stdin.closed:
            with contextlib.suppress(OSError):
                child.stdin.close()

    # -- result mapping ------------------------------------------------------

    def _outcome(
        self,
        check: CheckDef,
        argv: list[str],
        result: dict[str, object],
        stdout: StreamCapture,
        stderr: StreamCapture,
        elapsed: float,
        environment: str,
    ) -> CheckOutcome:
        """Map one execution to a CheckOutcome. Only this method decides PASSED."""
        status = EvidenceStatus.ERROR
        returncode: int | None = None
        detail = ""
        reason = ""

        if result["phase"] in {"boundary", "startup"}:
            # Nothing ran, or nothing ran unmanaged. The reason is the whole detail.
            detail = f"check {check.id}: {result['error']}"
            reason = str(result["phase"])
        else:
            returncode = result["returncode"]  # type: ignore[assignment]
            timed_out = bool(result["timed_out"])
            settled = str(result["settled"])
            segments = [str(segment) for segment in result["details"] or []]  # type: ignore[union-attr]
            if timed_out:
                # A timeout stays an error whatever a process does afterwards: the check did
                # not finish inside its deadline, so nothing about its result is trustworthy.
                detail = (
                    f"check {check.id}: exit={returncode} elapsed={elapsed}s "
                    f"(timeout after {check.timeout_seconds}s)"
                )
                reason = REASON_TIMED_OUT
            else:
                detail = f"check {check.id}: exit={returncode} elapsed={elapsed}s"
                if returncode == 0 and settled == "settled":
                    status = EvidenceStatus.PASSED
                    reason = REASON_COMPLETED
                elif returncode != 0:
                    status = EvidenceStatus.FAILED
                    reason = REASON_NONZERO_EXIT
                else:
                    reason = f"settlement_{settled}"
            if settled == "forced":
                # A zero exit is not completion if the check left processes behind.
                segments.insert(
                    0,
                    "the check left processes running after it finished; its owned boundary was "
                    "terminated, so this is a lifecycle error, not a clean result",
                )
                reason = REASON_SETTLEMENT_FORCED
            elif settled == "unknown":
                # Distinct from the case above on purpose: no lingering process was observed
                # here, the observation itself failed. Saying "processes were left running"
                # would report a fact this run does not have.
                segments.insert(
                    0,
                    "the owned boundary could not be observed, so settlement is unconfirmed; an "
                    "unanswered query is not an empty process tree and this is not a clean result",
                )
                reason = REASON_SETTLEMENT_UNKNOWN
            detail += " " + " ".join(segments)

        if stderr.total_bytes:
            # The head of the stream, decoded for the evidence row; the retained stream stays in
            # the artifact file this row names.
            # Under a marker that is not a reference spelling: these are the check's bytes.
            detail += f" {STDERR_EXCERPT_MARKER}" + stderr.head[: self.excerpt_limit]
            if stderr.truncated:
                detail += "… (stderr truncated for display)"
        return CheckOutcome(
            status,
            exit_code=returncode,
            stdout_digest=stdout.digest,
            stderr_digest=stderr.digest,
            detail=detail,
            command=argv,
            exit_reason=reason,
            timed_out=bool(result.get("timed_out")),
            environment=environment,
        )


class DenyCheckRunner:
    """Default runner for kinds this build cannot execute: refuse, never fake it."""

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome:
        return CheckOutcome(
            EvidenceStatus.ERROR,
            detail=(
                f"check {check.id!r} has kind {check.kind!r}, which this runtime cannot execute; "
                "refusing rather than reporting an unverified pass"
            ),
            # Nothing was started, and the reason says so: a refusal to run is not a check that
            # failed its business assertion.
            exit_reason=REASON_NOT_LAUNCHED,
        )


class CheckRunners:
    """Registry from check kind to runner. Unknown kinds are refused, not guessed."""

    def __init__(self, runners: dict[str, CheckRunner] | None = None) -> None:
        self.runners: dict[str, CheckRunner] = dict(runners or {})

    @classmethod
    def offline_default(
        cls,
        extra_env: dict[str, str] | None = None,
        *,
        artifact_factory: Callable[[str, str], Path] | None = None,
    ) -> CheckRunners:
        return cls(
            {
                "fake": FakeCheckRunner(),
                "command": CommandCheckRunner(
                    extra_env=extra_env, artifact_factory=artifact_factory
                ),
            }
        )

    def for_kind(self, kind: str) -> CheckRunner:
        return self.runners.get(kind, DenyCheckRunner())


def verify_candidate(
    *,
    store: Store,
    spec: TaskSpec,
    project: ProjectConfig,
    project_root: Path,
    project_checks_digest: str,
    candidate_fingerprint: str,
    attempt_id: str,
    run_id: str,
    runners: CheckRunners,
    artifact_factory: Callable[[str, str], Path] | None = None,
    force_refresh: bool = False,
    time_budget_seconds: int | None = None,
    remove_bytecode: bool = False,
    committed_bytecode: Iterable[str] = (),
) -> VerificationResult:
    """Execute every required check once for this candidate; store evidence.

    Reuse rule (acceptance A10): an existing passing evidence row with the same
    candidate fingerprint and checks digest is reused for cacheable checks, and
    never reused for checks marked ``cacheable=False``.

    ``force_refresh=True`` re-runs every required check, including cacheable ones, even when a
    passing row with the same fingerprint and checks digest exists. Batch E2 passes it for a
    repaired candidate so the second round never inherits the first round's pass - "the same
    fingerprint" is not "the same round", and a repaired candidate's evidence has to be produced
    by an execution of *this* round. The default preserves the historical reuse rule exactly.

    ``time_budget_seconds`` is what is left of the root's clock, and it caps **each** check: a
    check must not be given its full configured timeout when the root it belongs to has less time
    than that, because the run would then keep working past the deadline it recorded and could
    accept a delivery that finished after it. ``None`` means no root clock is known and the
    configured timeouts apply unchanged. When the budget is already exhausted, no process is
    started at all: each remaining check is recorded as a timed-out error, which blocks the run
    and can never trigger a repair.

    ``artifact_factory`` decides where a check's captured output is kept. The controller passes a
    run-scoped directory under its own data dir, so the log a reviewer is pointed at is a file in
    the run's record; without one (offline callers) the runner uses a private temporary directory
    and cleans it up.

    ``remove_bytecode=True`` (a worktree run: ``project_root`` is the candidate's worktree) removes
    the ``__pycache__`` bytecode in that worktree before **every** command check, so a cache the
    worker left can never stand in for the committed source, whatever flags the check passes to
    Python (see :func:`remove_worker_bytecode`). ``committed_bytecode`` names the bytecode paths the
    candidate commit tracks, which are kept. Each such check's evidence says how much was removed,
    and a run note records the total. When something bytecode-shaped could not be removed safely
    (a link, a junction, a file that could not be deleted) the check is not started: it is recorded
    as an error that never buys a repair. An in-place run passes ``False``: its working tree is the
    user's checkout, and nothing in it is deleted.
    """
    check_map = project.check_map()
    required = spec.required_check_ids()
    if not required:
        return VerificationResult(status="not_run", detail="no checks required by acceptance")

    existing = [dict(r) for r in store.evidence_for(run_id, kind="verification")]
    evidence_ids: list[str] = []
    failure_details: list[str] = []
    overall = EvidenceStatus.PASSED
    # One countdown for the whole loop, shared by every check: the root's budget is what the run
    # has left, not what each check may spend on its own.
    budget_deadline = (
        time.monotonic() + float(time_budget_seconds)
        if time_budget_seconds is not None
        else None
    )

    removed_files = 0
    for check_id in required:
        check = check_map.get(check_id)
        if check is None:
            raise RefusedError(
                RefusalCode.UNKNOWN_CHECK,
                f"check {check_id!r} disappeared from the project contract since admission",
            )
        reusable = None
        if check.cacheable and not force_refresh:
            for row in existing:
                if (
                    row["check_id"] == check_id
                    and row["status"] == EvidenceStatus.PASSED.value
                    and row["candidate_fingerprint"] == candidate_fingerprint
                    and row["checks_digest"] == project_checks_digest
                ):
                    reusable = row
                    break
        if reusable is not None:
            evidence_ids.append(reusable["evidence_id"])
            for runner in runners.runners.values():
                if isinstance(runner, FakeCheckRunner):
                    runner.cache_hits += 1
            continue

        evidence_id = new_evidence_id()
        runner = runners.for_kind(check.kind)
        if isinstance(runner, CommandCheckRunner) and artifact_factory is not None:
            # One directory per check execution, named after the evidence row it belongs to, so
            # the artifact and the row can be matched in both directions. The runner is rebuilt
            # with that factory rather than mutated, so a shared runner instance in a registry
            # never carries one run's directory into another run.
            runner = CommandCheckRunner(
                excerpt_limit=runner.excerpt_limit,
                extra_env=runner.extra_env,
                stream_limit_bytes=runner.stream_limit_bytes,
                artifact_factory=lambda _check, _ignored, _eid=evidence_id: artifact_factory(
                    check_id, _eid
                ),
                reader_timeout_seconds=runner.reader_timeout_seconds,
            )
        # The check gets the smaller of its own configured timeout and what the root has left. The
        # longer of the two would let the run outlive the deadline it recorded and then accept a
        # delivery produced after it - the deadline would be a note rather than a bound.
        #
        # "What the root has left" is re-read for every check, not taken once for the whole loop:
        # each check consumes time, so a budget of 2 seconds would otherwise be handed to the first
        # check *and* again to the second, and the pair could run for four. The countdown starts
        # when this function was entered and is measured monotonically, so a clock correction or a
        # long-running first check cannot silently give the later checks more time than the run has.
        effective_timeout = int(check.timeout_seconds)
        if budget_deadline is not None:
            remaining_budget = budget_deadline - time.monotonic()
            if remaining_budget < 1:
                # Less than a whole second left: record the fact without starting a process. The
                # reason is `timed_out`, which is deliberately *not* a clean completion, so this can
                # never buy a repair and the run stops instead of continuing on an exhausted clock.
                outcome = CheckOutcome(
                    EvidenceStatus.ERROR,
                    exit_code=None,
                    detail=(
                        f"check {check_id!r} was not started: the root's remaining time "
                        f"({max(0.0, remaining_budget):.3f}s) is exhausted"
                    ),
                    exit_reason="timed_out",
                )
                effective_timeout = 0
            else:
                effective_timeout = min(int(check.timeout_seconds), int(remaining_budget))
        if effective_timeout > 0 and remove_bytecode and check.kind == "command":
            removal = remove_worker_bytecode(project_root, keep=committed_bytecode)
            removed_files += removal.files
            if removal.refused:
                outcome = CheckOutcome(
                    EvidenceStatus.ERROR,
                    detail=(
                        f"check {check_id!r} was not started: bytecode in the candidate worktree "
                        "could not be removed safely, and Python could load it in place of the "
                        "committed source ("
                        + "; ".join(removal.refused[:5])
                        + (
                            f"; +{len(removal.refused) - 5} more"
                            if len(removal.refused) > 5
                            else ""
                        )
                        + "); nothing was deleted outside __pycache__ and no link was followed"
                    ),
                    exit_reason=REASON_BYTECODE_NOT_CLEARED,
                )
            else:
                outcome = runner.run(check, project_root, effective_timeout)
                outcome.detail = (
                    f"{outcome.detail} ({removal.note()})" if outcome.detail else removal.note()
                )
        elif effective_timeout > 0:
            outcome = runner.run(check, project_root, effective_timeout)

        # The evidence row carries a human-readable reason and the *readable* artifact references,
        # because "the full log is available" has to name a file a reader can open. Only the
        # paths, sizes, digests and truncation state are added; the detail and digests the row
        # already had are unchanged, so nothing downstream has to parse this to work.
        record = store.record_evidence(
            evidence_id=evidence_id,
            run_id=run_id,
            attempt_id=attempt_id,
            kind="verification",
            status=outcome.status,
            candidate_fingerprint=candidate_fingerprint,
            checks_digest=project_checks_digest,
            check_id=check_id,
            command=outcome.command,
            exit_code=outcome.exit_code,
            # The structured execution fact, stored as its own column: a repair decision is
            # classified from it, never from the ``verification_failed`` text or a log keyword.
            # An offline runner that observed nothing reports no reason, and that is honest.
            exit_reason=outcome.exit_reason,
            stdout_digest=outcome.stdout_digest,
            stderr_digest=outcome.stderr_digest,
            detail=_detail_with_references(outcome),
        )
        evidence_ids.append(record.evidence_id)
        if outcome.status is not EvidenceStatus.PASSED:
            failure_details.append(f"{check_id}: {outcome.status.value} ({outcome.detail})")
            overall = EvidenceStatus.FAILED if overall is EvidenceStatus.PASSED else overall

    if removed_files:
        store.record_note(
            run_id,
            f"{NOTE_BYTECODE_REMOVED}: {removed_files} ignored .pyc file(s) under __pycache__ were "
            "removed from the candidate worktree before its command checks; they are never "
            "committed or fingerprinted, so the candidate commit and fingerprint are unchanged",
        )
    if failure_details:
        return VerificationResult(
            status="failed",
            evidence_ids=evidence_ids,
            detail="; ".join(failure_details),
        )
    return VerificationResult(
        status="passed",
        evidence_ids=evidence_ids,
        detail=f"{len(required)} check(s) passed for candidate {candidate_fingerprint}",
    )


def _detail_with_references(outcome: CheckOutcome) -> str:
    """The record of one check: reason, references, then the human-readable detail.

    The reference fields come **first** on purpose. Anything downstream that shortens this string
    (a log line cap, a packet summary) cuts the tail, and the tail must not be the part that says
    where the artifact is and what the check could see. The order is fixed, so the same outcome
    always produces the same string.
    """
    references: list[str] = []
    if outcome.exit_reason:
        references.append(f"reason={outcome.exit_reason}")
    if outcome.artifact_path:
        references.append(f"artifact={outcome.artifact_path}")
    for name, capture in sorted(outcome.artifacts.items()):
        references.append(
            f"{name}: {capture.get('retained_bytes')}/{capture.get('total_bytes')} bytes"
            f" truncated={capture.get('truncated')} digest={capture.get('digest')}"
        )
    if outcome.environment:
        # The environment summary is a named list, so it also belongs in the reference block: it
        # is evidence ("the check could not see X"), not prose.
        references.append(outcome.environment)
    if outcome.detail:
        references.append(outcome.detail)
    return " ".join(references)


def evidence_is_current(
    rows: Iterable[dict[str, object]],
    *,
    status: str,
    candidate_fingerprint: str,
    checks_digest: str,
) -> bool:
    """True only if a stored row matches every freshness key.

    Kept as a standalone predicate so ``controller._accept`` and the tests agree on
    what "current evidence" means instead of each re-implementing the comparison.
    """
    return any(
        row.get("status") == status
        and row.get("candidate_fingerprint") == candidate_fingerprint
        and row.get("checks_digest") == checks_digest
        for row in rows
    )


# --------------------------------------------------------------------------
# The facts a repair decision reads (batch E2)
# --------------------------------------------------------------------------
#
# ``failed_check_facts`` is the one place a caller may read "what failed about this candidate"
# from. It reads the stored row - the structured columns, not a re-derivation of the detail text -
# and it filters by attempt *and* candidate, because round one's evidence shares the run: a query
# by ``run_id`` alone would let a previous candidate's failure describe the current one.
#
# The ``artifact``/``stdout``/``stderr`` references are decoded from the detail text that
# ``_detail_with_references`` writes. The reader lives next to the writer, and the controller
# imports this one reader for the reviewer packet, so the format has exactly one reader.

#: The marker the check's own stderr head is written under, inside the free-text detail. It is
#: deliberately *not* a reference spelling: a check controls these bytes, so they must never read
#: as HFlow's ``stderr:`` capture reference. Rows written before this marker existed carry
#: `` stderr=<head>`` instead; the reader below never searches the free text, so both kinds of
#: row read back to HFlow's real reference.
STDERR_EXCERPT_MARKER = "stderr_excerpt="

#: The retained/total/truncated/digest shape of one stream reference. Both spellings are accepted
#: on purpose: the evidence row writes ``stdout: 3000/3000 bytes`` (reading like a log line) and
#: the reviewer packet writes ``stdout=3000/3000B``.
_STREAM_REFERENCE = re.compile(
    r"^(?P<retained>\d+)\s*/\s*(?P<total>\d+)\s*B?(?:\s*bytes)?"
    r"\s+truncated=(?P<truncated>\w+)"
    r"\s+digest=(?P<digest>\S+)"
)

_STREAM_VALUE = r"\d+\s*/\s*\d+\s*B?(?:\s*bytes)?\s+truncated=\w+\s+digest=\S+"

#: One stream segment of the reference block. ``name: value`` (the row's spelling) is tried before
#: ``name=value`` (the packet's).
_BLOCK_STREAM = re.compile(rf"(?P<name>[a-z_]+)(?::\s|=)(?P<value>{_STREAM_VALUE})(?= |$)")
_BLOCK_REASON = re.compile(r"reason=(?P<value>\S+)(?= |$)")
#: Where an ``artifact=`` value ends: at the next segment of the block (a stream reference or the
#: environment summary), preceded by one space. A path may contain spaces, so it is never split
#: on whitespace: "C:/temp/run 1/artifact.json" is one value, not two tokens.
_ARTIFACT_END = re.compile(rf" (?=[a-z_]+(?::\s|=){_STREAM_VALUE}(?: |$)|env_names=)")
#: The free text a command check's detail starts with. It bounds an artifact value that no block
#: segment follows, so the value never runs on into text the check wrote.
_FREE_TEXT_START = re.compile(r" (?=check \S)")


def _reference_block(detail: str) -> dict[str, str]:
    """The references in HFlow's **leading** block of an evidence row's detail text.

    ``_detail_with_references`` writes, in this fixed order: ``reason=``, ``artifact=``, one
    ``<stream>: R/T bytes truncated=.. digest=..`` per captured stream, the environment summary,
    and only then the free-text detail, which carries text the check itself printed. The block is
    read segment by segment from the start, and reading stops at the first segment that is not one
    of these. So nothing a check wrote (a ``stderr=`` or ``stdout:`` look-alike in its stderr, for
    example) can stand in for, or hide, HFlow's own reference.
    """
    fields: dict[str, str] = {}
    position = 0
    match = _BLOCK_REASON.match(detail, position)
    if match is not None:
        fields["reason"] = match.group("value")
        position = match.end() + 1
    if detail.startswith("artifact=", position):
        value_start = position + len("artifact=")
        end_match = _ARTIFACT_END.search(detail, value_start) or _FREE_TEXT_START.search(
            detail, value_start
        )
        end = end_match.start() if end_match is not None else len(detail)
        fields["artifact"] = detail[value_start:end].strip()
        position = end + 1
    while position < len(detail):
        match = _BLOCK_STREAM.match(detail, position)
        if match is None:
            break
        # The first reference of a name wins; a repeated name never overwrites it.
        fields.setdefault(match.group("name"), match.group("value"))
        position = match.end() + 1
    return fields


def _reference_field(detail: str, field: str) -> str:
    """One reference (``reason``, ``artifact``, ``stdout``, ``stderr``) from the leading block.

    ``""`` when the block does not carry it. The free-text tail is never searched.
    """
    return _reference_block(detail).get(field, "")


def _stream_reference(detail: str, name: str) -> dict[str, object]:
    """The retained-bytes/truncated/digest facts for one stream, out of the detail text.

    ``{}`` means the row holds no usable reference for that stream - reported as "not recorded"
    rather than filled in with zeros a reader could mistake for a real capture.
    """
    value = _reference_field(detail, name)
    if not value:
        return {}
    match = _STREAM_REFERENCE.match(value)
    if match is None:
        return {}
    return {
        "retained_bytes": match.group("retained"),
        "total_bytes": match.group("total"),
        "truncated": match.group("truncated"),
        "digest": match.group("digest"),
    }


def failed_check_facts(
    store: Store,
    *,
    run_id: str,
    attempt_id: str,
    candidate_fingerprint: str,
    checks_digest: str,
) -> list[dict[str, object]]:
    """Every verification row of **this attempt and this candidate** that did not pass.

    A row qualifies only when all four freshness keys match: the run, the attempt, the candidate
    fingerprint and the checks digest. The candidate filter is the important one - a previous
    round's failed evidence is still part of the same run, so a query by run alone would let round
    one's failure be read as a fact about round two's candidate.

    FAILED *and* ERROR rows are returned, in the order they were recorded. A caller that only saw
    the failures could not notice that an error was mixed in, and the E2 rule is that any error in
    the round stops the run without buying a second implementer.

    Every value comes from the stored row (``exit_reason`` included - empty for a row written
    before the reason was recorded) or from the reference block that row's own detail text
    carries. Nothing here re-derives a reason from prose.
    """
    facts: list[dict[str, object]] = []
    for row in store.evidence_for(run_id, kind="verification"):
        if str(row["attempt_id"]) != attempt_id:
            continue
        if str(row["candidate_fingerprint"]) != candidate_fingerprint:
            continue
        if str(row["checks_digest"]) != checks_digest:
            continue
        if str(row["status"]) == EvidenceStatus.PASSED.value:
            continue
        detail = str(row["detail"] or "")
        facts.append(
            {
                "check_id": str(row["check_id"] or ""),
                "status": str(row["status"]),
                "exit_code": row["exit_code"],
                "exit_reason": str(row["exit_reason"] or ""),
                "detail": detail,
                "evidence_id": str(row["evidence_id"]),
                "artifact": _reference_field(detail, "artifact"),
                "stdout": _stream_reference(detail, "stdout"),
                "stderr": _stream_reference(detail, "stderr"),
            }
        )
    return facts
