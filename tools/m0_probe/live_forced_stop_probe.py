"""Live forced-stop trial: prove the Driver can stop a real, running, managed tool process.

This exists for exactly one question, and only runs with explicit user authorization:

    when the real DSH agent is running a tool process inside this invocation's managed
    boundary, can the *existing* controller/driver stop path confirm that the local managed
    process has ended, without dispatching anything new and without accepting a late success?

Design rules taken from the trial specification:

* **The probe never starts the helper itself.** DSH starts it through its own tool call;
  otherwise the evidence would be about the probe, not about the harness.
* **Ownership is proven by the OS, not by the helper's self-report.** ``IsProcessInJob`` is
  called with *this* invocation's job handle, and the parent chain is walked to connect the
  helper to the client the driver started.
* **Tool status is supporting evidence only.** ACP ``in_progress`` is the agent's report of
  its own tool state; it is recorded when seen and never treated as a process fact. The
  trigger is the helper's READY file plus OS liveness plus job membership.
* **One authorization, one submission.** The attempt is written to a durable record *before*
  dispatch; a failure, timeout or unknown result consumes it. There is no retry path.
* Monotonic clocks for every deadline; bounded waits everywhere; no unbounded sleep.

Usage:

    python tools/m0_probe/live_forced_stop_probe.py --prepare-only     # no model traffic
    python tools/m0_probe/live_forced_stop_probe.py --authorized       # the one real trial
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.contracts import (  # noqa: E402
    AcceptanceCriterion,
    AgentBinding,
    BudgetRequest,
    CheckDef,
    InvocationRequest,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
)
from hflow.controller import Controller  # noqa: E402
from hflow.drivers.acpx_dsh import AcpxDshDriver  # noqa: E402
from hflow.drivers.winjob import parent_pid, process_gone  # noqa: E402
from hflow.paths import default_data_dir  # noqa: E402
from hflow.store import Store  # noqa: E402
from hflow.verify import CheckRunners  # noqa: E402

PROBE_ROOT = REPO_ROOT / ".probe" / "live-forced-stop"
ATTEMPT_RECORD = PROBE_ROOT / "attempts.json"
HELPER = Path(__file__).resolve().parent / "fixtures" / "stoppable_helper.py"

# Trial parameters (probe settings, not system guarantees).
WAIT_FOR_HELPER_SECONDS = 180.0
HELPER_LIFETIME_SECONDS = 60.0
CONFIRM_STOP_SECONDS = 10.0
POST_EXIT_OBSERVE_SECONDS = 2.0
CREDENTIAL_REF = "DEEPSEEK_API_KEY"


@dataclass
class TrialEvidence:
    """Everything the report needs, in one place."""

    authorization: str = ""
    baseline_sha: str = ""
    started_at: str = ""
    helper_ready: dict = field(default_factory=dict)
    helper_pid: int | None = None
    helper_in_this_job: bool | None = None
    helper_parent_chain: list[int] = field(default_factory=list)
    client_pid: int | None = None
    job_kind: str = ""
    tool_call_observed: bool = False
    dispatched_observed: bool = False
    cancel_requested_at: str = ""
    cancel_receipt: dict = field(default_factory=dict)
    stop_seconds: float | None = None
    helper_gone: bool | None = None
    client_gone: bool | None = None
    boundary_active_after: int | None = None
    heartbeat_after_exit: str = ""
    run_outcome: dict = field(default_factory=dict)
    late_acceptance: bool | None = None
    extra_dispatches: int | None = None
    result: str = "INCONCLUSIVE"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "authorization": self.authorization,
            "baseline_sha": self.baseline_sha,
            "started_at": self.started_at,
            "helper_ready": self.helper_ready,
            "helper_pid": self.helper_pid,
            "helper_in_this_job": self.helper_in_this_job,
            "helper_parent_chain": self.helper_parent_chain,
            "client_pid": self.client_pid,
            "boundary_kind": self.job_kind,
            "tool_call_observed": self.tool_call_observed,
            "dispatched_observed": self.dispatched_observed,
            "cancel_requested_at": self.cancel_requested_at,
            "cancel_receipt": self.cancel_receipt,
            "stop_seconds": self.stop_seconds,
            "helper_gone": self.helper_gone,
            "client_gone": self.client_gone,
            "boundary_active_after": self.boundary_active_after,
            "heartbeat_after_exit": self.heartbeat_after_exit,
            "run_outcome": self.run_outcome,
            "late_acceptance": self.late_acceptance,
            "extra_dispatches": self.extra_dispatches,
            "result": self.result,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# authorization gate and durable attempt record
# --------------------------------------------------------------------------


def read_attempts() -> dict:
    if not ATTEMPT_RECORD.exists():
        return {"attempts": []}
    return json.loads(ATTEMPT_RECORD.read_text(encoding="utf-8"))


def record_attempt(entry: dict) -> None:
    state = read_attempts()
    state["attempts"].append(entry)
    ATTEMPT_RECORD.parent.mkdir(parents=True, exist_ok=True)
    ATTEMPT_RECORD.write_text(json.dumps(state, indent=2), encoding="utf-8")


def authorization_present(argv: list[str]) -> tuple[bool, str]:
    """A real trial needs the explicit flag *and* a non-empty authorization note."""
    if "--authorized" not in argv:
        return False, "no --authorized flag"
    note = ""
    for index, item in enumerate(argv):
        if item == "--authorization-note" and index + 1 < len(argv):
            note = argv[index + 1]
    if not note.strip():
        return False, "--authorized without an --authorization-note"
    return True, note.strip()


# --------------------------------------------------------------------------
# workspace preparation (zero model traffic)
# --------------------------------------------------------------------------


def prepare_workspace() -> tuple[Path, Path]:
    """Lay out the one-shot workspace and the isolated probe home. No model involved."""
    work = PROBE_ROOT / "workspace"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "helper_output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(HELPER, work / "stoppable_helper.py")
    home = PROBE_ROOT / "home"
    home.mkdir(parents=True, exist_ok=True)
    return work, home


def task_spec(helper_name: str, revision: int = 1) -> TaskSpec:
    """The one task this trial sends.

    ``revision`` is bumped per trial because the controller's idempotency rule is *correct*:
    the same TaskSpec never buys a second worker turn. A new revision is a genuinely new
    trial, which is exactly what a fresh authorization produces.
    """
    return TaskSpec(
        task_id="T-live-forced-stop",
        revision=revision,
        goal=(
            f"Run `{sys.executable} {helper_name} --nonce-file helper_ready.json "
            f"--lifetime {int(HELPER_LIFETIME_SECONDS)}` in the foreground with your shell "
            "tool and wait for it to finish. Do not edit any file, do not create other "
            "files, do not research anything, and do not start any other process."
        ),
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="helper ran in the foreground", check_ids=["noop"])
        ],
        scope=Scope(write_allow=["helper_ready.json"], write_deny=[".hflow/**"]),
        risk="standard",
        reuse=ReuseDecision(status=ReuseStatus.EXEMPT, reason="probe-only trial, no component choice"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
    )


def project_config() -> ProjectConfig:
    return ProjectConfig(
        project_id="hflow-live-probe",
        checks=[CheckDef(id="noop", kind="fake")],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        review_required=False,
    )


# --------------------------------------------------------------------------
# credential injection (unchanged, narrowly scoped)
# --------------------------------------------------------------------------


def resolve_managed_credential(ref: str) -> tuple[str | None, str]:
    """Read one named credential in memory, exactly as the M0 probe did.

    Reused rather than reinvented: same reference, same source, same boundaries. The value
    is never printed, logged, or written.
    """
    from tools.m0_probe.run_probe import resolve_managed_credential as resolve  # type: ignore

    return resolve(ref)


# --------------------------------------------------------------------------
# the trial
# --------------------------------------------------------------------------


def run_trial(
    evidence: TrialEvidence,
    work: Path,
    home: Path,
    deadline_seconds: int,
    *,
    offline_client: bool = False,
) -> TrialEvidence:
    data_dir = PROBE_ROOT / "data"
    store = Store(data_dir / "hflow.sqlite")
    binding = AgentBinding(harness="dsh", driver="acpx-dsh")
    if offline_client:
        # Self-check wiring: the same probe logic against the test-only stand-in client, so
        # the evidence chain can be exercised without a model. It is not a live trial and
        # records itself as INCONCLUSIVE by construction.
        driver = AcpxDshDriver(
            data_dir=data_dir,
            acpx_cli=REPO_ROOT / "tests" / "fixtures" / "fake_acpx_client.py",
            python_executable=sys.executable,
            completion_timeout_seconds=300,
            agent_argv_override=[
                sys.executable,
                "-u",
                str(REPO_ROOT / "tools" / "m0_probe" / "fixtures" / "stub_helper_agent.py"),
                "helper",
                "--helper",
                str(work / "stoppable_helper.py"),
                "--lifetime",
                "30",
            ],
        )
        driver._agent_argv = lambda: list(driver.agent_argv_override or [])  # type: ignore[method-assign]
        driver.extra_env["STUB_SCRATCH_DIR"] = str(work / "stub-scratch")
    else:
        driver = AcpxDshDriver(data_dir=data_dir, dsh_home=home, completion_timeout_seconds=300)
    controller = Controller(
        store,
        driver,
        controller_build="live-forced-stop-probe",
        runners=CheckRunners({"fake": _NoopRunner()}),
        data_dir=data_dir,
    )
    spec = task_spec("stoppable_helper.py", revision=_next_revision(store))
    project = project_config()
    request = RunRequest(
        task=spec,
        project=project,
        project_root=work,
        workspace_root=work,
    )

    outcome_box: dict = {}
    stop_flag = threading.Event()

    def drive() -> None:
        try:
            outcome = controller.run_task(request)
            outcome_box["outcome"] = {
                "run_id": outcome.run_id,
                "task_state": outcome.task_state.value,
                "phase": outcome.phase.value if outcome.phase else None,
                "block_code": outcome.block_code.value if outcome.block_code else None,
                "block_reason": outcome.block_reason,
                "receipt": bool(outcome.receipt),
            }
            outcome_box["run_id"] = outcome.run_id
        except BaseException as exc:  # noqa: BLE001 - the trial must record, not crash
            outcome_box["error"] = repr(exc)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()

    # Wait for the invocation handle to exist so we can inspect the managed boundary.
    handle = None
    started = time.monotonic()
    while time.monotonic() - started < 30.0:
        if driver._handles:
            handle = next(iter(driver._handles.values()))
            break
        if "error" in outcome_box:
            evidence.notes.append(f"driver never started: {outcome_box['error']}")
            evidence.result = "INCONCLUSIVE"
            return evidence
        time.sleep(0.05)
    if handle is None:
        evidence.notes.append("no invocation handle appeared within 30s")
        evidence.result = "INCONCLUSIVE"
        return evidence

    evidence.client_pid = handle.pid
    evidence.job_kind = handle.boundary_kind
    boundary = driver._boundaries[handle.invocation_id]

    # --- 1. wait for the helper's READY, which DSH must have produced by running it
    ready_path = work / "helper_ready.json"
    ready_deadline = time.monotonic() + WAIT_FOR_HELPER_SECONDS
    ready: dict | None = None
    while time.monotonic() < ready_deadline:
        if ready_path.exists():
            try:
                candidate = json.loads(ready_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict) and candidate.get("pid"):
                ready = candidate
                break
        if handle.dispatched and worker.is_alive() is False:
            break
        time.sleep(0.1)

    if ready is None:
        evidence.notes.append(
            f"helper READY not observed within {WAIT_FOR_HELPER_SECONDS:.0f}s "
            f"(dispatched={handle.dispatched})"
        )
        evidence.result = "INCONCLUSIVE"
        _safe_cleanup(driver, handle, boundary)
        return evidence

    evidence.helper_ready = ready
    evidence.helper_pid = int(ready["pid"])
    evidence.dispatched_observed = bool(handle.dispatched)

    # --- 2. OS-level ownership: is this exact PID inside THIS invocation's job?
    evidence.helper_in_this_job = boundary.contains(evidence.helper_pid)
    chain: list[int] = []
    cursor: int | None = evidence.helper_pid
    for _ in range(5):
        if cursor is None or cursor <= 0:
            break
        parent = parent_pid(cursor)
        if parent is None or parent <= 0:
            break
        chain.append(parent)
        cursor = parent
    evidence.helper_parent_chain = chain
    if chain and chain[-1] == evidence.client_pid:
        evidence.notes.append("parent chain reaches the managed client process")
    elif chain:
        evidence.notes.append(
            f"parent chain recorded (first 5 links); managed client pid={evidence.client_pid} "
            "not reached within the cap - job membership is the ownership proof"
        )

    alive_before = not process_gone(evidence.helper_pid, 0.0)

    # --- 3. supporting evidence only: the agent's own tool-call projection
    for event in driver.events(handle.invocation_id):
        if event.method in {"tool_call", "tool_call_update"} and "in_progress" in event.message:
            evidence.tool_call_observed = True

    if ready.get("nonce") is None:
        evidence.notes.append("helper READY carried no nonce")
    if not alive_before:
        evidence.notes.append("helper had already exited before the stop request")
        evidence.result = "INCONCLUSIVE"
        _safe_cleanup(driver, handle, boundary)
        return evidence
    if evidence.helper_in_this_job is not True:
        evidence.notes.append(
            f"helper job membership not proven (IsProcessInJob={evidence.helper_in_this_job})"
        )

    # --- 4. stop through the existing controller path: intent first, then the driver
    evidence.cancel_requested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Capture the boundary state *before* the stop: a confirmed stop releases the boundary,
    # after which the job handle is gone and its live count is no longer readable.
    active_before_stop = boundary.active_processes()
    stop_started = time.monotonic()
    try:
        receipt = controller.cancel(handle.run_id)
        evidence.cancel_receipt = receipt.model_dump(mode="json")
    except BaseException as exc:  # noqa: BLE001
        evidence.cancel_receipt = {"error": repr(exc)}
        evidence.notes.append(f"controller.cancel raised: {exc!r}")

    # --- 5. confirm: bounded wait for the helper and the client, then the boundary
    helper_gone = _wait_gone(evidence.helper_pid, CONFIRM_STOP_SECONDS)
    client_gone = _wait_gone(evidence.client_pid, 2.0)
    evidence.stop_seconds = round(time.monotonic() - stop_started, 2)
    evidence.helper_gone = helper_gone
    evidence.client_gone = client_gone
    evidence.boundary_active_after = boundary.active_processes()
    if active_before_stop is not None:
        evidence.notes.append(f"boundary active processes before the stop: {active_before_stop}")

    # --- 6. a short post-exit observation: heartbeats must not resume
    if helper_gone:
        before = _read_heartbeat(ready)
        time.sleep(min(POST_EXIT_OBSERVE_SECONDS, 2.0))
        after = _read_heartbeat(ready)
        evidence.heartbeat_after_exit = "stopped" if before == after else "changed"

    stop_flag.set()
    worker.join(timeout=30.0)
    evidence.run_outcome = outcome_box.get("outcome", {"error": outcome_box.get("error")})
    if "error" in evidence.run_outcome:
        notes = evidence.run_outcome["error"]
        if "is not live" in notes:
            # Expected race: the stop finalized the attempt before the driver could report a
            # result. That is the cancellation working, not a driver failure.
            evidence.notes.append("driver result arrived after the stop; the attempt was already terminal")
        else:
            evidence.notes.append(f"drive thread error: {notes}")

    # --- 7. no extra work, no late acceptance
    evidence.extra_dispatches = len(store.attempts_for(handle.run_id))
    row = store.get_run(handle.run_id)
    evidence.late_acceptance = row["task_state"] == "ACCEPTED"

    evidence.result = _classification(evidence, alive_before)
    _safe_cleanup(driver, handle, boundary)
    store.close()
    return evidence


class _NoopRunner:
    """Verification is out of scope for this trial; the noop check always passes."""

    def run(self, check, cwd, timeout_seconds):  # noqa: ANN001, ANN201
        from hflow.contracts import EvidenceStatus
        from hflow.verify import CheckOutcome

        return CheckOutcome(EvidenceStatus.PASSED, exit_code=0, detail="noop")


def _wait_gone(pid: int | None, timeout: float) -> bool:
    if pid is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_gone(pid, 0.0):
            return True
        time.sleep(0.05)
    return process_gone(pid, 0.2)


def _read_heartbeat(ready: dict) -> str:
    path = Path(ready.get("heartbeat_file", ""))
    if not path or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _classification(evidence: TrialEvidence, alive_before: bool) -> str:
    receipt_status = str(evidence.cancel_receipt.get("status", ""))
    mechanism = str(evidence.cancel_receipt.get("mechanism", ""))
    if (
        alive_before
        and evidence.helper_in_this_job is True
        and receipt_status == "confirmed_stopped"
        and mechanism == "forced"
        and evidence.helper_gone is True
        and evidence.late_acceptance is False
    ):
        return "PASS"
    if evidence.helper_gone is False:
        return "FAIL"
    return "INCONCLUSIVE"


def _next_revision(store: Store) -> int:
    """One past the highest revision this probe has used, so each trial is its own task.

    The controller would otherwise (correctly) return the previous trial's run and never
    dispatch again.
    """
    highest = 0
    for row in store.list_runs(limit=100):
        try:
            spec = json.loads(row["task_spec_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if spec.get("task_id") == "T-live-forced-stop":
            highest = max(highest, int(spec.get("revision", 0)))
    return highest + 1


def _head_sha() -> str:
    """Baseline commit recorded with the evidence, without shelling out to a string form."""
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603,S607 - fixed read-only git query
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _safe_cleanup(driver: AcpxDshDriver, handle, boundary) -> None:  # noqa: ANN001
    """Bounded cleanup of *this invocation's* managed processes only."""
    try:
        if driver._processes.get(handle.invocation_id) is not None:
            driver.cancel_handle(handle)
    except Exception:  # noqa: BLE001
        pass
    try:
        boundary.terminate()
        boundary.wait_empty(CONFIRM_STOP_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    try:
        driver.release(handle.invocation_id)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true", help="lay out the workspace, no model")
    parser.add_argument("--authorized", action="store_true", help="user authorization for one live task")
    parser.add_argument("--authorization-note", default="", help="the authorization text/quote")
    parser.add_argument(
        "--offline-self-check",
        action="store_true",
        help="exercise the probe's own evidence chain via the test-only stand-in client",
    )
    parser.add_argument("--deadline-seconds", type=int, default=240)
    parser.add_argument("--out", default=str(PROBE_ROOT / "evidence.json"))
    args = parser.parse_args(argv)

    work, home = prepare_workspace()
    print(f"workspace: {work}")
    print(f"probe home: {home}")

    if args.prepare_only:
        print("prepared only: no model task was dispatched")
        return 0

    if args.offline_self_check:
        # No model, no credential, no authorization needed: this only exercises the probe's
        # own logic against the test-only stand-in client.
        evidence = TrialEvidence(
            authorization="offline self-check (no model task)",
            baseline_sha=_head_sha(),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        evidence = run_trial(evidence, work, home, args.deadline_seconds, offline_client=True)
        # A stand-in client replaces DSH, so this can never be a live PASS whatever the
        # mechanics did. Saying otherwise would be exactly the overclaim this trial avoids.
        if evidence.result == "PASS":
            evidence.notes.append(
                "mechanics passed, but the stand-in client replaced DSH: not live evidence"
            )
        evidence.result = "INCONCLUSIVE"
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence.as_dict(), indent=2), encoding="utf-8")
        print(json.dumps(evidence.as_dict(), indent=2))
        print("\nself-check only: a trial that uses the stand-in client is never a live PASS")
        return 0

    allowed, note = authorization_present(argv)
    state = read_attempts()
    used = len(state.get("attempts", []))
    if not allowed:
        print(f"refusing to run a live trial: {note}", file=sys.stderr)
        print(f"attempts already recorded: {used}", file=sys.stderr)
        return 4
    if used >= 1:
        print(f"refusing: this probe already consumed its single authorized attempt ({used})", file=sys.stderr)
        return 4

    value, status = resolve_managed_credential(CREDENTIAL_REF)
    if not value:
        print(f"credentials unavailable: {status}", file=sys.stderr)
        print("recording the attempt as consumed; no retry will be made", file=sys.stderr)
        record_attempt({"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "authorization": note, "result": "INCONCLUSIVE", "reason": status})
        return 4

    evidence = TrialEvidence(
        authorization=note,
        baseline_sha=_head_sha(),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    # Durable intent + budget record *before* dispatch: a failed or unknown run still counts.
    record_attempt(
        {
            "started_at": evidence.started_at,
            "authorization": note,
            "baseline_sha": evidence.baseline_sha,
            "result": "DISPATCHED_PENDING",
        }
    )
    os.environ[CREDENTIAL_REF] = value
    del value

    try:
        evidence = run_trial(evidence, work, home, args.deadline_seconds)
    finally:
        os.environ.pop(CREDENTIAL_REF, None)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence.as_dict(), indent=2), encoding="utf-8")
    record_attempt(
        {
            "started_at": evidence.started_at,
            "authorization": note,
            "baseline_sha": evidence.baseline_sha,
            "result": evidence.result,
            "evidence": str(out_path),
        }
    )
    print(json.dumps(evidence.as_dict(), indent=2))
    return 0 if evidence.result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
