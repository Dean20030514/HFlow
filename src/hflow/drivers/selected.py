"""Selection of the one production Driver, plus the M0 local capability record.

Status of this file, stated plainly: **no production driver is implemented yet.**
M0 could not be executed in this environment, because

* ``acpx`` is not installed (no global binary, no npm package), and
* the local DSH home has profiles ``headless`` and ``web`` only - there is no
  ``acp`` profile, so ``dsh --profile acp`` has nothing to boot.

The ADR (``docs/adr/0001-transport.md``) records the decision, and this module
refuses loudly instead of pretending a real harness is wired up. Nothing here
contacted a model provider or changed global DSH configuration.
"""

from __future__ import annotations

import platform
from pathlib import Path

from ..contracts import (
    AgentBinding,
    CapabilityReport,
    CapabilityState,
    CancellationReceipt,
    InvocationOutcome,
    InvocationRequest,
    InvocationResult,
    ReconcileOutcome,
    ReconcileResult,
    RefusalCode,
    RefusedError,
)

SELECTED_DRIVER_ID = "m0-unselected"
ACPX_PRESENT_IN_THIS_ENV = False
LOCAL_DSH_PROFILES = ("headless", "web")


def local_probe(executable: str = "dsh", acpx_present: bool = ACPX_PRESENT_IN_THIS_ENV) -> CapabilityReport:
    """Static, zero-model capability record for this machine.

    ``documented`` means upstream documentation describes it. ``unknown`` means it
    was never executed here. Nothing in this report was live-tested.
    """
    capabilities = {
        "fresh_session": CapabilityState.DOCUMENTED,
        "session_resume": CapabilityState.DOCUMENTED,
        "session_list": CapabilityState.DOCUMENTED,
        "session_load": CapabilityState.UNSUPPORTED,
        "cancel": CapabilityState.DOCUMENTED,
        "model_selection": CapabilityState.DOCUMENTED,
        "permission_requests": CapabilityState.DOCUMENTED,
        "structured_output": CapabilityState.UNKNOWN,
        "native_subagents": CapabilityState.UNKNOWN,
        "billing_usage": CapabilityState.UNKNOWN,
        "readonly_enforcement": CapabilityState.UNKNOWN,
    }
    notes = [
        "probe is static: no model was called and no DSH profile was booted",
        f"local DSH profiles found: {', '.join(LOCAL_DSH_PROFILES)} (no 'acp' profile)",
        f"acpx present: {acpx_present}",
        "every capability stays 'documented/unknown' until a live M0 check runs",
    ]
    return CapabilityReport(
        driver_id=SELECTED_DRIVER_ID,
        driver_version="0.0.1",
        harness="dsh",
        harness_version=None,
        os=f"{platform.system()}-{platform.release()}",
        arch=platform.machine(),
        probe_only=True,
        live_tested=False,
        capabilities=capabilities,
        notes=notes,
    )


class UnselectedDriver:
    """Placeholder that refuses to run. Present so misconfiguration fails loudly."""

    driver_id = SELECTED_DRIVER_ID

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def probe(self, binding: AgentBinding) -> CapabilityReport:
        return local_probe()

    def _refuse(self) -> None:
        raise RefusedError(RefusalCode.NOT_IMPLEMENTED, self.reason)

    def start(self, request: InvocationRequest) -> InvocationResult:
        self._refuse()

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        self._refuse()

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        self._refuse()


def default_refusal_reason() -> str:
    return (
        "no production driver is selected or implemented in this build: M0 could not run "
        "because acpx is not installed and this machine has no DSH 'acp' profile, and the "
        "headless fallback is intentionally not implemented in the same change (one "
        "production transport at a time). Use the offline fake driver for controller work."
    )


def build_driver(binding: AgentBinding, *, project_root: Path, offline: bool = False) -> object:
    """Resolve a binding to a driver instance.

    ``offline=True`` is the only path this build supports end to end.
    """
    from .fake import FakeDriver  # local import keeps the fake out of the real path

    if binding.driver in {"fake", "fake-offline"}:
        return FakeDriver(project_root)
    if offline:
        raise RefusedError(
            RefusalCode.NOT_IMPLEMENTED,
            f"binding {binding.driver!r} cannot run offline; there is no {binding.driver!r} driver "
            "in this build",
        )
    raise RefusedError(RefusalCode.NOT_IMPLEMENTED, default_refusal_reason())
