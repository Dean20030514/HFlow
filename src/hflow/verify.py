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

import subprocess
import tempfile
import time
from collections.abc import Iterable
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
from .ids import new_evidence_id
from .store import Store


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
    """Runs an approved argv as a subprocess.

    Output is captured through temporary files rather than pipes: it is portable,
    avoids deadlocks on large output, and keeps the controller free of a reader
    thread. Only digests and a short excerpt reach the database.
    """

    def __init__(self, excerpt_limit: int = 2000) -> None:
        self.excerpt_limit = excerpt_limit

    def run(self, check: CheckDef, cwd: Path, timeout_seconds: int) -> CheckOutcome:
        if not check.argv:
            return CheckOutcome(EvidenceStatus.ERROR, detail=f"check {check.id}: empty argv")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="hflow-check-") as tmp:
            out_path = Path(tmp) / "stdout.txt"
            err_path = Path(tmp) / "stderr.txt"
            try:
                with out_path.open("wb") as out, err_path.open("wb") as err:
                    completed = subprocess.run(  # noqa: S603 - argv comes from the project contract
                        list(check.argv),
                        cwd=str(cwd),
                        stdout=out,
                        stderr=err,
                        timeout=timeout_seconds,
                        check=False,
                    )
                returncode: int | None = completed.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                returncode, timed_out = None, True
            except OSError as exc:
                return CheckOutcome(
                    EvidenceStatus.ERROR,
                    detail=f"check {check.id} could not start: {exc}",
                    command=list(check.argv),
                )
            stdout = out_path.read_bytes() if out_path.exists() else b""
            stderr = err_path.read_bytes() if err_path.exists() else b""
        elapsed = round(time.monotonic() - started, 3)
        detail = f"check {check.id}: exit={returncode} elapsed={elapsed}s"
        if timed_out:
            detail += f" (timeout after {timeout_seconds}s)"
        if stderr:
            detail += " stderr=" + stderr[: self.excerpt_limit].decode("utf-8", "replace")
        return CheckOutcome(
            EvidenceStatus.ERROR if timed_out else (
                EvidenceStatus.PASSED if returncode == 0 else EvidenceStatus.FAILED
            ),
            exit_code=returncode,
            stdout_digest=digest_of(stdout.decode("utf-8", "replace")),
            stderr_digest=digest_of(stderr.decode("utf-8", "replace")),
            detail=detail,
            command=list(check.argv),
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
    def offline_default(cls) -> CheckRunners:
        return cls({"fake": FakeCheckRunner(), "command": CommandCheckRunner()})

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
