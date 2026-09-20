"""Offline driver for tests and for exercising the controller without any model.

The fake driver can *only* report an invocation outcome plus an optional candidate
description and review verdict. ``TaskState``, verification results and receipts
are unreachable from here by construction, which is what makes the "a worker
cannot accept its own task" rule structural rather than a convention.

It is a development fixture. Passing tests with it proves controller behaviour and
nothing about real DSH/acpx interoperability (plan 18, M4 boundary).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts import (
    AgentBinding,
    CandidateRef,
    CapabilityReport,
    CapabilityState,
    CancellationReceipt,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    ReconcileOutcome,
    ReconcileResult,
    ReviewOutput,
)
from ..ids import utc_now

DRIVER_ID = "fake-offline"
DRIVER_VERSION = "0.1.0"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class FakeScript:
    """What the fake worker does on its next invocation."""

    outcome: InvocationOutcome = InvocationOutcome.COMPLETED
    write_plan: dict[str, str] = field(default_factory=dict)
    remove_plan: list[str] = field(default_factory=list)
    agent_turns: int | None = 1
    #: Verdict the fake reviewer returns when a review is requested. Tests that want
    #: a rejection replace this explicitly; the default is an accepting review.
    review: ReviewOutput | None = field(
        default_factory=lambda: ReviewOutput(verdict="accepted", findings=[])
    )
    #: Reviewer-side isolation claim from the (fake) model, used to prove that a
    #: self-claimed isolation level never upgrades the recorded one (acceptance A08).
    claimed_isolation: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    limitations: list[str] = field(default_factory=lambda: ["fake driver: no model was invoked"])
    #: Invocations that model an interrupted run: no result is produced.
    unknown_invocations: int = 0
    #: Test hook: change a scoped file *after* verification, to prove stale evidence
    #: is rejected instead of being reused (acceptance A09).
    mutate_after_verify: bool = False


class FakeDriver:
    driver_id = DRIVER_ID
    driver_version = DRIVER_VERSION

    def __init__(self, project_root: Path, script: FakeScript | None = None) -> None:
        self.project_root = Path(project_root)
        self.script = script or FakeScript()
        self.started: list[InvocationRequest] = []
        self.cancelled: list[str] = []
        self.reconciled: list[str] = []
        self._unknown_seen = 0

    # -- HarnessDriver protocol ---------------------------------------------

    def probe(self, binding: AgentBinding) -> CapabilityReport:
        """No model calls. Real capabilities are unknown until a live probe runs."""
        return CapabilityReport(
            driver_id=DRIVER_ID,
            driver_version=DRIVER_VERSION,
            harness=binding.harness,
            harness_version=None,
            os=f"{platform.system()}-{platform.release()}",
            arch=platform.machine(),
            probe_only=True,
            live_tested=False,
            capabilities={
                "fresh_session": CapabilityState.PROBED,
                "cancel": CapabilityState.PROBED,
                "native_subagents": CapabilityState.UNSUPPORTED,
                "model_selection": CapabilityState.UNSUPPORTED,
                "billing_usage": CapabilityState.UNKNOWN,
                "readonly_enforcement": CapabilityState.UNSUPPORTED,
                "structured_output": CapabilityState.PROBED,
            },
            notes=[
                "offline fake: exercises the controller, proves nothing about a real harness",
                "no model provider is contacted",
            ],
        )

    def start(self, request: InvocationRequest) -> InvocationResult:
        self.started.append(request)
        attempt_dir = Path(request.workspace)

        if self._unknown_seen < self.script.unknown_invocations:
            self._unknown_seen += 1
            return InvocationResult(
                invocation_id=request.invocation_id,
                outcome=InvocationOutcome.OUTCOME_UNKNOWN,
                agent_turns=None,
                error_code="interrupted",
                error_message="fake worker was interrupted; the result is unknown",
                limitations=["no result was produced; do not re-dispatch blindly"],
            )

        for relative in self.script.remove_plan:
            target = attempt_dir / relative
            if target.is_file():
                target.unlink()

        if self.script.outcome is InvocationOutcome.COMPLETED:
            for relative, text in self.script.write_plan.items():
                _write_text(attempt_dir / relative, text)

        return InvocationResult(
            invocation_id=request.invocation_id,
            outcome=self.script.outcome,
            candidate=CandidateRef(
                base_ref=f"base:{request.task_revision}:{request.spec_digest[:18]}",
                change_summary=f"fake change for {request.task_id} revision {request.task_revision}",
                produced_paths=sorted(self.script.write_plan),
            ),
            review=self.script.review,
            agent_turns=self.script.agent_turns,
            provider_billed_tokens=None,
            reported_cost=None,
            limitations=list(self.script.limitations),
            raw_ref=f"fake://invocation/{request.invocation_id}",
            error_code=self.script.error_code,
            error_message=self.script.error_message,
        )

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self.cancelled.append(invocation_id)
        return CancellationReceipt(
            invocation_id=invocation_id,
            status="confirmed_stopped",
            detail="fake driver has no external process to stop",
        )

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        self.reconciled.append(invocation_id)
        return ReconcileResult(
            invocation_id=invocation_id,
            outcome=ReconcileOutcome.UNKNOWN,
            detail="fake driver cannot prove whether work happened; staying unknown",
        )


class ProcessGuard:
    """Records a process identity for an attempt.

    The controller stores PID *plus* start time *plus* a managed-instance marker,
    because a bare PID can be reused by an unrelated process (plan 9.3).
    """

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id

    def identity(self) -> tuple[int, str, str]:
        pid = os.getpid()
        started_at = utc_now()
        return pid, started_at, f"{self.owner_id}:{pid}:{started_at}"
