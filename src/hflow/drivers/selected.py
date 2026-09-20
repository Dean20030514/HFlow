"""Selection of the one production Driver.

M0 selected `acpx 0.17.1 -> official DSH ACP` with bounded live evidence
(``docs/adr/0001-transport.md``, ``docs/m0-results.md``). This module is now the *single*
place that maps a logical binding to a concrete driver object, so nothing else has to know
which transport is production.

Two rules survive from the M0 round:

* one production transport. There is no second (headless) implementation to fall back to at
  runtime, and no silent fallback of any kind - an unknown driver name refuses;
* a driver that is not configured refuses loudly instead of degrading to something cheaper.
"""

from __future__ import annotations

import platform
from pathlib import Path

from ..contracts import (
    AgentBinding,
    CapabilityReport,
    CapabilityState,
    CancellationReceipt,
    InvocationRequest,
    InvocationResult,
    ReconcileResult,
    RefusalCode,
    RefusedError,
)
from .acpx_dsh import DRIVER_ID as ACPX_DSH_DRIVER_ID
from .acpx_dsh import AcpxDshDriver

SELECTED_DRIVER_ID = ACPX_DSH_DRIVER_ID
#: Names a machine profile may use for the selected transport.
ACPX_DSH_ALIASES = {ACPX_DSH_DRIVER_ID, "acpx-dsh", "acpx", "dsh-acp"}


def default_refusal_reason() -> str:
    """Why an unconfigured driver refuses, in one sentence a operator can act on."""
    return (
        f"the selected transport is {SELECTED_DRIVER_ID}; pass a real binding (or --driver fake "
        "for offline work). No unattended production execution is approved yet: cooperative "
        "cancellation on this launch path is unverified."
    )


def local_probe(
    binding: AgentBinding | None = None,
    *,
    driver: AcpxDshDriver | None = None,
) -> CapabilityReport:
    """Static capability record for this machine. Sends no task and calls no model.

    With a driver instance the report is the driver's own probe (it knows its executable
    path and launch argv); without one, this returns the deliberately conservative record
    used before any driver is constructed.
    """
    if driver is not None and binding is not None:
        return driver.probe(binding)

    capabilities = {
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
    }
    return CapabilityReport(
        driver_id=SELECTED_DRIVER_ID,
        driver_version="0.1.0",
        harness="dsh",
        harness_version=None,
        os=f"{platform.system()}-{platform.release()}",
        arch=platform.machine(),
        probe_only=True,
        live_tested=False,
        capabilities=capabilities,
        notes=[
            "probe is static: no prompt, no session, no model request",
            "transport selected by M0: acpx 0.17.1 -> official DSH ACP",
            "cooperative cancellation is unsupported on the one-shot exec path",
        ],
    )


def build_driver(
    binding: AgentBinding,
    *,
    data_dir: Path,
    dsh_home: Path | None = None,
) -> object:
    """Resolve a binding to the driver instance. Refuses anything not selected in M0."""
    name = binding.driver.strip().lower()
    if name in {"fake", "fake-offline"}:
        from .fake import FakeDriver

        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        return FakeDriver(root)
    if name in ACPX_DSH_ALIASES:
        return AcpxDshDriver(data_dir=data_dir, dsh_home=dsh_home)
    raise RefusedError(
        RefusalCode.NOT_IMPLEMENTED,
        f"driver {binding.driver!r} is not implemented. This build has exactly one production "
        f"transport ({SELECTED_DRIVER_ID}) plus the offline fake; there is no runtime fallback.",
    )


class UnselectedDriver:
    """Refuses to run. Kept so a misconfiguration fails loudly rather than silently."""

    driver_id = "unselected"

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
