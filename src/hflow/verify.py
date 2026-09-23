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
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

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


class CheckOutcome:
    """Result of one check execution, before it becomes stored evidence."""

    __slots__ = ("command", "detail", "exit_code", "stderr_digest", "stdout_digest", "status")

    def __init__(
        self,
        status: EvidenceStatus,
        *,
        exit_code: int | None = None,
        stdout_digest: str = "",
        stderr_digest: str = "",
        detail: str = "",
        command: list[str] | None = None,
    ) -> None:
        self.status = status
        self.exit_code = exit_code
        self.stdout_digest = stdout_digest
        self.stderr_digest = stderr_digest
        self.detail = detail
        self.command = command or []


class CheckRunner(Protocol):
    """A way to execute one approved check kind. Deliberately tiny."""

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome: ...


class FakeCheckRunner:
    """Offline runner for kind=fake. Its verdicts come from the caller, not the model."""

    def __init__(self, verdicts: dict[str, EvidenceStatus] | None = None) -> None:
        self.verdicts = verdicts or {}
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

    Output-size limits and environment minimization are not part of this slice:
    ``extra_env`` is still merged over the ambient environment.
    """

    def __init__(
        self,
        excerpt_limit: int = 2000,
        extra_env: dict[str, str] | None = None,
        *,
        _observation_override: Callable[[ProcessBoundary], int | None] | None = None,
    ) -> None:
        self.excerpt_limit = excerpt_limit
        self.extra_env = dict(extra_env or {})
        # Test-only: supplies the boundary observation instead of querying it, so a lifecycle
        # test can reproduce a query that failed on a real Windows Job Object (the helper returns
        # ``None`` for that, exactly as it does where no job exists). Production code never
        # passes it.
        self._observation_override = _observation_override

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome:
        if not check.argv:
            return CheckOutcome(EvidenceStatus.ERROR, detail=f"check {check.id}: empty argv")
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout_seconds))
        env = {**os.environ, **self.extra_env}
        argv = list(check.argv)
        # Deliberately not ``TemporaryDirectory``: on Windows a process that still holds the
        # captured file open makes deleting it fail, and failing to delete a log file must not
        # turn a check's real result into an exception. Removal is explicit, best effort and
        # bounded, and a file that stays behind is reported as a fact in the detail text.
        try:
            scratch = Path(tempfile.mkdtemp(prefix="hflow-check-"))
            out_path = scratch / "stdout.txt"
            err_path = scratch / "stderr.txt"
        except OSError as exc:
            # Refusing to run without somewhere to capture output is the honest answer: the
            # alternative is a check whose evidence cannot be recorded.
            return CheckOutcome(
                EvidenceStatus.ERROR,
                detail=f"check {check.id}: no writable scratch directory for its output ({exc})",
                command=argv,
            )
        kept = 0
        try:
            result = self._execute(argv, check, cwd, env, out_path, err_path, deadline)
            stdout = out_path.read_bytes() if out_path.exists() else b""
            stderr = err_path.read_bytes() if err_path.exists() else b""
        finally:
            kept = self._discard_output_files(scratch, (out_path, err_path))
        elapsed = round(time.monotonic() - started, 3)
        if kept:
            result["details"] = [*(result.get("details") or []), "output_files_kept=1"]
        return self._outcome(check, argv, result, stdout, stderr, elapsed)

    @staticmethod
    def _discard_output_files(directory: Path, paths: tuple[Path, ...]) -> int:
        """Remove this call's scratch files; report how many could not be removed."""
        for _ in range(10):
            try:
                for path in paths:
                    path.unlink(missing_ok=True)
                directory.rmdir()
                return 0
            except OSError:
                time.sleep(0.02)
        try:
            directory.rmdir()
            return 0
        except OSError:
            return 1

    # -- one execution, fully owned ------------------------------------------

    def _execute(
        self,
        argv: list[str],
        check: CheckDef,
        cwd: Path,
        env: dict[str, str],
        out_path: Path,
        err_path: Path,
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
        details: list[str] = [f"boundary={boundary.kind}"]
        try:
            try:
                with out_path.open("wb") as out, err_path.open("wb") as err:
                    child = popen_in_boundary(
                        argv,
                        cwd=str(cwd),
                        env=env,
                        boundary=boundary,
                        stdout_handle=out,
                        stderr_handle=err,
                    )
            except (JobBoundaryError, OSError, ValueError) as exc:
                # Covers "could not start" and "started but could not be assigned/resumed".
                # Nothing here ran unmanaged, so there is nothing to tear down.
                return {"phase": "startup", "error": f"the check could not be started ({exc})"}
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
                details.append(f"direct_child_gone={process_gone(child.pid, 0.0)}")
            return {
                "phase": "completed",
                "returncode": returncode,
                "timed_out": timed_out,
                # ``settled`` is "settled", "forced" or "unknown"; only "settled" may pass.
                "settled": settled,
                "details": details,
            }
        finally:
            # Both steps happen before the caller reads the captured output: on Windows a
            # process that is still inside the boundary holds its stdout/stderr handles open,
            # and reading or deleting those files while it does is a sharing violation. The
            # boundary is therefore released here - kill-on-close settles whatever is left -
            # and this also guarantees the handle never outlives the call.
            if child is not None:
                self._reap_direct_child(child)
            with contextlib.suppress(Exception):
                boundary.close()

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
        stdout: bytes,
        stderr: bytes,
        elapsed: float,
    ) -> CheckOutcome:
        """Map one execution to a CheckOutcome. Only this method decides PASSED."""
        status = EvidenceStatus.ERROR
        returncode: int | None = None
        detail = ""

        if result["phase"] in {"boundary", "startup"}:
            # Nothing ran, or nothing ran unmanaged. The reason is the whole detail.
            detail = f"check {check.id}: {result['error']}"
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
            else:
                detail = f"check {check.id}: exit={returncode} elapsed={elapsed}s"
                if returncode == 0 and settled == "settled":
                    status = EvidenceStatus.PASSED
                elif returncode != 0:
                    status = EvidenceStatus.FAILED
            if settled == "forced":
                # A zero exit is not completion if the check left processes behind.
                segments.insert(
                    0,
                    "the check left processes running after it finished; its owned boundary was "
                    "terminated, so this is a lifecycle error, not a clean result",
                )
            elif settled == "unknown":
                # Distinct from the case above on purpose: no lingering process was observed
                # here, the observation itself failed. Saying "processes were left running"
                # would report a fact this run does not have.
                segments.insert(
                    0,
                    "the owned boundary could not be observed, so settlement is unconfirmed; an "
                    "unanswered query is not an empty process tree and this is not a clean result",
                )
            detail += " " + " ".join(segments)

        if stderr:
            detail += " stderr=" + stderr[: self.excerpt_limit].decode("utf-8", "replace")
        return CheckOutcome(
            status,
            exit_code=returncode,
            stdout_digest=digest_of(stdout.decode("utf-8", "replace")),
            stderr_digest=digest_of(stderr.decode("utf-8", "replace")),
            detail=detail,
            command=argv,
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
        )


class CheckRunners:
    """Registry from check kind to runner. Unknown kinds are refused, not guessed."""

    def __init__(self, runners: dict[str, CheckRunner] | None = None) -> None:
        self.runners: dict[str, CheckRunner] = dict(runners or {})

    @classmethod
    def offline_default(cls, extra_env: dict[str, str] | None = None) -> CheckRunners:
        return cls({"fake": FakeCheckRunner(), "command": CommandCheckRunner(extra_env=extra_env)})

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
) -> VerificationResult:
    """Execute every required check once for this candidate; store evidence.

    Reuse rule (acceptance A10): an existing passing evidence row with the same
    candidate fingerprint and checks digest is reused for cacheable checks, and
    never reused for checks marked ``cacheable=False``.
    """
    check_map = project.check_map()
    required = spec.required_check_ids()
    if not required:
        return VerificationResult(status="not_run", detail="no checks required by acceptance")

    existing = [dict(r) for r in store.evidence_for(run_id, kind="verification")]
    evidence_ids: list[str] = []
    failure_details: list[str] = []
    overall = EvidenceStatus.PASSED

    for check_id in required:
        check = check_map.get(check_id)
        if check is None:
            raise RefusedError(
                RefusalCode.UNKNOWN_CHECK,
                f"check {check_id!r} disappeared from the project contract since admission",
            )
        reusable = None
        if check.cacheable:
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

        outcome = runners.for_kind(check.kind).run(check, project_root, check.timeout_seconds)
        record = store.record_evidence(
            evidence_id=new_evidence_id(),
            run_id=run_id,
            attempt_id=attempt_id,
            kind="verification",
            status=outcome.status,
            candidate_fingerprint=candidate_fingerprint,
            checks_digest=project_checks_digest,
            check_id=check_id,
            command=outcome.command,
            exit_code=outcome.exit_code,
            stdout_digest=outcome.stdout_digest,
            stderr_digest=outcome.stderr_digest,
            detail=outcome.detail,
        )
        evidence_ids.append(record.evidence_id)
        if outcome.status is not EvidenceStatus.PASSED:
            failure_details.append(f"{check_id}: {outcome.status.value} ({outcome.detail})")
            overall = EvidenceStatus.FAILED if overall is EvidenceStatus.PASSED else overall

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
