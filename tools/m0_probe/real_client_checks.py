"""Zero-model checks of the *real* acpx client through the production launch path.

The live forced-stop trial failed because the client was started with the wrong interpreter,
and no offline test caught it: the test stand-in is a Python script, so it never exercised
Node entry-point launching. These two checks close that gap without a model:

``version``  runs the installed ``acpx`` (Node, ``dist/cli.js``) with a read-only metadata
             argument through the driver's own argv/boundary/drain code;
``mock``     runs the real acpx client against the existing project mock ACP agent, so the
             full one-shot ``exec`` path - structured argv, task stdin, Job, event drain -
             is exercised with a test double instead of DSH.

Neither check reaches the real DSH, reads a credential, or injects a provider key. "No
model calls" here is a property of *what is launched* (a metadata argument, or a mock
agent), not of a missing key.

Usage:
    python tools/m0_probe/real_client_checks.py version
    python tools/m0_probe/real_client_checks.py mock
    python tools/m0_probe/real_client_checks.py all
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.contracts import EventKind, InvocationOutcome, InvocationRequest  # noqa: E402
from hflow.drivers.acpx_dsh import AcpxDshDriver  # noqa: E402
from hflow.drivers.winjob import process_gone  # noqa: E402

PROBE_ROOT = REPO_ROOT / ".probe" / "real-client-checks"
INSTALLED_ACPX = REPO_ROOT / ".probe" / "acpx" / "node_modules" / "acpx" / "dist" / "cli.js"
MOCK_AGENT = Path(__file__).resolve().parent / "mock_acp_agent.py"


def resolve_node() -> str:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is not on PATH; the real acpx client cannot be launched")
    return node


def node_syntax_check(node: str, entry: Path) -> dict:
    """``node --check`` is a syntax check only; it is recorded as supporting evidence."""
    completed = subprocess.run(  # noqa: S603 - fixed read-only check
        [node, "--check", str(entry)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return {
        "argv": [node, "--check", str(entry)],
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip()[:400],
        "note": "syntax check only: it does not execute the script and does not replace the run below",
    }


def check_version(node: str) -> dict:
    """A: launch the real client with a read-only argument through the driver's launcher."""
    if not INSTALLED_ACPX.exists():
        return {"check": "version", "status": "BLOCKED", "reason": f"acpx not installed at {INSTALLED_ACPX}"}
    result: dict = {
        "check": "version",
        "installed_acpx": str(INSTALLED_ACPX),
        "node": node,
        "node_syntax_check": node_syntax_check(node, INSTALLED_ACPX),
    }
    PROBE_ROOT.mkdir(parents=True, exist_ok=True)
    driver = AcpxDshDriver(
        data_dir=PROBE_ROOT / "version-data",
        acpx_cli=INSTALLED_ACPX,
        python_executable=sys.executable,
    )
    probe = driver.readonly_client_check(["--version"], timeout_seconds=60)
    result.update(probe)
    reported = probe["stdout"].strip()
    result["reported_version"] = reported
    result["status"] = (
        "PASS"
        if probe["returncode"] == 0
        and reported
        and probe["process_gone"]
        and probe["boundary_empty"]
        and not probe["timed_out"]
        else "FAIL"
    )
    return result


def check_mock(node: str, *, deadline_seconds: int = 60) -> dict:
    """B: real acpx client, existing mock ACP agent, full one-shot exec path."""
    if not INSTALLED_ACPX.exists():
        return {"check": "mock", "status": "BLOCKED", "reason": f"acpx not installed at {INSTALLED_ACPX}"}
    run_dir = PROBE_ROOT / "mock"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    workspace = run_dir / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    wire_log = run_dir / "mock-wire.jsonl"

    agent_argv = [
        sys.executable,
        "-u",
        str(MOCK_AGENT),
        "--scenario",
        "echo-nonce",
        "--wire-log",
        str(wire_log),
        "--ready-file",
        str(run_dir / "mock-ready.txt"),
    ]
    data_dir = run_dir / "data"
    driver = AcpxDshDriver(
        data_dir=data_dir,
        acpx_cli=INSTALLED_ACPX,
        python_executable=sys.executable,
        agent_argv_override=agent_argv,
        completion_timeout_seconds=90,
    )
    # acpx reads its config from the OS home; give this run its own home so no user config
    # and no default agent route (codex) can be involved.
    home = (run_dir / "home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    driver.extra_env["USERPROFILE"] = str(home)
    driver.extra_env["HOME"] = str(home)

    nonce = f"nonce{int(time.time())}"
    request = InvocationRequest(
        invocation_id="I-real-mock-1",
        attempt_id="A-real-mock-1",
        run_id="R-real-mock-1",
        role="implementer",
        task_id="T-real-mock",
        task_revision=1,
        goal=f"reply with NONCE={nonce}",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(workspace),
        deadline_seconds=deadline_seconds,
        spec_digest="sha256:real-client-check",
        data_dir=str(data_dir),
    )

    handle = driver.start_handle(request)
    events = []
    for event in driver.observe(handle):
        events.append(event)
    result_invocation = driver.collect(handle)
    kinds = [event.kind.value for event in events]
    raw = "\n".join(driver.raw_lines(handle.invocation_id))
    agent_spawned = (run_dir / "mock-ready.txt").exists()
    helper = process_gone(handle.pid, 3.0)
    boundary_kind = handle.boundary_kind
    driver.release(handle.invocation_id)

    status = "PASS" if (
        result_invocation.outcome is InvocationOutcome.COMPLETED
        and EventKind.DISPATCHED.value in kinds
        and EventKind.COMPLETED.value in kinds
        and nonce in raw
        and agent_spawned
        and helper
        and driver.unparsed_line_count(handle.invocation_id) == 0
    ) else "FAIL"
    return {
        "check": "mock",
        "status": status,
        "client_argv": driver._client_argv(Path(data_dir), workspace, deadline_seconds),
        "outcome": result_invocation.outcome.value,
        "stop_reason_seen": EventKind.COMPLETED.value in kinds,
        "event_kinds": kinds,
        "nonce_echoed": nonce in raw,
        "mock_agent_launched": agent_spawned,
        "boundary_kind": boundary_kind,
        "client_process_gone": helper,
        "unparsed_lines": driver.unparsed_line_count(handle.invocation_id),
        "wire_log": str(wire_log) if wire_log.exists() else None,
        "data_dir": str(data_dir),
    }


def check_config(node: str, *, allow_writes: bool) -> dict:
    """C: the client must accept the config the driver writes, before any live dispatch.

    This is the check that would have prevented a wasted live submission: a client that rejects
    a config key exits during startup, so an invented key looks exactly like "the agent did
    nothing". Both permission modes are validated against the installed client, with no session
    and no prompt.
    """
    if not INSTALLED_ACPX.exists():
        return {"check": "config", "status": "BLOCKED", "reason": f"acpx not installed at {INSTALLED_ACPX}"}
    run_dir = PROBE_ROOT / f"config-{'writes' if allow_writes else 'reads'}"
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    home = (run_dir / "home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    driver = AcpxDshDriver(
        data_dir=run_dir / "data",
        acpx_cli=INSTALLED_ACPX,
        python_executable=sys.executable,
    )
    driver.allow_writes = allow_writes
    config_path = driver._write_config(run_dir)
    acpx_home = home / ".acpx"
    acpx_home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, acpx_home / "config.json")

    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
    completed = subprocess.run(  # noqa: S603 - argv is built here
        [node, str(INSTALLED_ACPX), "config", "show"],
        cwd=str(run_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    accepted = completed.returncode == 0
    detail = ""
    if accepted:
        try:
            shown = json.loads(completed.stdout)
            detail = f"defaultPermissions={shown.get('defaultPermissions')}"
        except json.JSONDecodeError:
            accepted = False
            detail = "config show did not print JSON"
    else:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1][:200]
    return {
        "check": f"config({'writes' if allow_writes else 'reads'})",
        "status": "PASS" if accepted else "FAIL",
        "allow_writes": allow_writes,
        "config_keys": sorted(json.loads(config_path.read_text(encoding="utf-8")).keys()),
        "returncode": completed.returncode,
        "detail": detail,
        "config_path": str(config_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=["version", "mock", "config", "all"], nargs="?", default="all")
    parser.add_argument("--out", default=str(PROBE_ROOT / "results.json"))
    args = parser.parse_args(argv)

    node = resolve_node()
    results: dict = {"node": node, "installed_acpx": str(INSTALLED_ACPX)}
    if args.check in {"version", "all"}:
        results["version"] = check_version(node)
    if args.check in {"config", "all"}:
        results["config_reads"] = check_config(node, allow_writes=False)
        results["config_writes"] = check_config(node, allow_writes=True)
    if args.check in {"mock", "all"}:
        results["mock"] = check_mock(node)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if not isinstance(v, dict)}, indent=2))
    for name in ("version", "config_reads", "config_writes", "mock"):
        if name in results:
            entry = results[name]
            print(f"\n{name}: {entry.get('status')} ({entry.get('reason', entry.get('detail', ''))})")
            if name == "version":
                print(f"  argv: {entry.get('argv')}")
                print(f"  rc={entry.get('returncode')} reported={entry.get('reported_version')!r} "
                      f"gone={entry.get('process_gone')} boundary_empty={entry.get('boundary_empty')}")
                print(f"  syntax check: rc={entry.get('node_syntax_check', {}).get('returncode')}")
            elif name.startswith("config"):
                print(f"  keys: {entry.get('config_keys')} | rc={entry.get('returncode')}")
            else:
                print(f"  argv: {entry.get('client_argv')}")
                print(f"  outcome={entry.get('outcome')} kinds={entry.get('event_kinds')} "
                      f"nonce_echoed={entry.get('nonce_echoed')} agent_launched={entry.get('mock_agent_launched')} "
                      f"gone={entry.get('client_process_gone')}")
    print(f"\nreport: {out_path}")
    failed = [
        name
        for name in ("version", "config_reads", "config_writes", "mock")
        if results.get(name, {}).get("status") not in {"PASS"}
    ]
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
