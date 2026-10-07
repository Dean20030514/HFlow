"""Verify the three re-check findings against the current implementation. Zero model calls."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from hflow.artifacts import BoundedTextSink  # noqa: E402
from hflow.contracts import CheckDef  # noqa: E402
from hflow.verify import CommandCheckRunner  # noqa: E402
from tests.test_driver_acpx_dsh import DriverHarness  # noqa: E402

# The stub agent needs no credential, but a real launch with none visible is refused
# (no_credential_source); a stand-in that is obviously not a key, never a real one.
os.environ["DEEPSEEK_API_KEY"] = "hflow-verify-stand-in-not-a-credential"

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
failures: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# --- P1a: a failing sink write must not produce passed ------------------------
original_write = BoundedTextSink.write


def failing_write(self, chunk: bytes) -> None:
    raise OSError("simulated full disk")


BoundedTextSink.write = failing_write
try:
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: work / "art")
    outcome = runner.run(
        CheckDef(
            id="unit",
            kind="command",
            argv=[sys.executable, "-c", "print('a line that cannot be retained')"],
            timeout_seconds=60,
        ),
        work,
        60,
    )
finally:
    BoundedTextSink.write = original_write

print("-- P1a: sink write failure --")
print(f"status={outcome.status.value} exit_reason={outcome.exit_reason} exit_code={outcome.exit_code}")
print(f"stdout failed={outcome.artifacts['stdout']['failed']} retained={outcome.artifacts['stdout']['retained_bytes']}")
report("P1a not passed", outcome.status.value != "passed", outcome.status.value)
report("P1a reason", outcome.exit_reason == "output_capture_error", outcome.exit_reason)
report("P1a exit code kept", outcome.exit_code == 0, str(outcome.exit_code))

# --- P1b: what HFlow keeps is bounded; the client's own file is measured ---------
print("-- P1b: worker retention --")
budget = 256 * 1024
harness = DriverHarness(work / "chatty", "chatty", max_raw_log_bytes=budget)
handle, _request = harness.start()
try:
    result = harness.driver.collect(handle)
    invocation = (work / "chatty" / "data" / "invocations" / handle.invocation_id)
    raw = (invocation / "stdout.ndjson").stat().st_size
    events = (invocation / "events.ndjson").stat().st_size
    stderr = (invocation / "stderr.txt").stat().st_size
    peak = harness.driver._peak_raw_bytes[handle.invocation_id]
    capture = harness.driver._stdout_captures[handle.invocation_id]
    print(
        f"budget={budget} events={events} stderr={stderr} "
        f"client_file={raw} measured_peak={peak} retained_total={capture.total_bytes}"
    )
    print(
        f"retained_events={len(harness.driver._events[handle.invocation_id])} "
        f"capped={harness.driver._events_capped[handle.invocation_id]}"
    )
    print(f"outcome={result.outcome.value} error_code={result.error_code}")
    report("P1b retained log within budget", events <= harness.driver.protocol_share_bytes)
    report("P1b stderr within its share", stderr <= harness.driver.stderr_share_bytes)
    report("P1b event container bounded", harness.driver._events_capped[handle.invocation_id] is True)
    report("P1b overflow named", result.error_code == "output_limit_exceeded", str(result.error_code))
    report(
        "P1b client file is measured, not claimed bounded",
        peak == raw and capture.total_bytes == raw and raw > budget,
        f"raw={raw} peak={peak} retained_total={capture.total_bytes}",
    )
    report(
        "P1b the 17 MiB is not reported as retained",
        capture.retained_bytes == harness.driver.protocol_share_bytes and capture.truncated is True,
        f"retained={capture.retained_bytes}",
    )
finally:
    harness.driver.release(handle.invocation_id)

# --- P1c: only the last line crossing the budget -------------------------------
print("-- P1c: last line crossing the budget --")
harness = DriverHarness(work / "cooperative", "cooperative", max_raw_log_bytes=1024)
handle, _request = harness.start()
try:
    result = harness.driver.collect(handle)
    print(
        f"overflow={harness.driver._overflow[handle.invocation_id]} "
        f"outcome={result.outcome.value} error_code={result.error_code} "
        f"events_bytes={(work / 'cooperative' / 'data' / 'invocations' / handle.invocation_id / 'events.ndjson').stat().st_size}"
    )
    report("P1c overflow detected", harness.driver._overflow[handle.invocation_id] is True)
    report("P1c not completed", result.outcome.value != "completed", result.outcome.value)
finally:
    harness.driver.release(handle.invocation_id)

print()
print(f"re-check verification: {len(failures)} failure(s)" + (f" -> {failures}" if failures else ""))
raise SystemExit(1 if failures else 0)
