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
    LaunchConfig,
    ReconcileResult,
    RefusalCode,
    RefusedError,
)
from .acpx_dsh import DRIVER_ID as ACPX_DSH_DRIVER_ID
from .acpx_dsh import AcpxDshDriver

SELECTED_DRIVER_ID = ACPX_DSH_DRIVER_ID
#: Names a machine profile may use for the selected transport.
ACPX_DSH_ALIASES = {ACPX_DSH_DRIVER_ID, "acpx-dsh", "acpx", "dsh-acp"}
#: The offline driver, which never reaches a model.
FAKE_DRIVER_ID = "fake"
FAKE_ALIASES = {FAKE_DRIVER_ID, "fake-offline"}
#: The one harness this build implements. Both drivers serve it: the production transport
#: launches it, and the offline fake stands in for it.
HARNESS_DSH = "dsh"
#: Which harness each implemented driver actually launches. A binding whose declared harness is
#: not in its driver's set is refused: a profile that says `codex` while naming a DSH driver
#: would otherwise be recorded as a Codex run whose every process was DSH.
DRIVER_HARNESSES: dict[str, frozenset[str]] = {
    SELECTED_DRIVER_ID: frozenset({HARNESS_DSH}),
    FAKE_DRIVER_ID: frozenset({HARNESS_DSH}),
}


def driver_id_for_name(driver_name: str) -> str:
    """Map a driver *name* to the driver id it selects. Pure name resolution, no binding.

    Separate from :func:`resolve_driver_id` because it is also needed where no binding exists
    yet - a command line that named only a driver, or a conflict check between two sources.
    """
    name = driver_name.strip().lower()
    if name in FAKE_ALIASES:
        return FAKE_DRIVER_ID
    if name in ACPX_DSH_ALIASES:
        return SELECTED_DRIVER_ID
    raise RefusedError(
        RefusalCode.NOT_IMPLEMENTED,
        f"driver {driver_name!r} is not implemented. This build has exactly one production "
        f"transport ({SELECTED_DRIVER_ID}) plus the offline fake; there is no runtime fallback.",
    )


def resolve_driver_id(binding: AgentBinding) -> str:
    """Map a binding to the driver id that will actually be constructed.

    Both halves of the binding are checked, not just the driver name: the declared harness has
    to be one this driver implements. Pure - it constructs nothing and touches no file - so
    ``prepare`` and ``doctor`` can report exactly the resolution a run would perform, including
    the refusal, without side effects.
    """
    driver_id = driver_id_for_name(binding.driver)
    declared = binding.harness.strip().lower()
    allowed = DRIVER_HARNESSES[driver_id]
    if declared not in allowed:
        raise RefusedError(
            RefusalCode.NOT_IMPLEMENTED,
            f"harness {binding.harness!r} is not served by driver {binding.driver!r}, which "
            f"launches {', '.join(sorted(allowed))}. This build implements exactly one harness "
            f"({HARNESS_DSH}) plus the offline fake; naming a different harness next to an "
            "implemented driver would record a run whose processes are all DSH. There is no "
            "driver for that harness here.",
        )
    return driver_id


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
    launch: LaunchConfig | None = None,
) -> object:
    """Resolve a binding to the driver instance. Refuses anything not selected in M0.

    The name resolution is ``resolve_driver_id``'s, not a second copy of it, so a name that
    ``prepare`` accepted cannot fail here for a different reason. ``launch`` is the launch that
    was resolved *before* the approval; when given, the driver consumes it instead of reading
    the environment again, so what was approved is what runs.
    """
    resolved = resolve_driver_id(binding)
    if resolved == FAKE_DRIVER_ID:
        from .fake import FakeDriver

        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        return FakeDriver(root)
    return AcpxDshDriver(data_dir=data_dir, dsh_home=dsh_home, launch=launch)


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
