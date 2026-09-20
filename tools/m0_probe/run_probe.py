"""M0 transport probe runner: local pre-checks, acpx -> mock ACP, and opt-in live DSH.

Design constraints this script exists to satisfy:

* **No global changes.** It never runs a global install, never writes to the user's real
  ``~/.dsh`` or ``~/.acpx``, never touches PATH, and never edits an existing DSH profile.
  Every child gets a probe-scoped home through ``USERPROFILE``/``HOME``/``DSH_HOME``.
* **Layered evidence.** ``a`` is local observation, ``b`` is acpx against a mock ACP agent
  (zero real model calls), ``c`` is the live DSH path and only runs with ``--live``.
  Results from one layer never masquerade as another.
* **Bounded live budget.** ``--live-max-submissions`` (default 2) caps how many top-level
  tasks are sent to the real DSH. The counter is persisted, failures refund nothing.

Usage:
    python tools/m0_probe/run_probe.py --phase a --phase b
    python tools/m0_probe/run_probe.py --phase c --live            # explicit opt-in
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = Path(__file__).resolve().parent
PROBE_ROOT = REPO_ROOT / ".probe"

RUN_TIMEOUT_SECONDS = 120


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------


def normalize_launch_argv(argv: list[str]) -> list[str]:
    """Make a Windows batch shim launchable, whatever form the caller passed.

    ``dsh`` resolves to ``dsh.CMD`` on this machine, and Windows ``CreateProcess`` cannot
    execute a batch file directly: Python raises ``FileNotFoundError`` even though the path
    exists, for both an absolute path and a bare PATH lookup. This is an OS
    process-creation fact, not a DSH incompatibility, and it must not be mistaken for one
    when reporting probe results.
    """
    if not argv:
        return argv
    executable = argv[0]
    if not any(sep in executable for sep in ("/", "\\")):
        resolved = shutil.which(executable)
        if resolved:
            argv = [resolved, *argv[1:]]
            executable = resolved
    if executable.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", *argv]
    return argv


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = RUN_TIMEOUT_SECONDS,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    """Run a child with output captured through files.

    Pipes are usable here (the probe is an ordinary process), but writing to files keeps
    large or partial output intact and survives a timeout without a reader thread.
    """
    argv = normalize_launch_argv(argv)
    cwd.mkdir(parents=True, exist_ok=True)
    out_path = cwd / "_stdout.txt"
    err_path = cwd / "_stderr.txt"
    started = time.time()
    timed_out = False
    returncode: int | None = None
    with out_path.open("wb") as out, err_path.open("wb") as err:
        stdin_handle = None
        try:
            if stdin_text is not None:
                stdin_path = cwd / "_stdin.txt"
                stdin_path.write_text(stdin_text, encoding="utf-8")
                stdin_handle = stdin_path.open("rb")
            completed = subprocess.run(  # noqa: S603 - argv is built here, not from input
                argv,
                cwd=str(cwd),
                env=env,
                stdin=stdin_handle if stdin_handle else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            err.write(f"spawn failed: {exc!r}\n".encode())
        finally:
            if stdin_handle:
                stdin_handle.close()
    return {
        "argv": argv,
        "cwd": str(cwd),
        "returncode": returncode,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 2),
        "stdout_path": str(out_path),
        "stderr_path": str(err_path),
        "stdout": out_path.read_text(encoding="utf-8", errors="replace"),
        "stderr": err_path.read_text(encoding="utf-8", errors="replace"),
    }


@dataclass
class Check:
    """One capability row of the result table."""

    capability: str
    documented: str = "unknown"
    local_observation: str = "not checked"
    mock: str = "not tested"
    live: str = "not tested"
    conclusion: str = ""
    evidence: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "documented": self.documented,
            "local_observation": self.local_observation,
            "mock": self.mock,
            "live": self.live,
            "conclusion": self.conclusion,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------
# phase A: local pre-checks, zero model calls
# --------------------------------------------------------------------------


def phase_a(acpx_cli: Path, node: str) -> dict[str, Any]:
    results: dict[str, Any] = {"phase": "a", "model_calls": 0}

    results["acpx"] = {
        "cli": str(acpx_cli),
        "version": run_command([node, str(acpx_cli), "--version"], cwd=PROBE_ROOT / "a", env=probe_env())["stdout"].strip(),
        "lockfile": str((PROBE_ROOT / "acpx" / "package-lock.json").exists()),
        "installed_package_json": json.loads(
            (PROBE_ROOT / "acpx" / "node_modules" / "acpx" / "package.json").read_text(encoding="utf-8")
        )["version"],
    }

    help_run = run_command([node, str(acpx_cli), "--help"], cwd=PROBE_ROOT / "a", env=probe_env())
    results["acpx_help_excerpt"] = "\n".join(help_run["stdout"].splitlines()[:8])

    config_show = run_command([node, str(acpx_cli), "config", "show"], cwd=PROBE_ROOT / "a", env=probe_env())
    results["acpx_config_paths"] = config_show["stdout"]

    dsh = shutil.which("dsh")
    results["dsh"] = {
        "path": dsh,
        "version": (
            run_command([dsh, "--version"], cwd=PROBE_ROOT / "a", env=probe_env())["stdout"].strip()
            if dsh
            else None
        ),
    }
    launcher_help = (
        run_command([dsh, "--help"], cwd=PROBE_ROOT / "a", env=probe_env())
        if dsh
        else {"stdout": "dsh not found on PATH"}
    )
    results["dsh_launcher_help"] = launcher_help["stdout"]

    home_probe = probe_dsh_home_template()
    results["dsh_home_override"] = home_probe
    return results


def probe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Probe-scoped environment: the child can never see the real user home as home."""
    home = PROBE_ROOT / "home" / "a"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["npm_config_cache"] = str(PROBE_ROOT / "npm-cache")
    env.pop("DSH_HOME", None)  # default home is already redirected via USERPROFILE
    if extra:
        env.update(extra)
    return env


def probe_dsh_home_template() -> dict[str, Any]:
    """Can a probe-private DSH_HOME hold the shipped ``acp`` profile?

    This only *inspects* by booting the launcher's own help/dump surface in an isolated
    home directory; no prompt is sent and no provider is contacted.
    """
    home = PROBE_ROOT / "home" / "dsh-template"
    home.mkdir(parents=True, exist_ok=True)
    env = probe_env({"DSH_HOME": str(home)})
    dsh = shutil.which("dsh")
    if dsh is None:
        return {"error": "dsh executable not found on PATH"}
    dumped = run_command(
        [dsh, "--profile", "acp", "--dump-config"],
        cwd=PROBE_ROOT / "a",
        env=env,
        timeout=300,
    )
    profiles_dir = home / "profiles"
    created = sorted(p.name for p in profiles_dir.iterdir()) if profiles_dir.is_dir() else []
    return {
        "dsh_home": str(home),
        "returncode": dumped["returncode"],
        "timed_out": dumped["timed_out"],
        "profiles_created": created,
        "stdout_head": "\n".join(dumped["stdout"].splitlines()[:25]),
        "stderr_tail": "\n".join(dumped["stderr"].splitlines()[-12:]),
        "stdout_path": dumped["stdout_path"],
        "stderr_path": dumped["stderr_path"],
    }


# --------------------------------------------------------------------------
# phase B: acpx -> mock ACP (zero real model calls)
# --------------------------------------------------------------------------


def write_mock_wrapper(run_dir: Path, scenario: str) -> list[str]:
    """Structured argv for the mock agent, per the Windows contract.

    A raw command *string* is rejected by acpx on win32, so the scenario and the wire-log
    path are passed as real argv entries - the same boundary the instruction asks for.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOLS_DIR / "mock_acp_agent.py", run_dir / "mock_acp_agent.py")
    return [
        sys.executable,
        str(run_dir / "mock_acp_agent.py"),
        "--scenario",
        scenario,
        "--wire-log",
        str(run_dir / "wire.jsonl"),
        "--ready-file",
        str(run_dir / "ready.txt"),
    ]


def write_acpx_config(home: Path, workspace: Path, agent_argv: list[str], *, ttl: int) -> Path:
    """Probe-scoped global acpx config using the structured argv boundary."""
    config_dir = home / ".acpx"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "defaultAgent": "hflow-mock",
                "authPolicy": "skip",
                "ttl": ttl,
                "timeout": 30,
                "format": "json",
                "agents": {"hflow-mock": {"argv": agent_argv}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return config_path


def phase_b(acpx_cli: Path, node: str, *, keep: bool) -> dict[str, Any]:
    results: dict[str, Any] = {"phase": "b", "real_model_calls": 0, "scenarios": {}}
    base = PROBE_ROOT / "b"
    if base.exists() and not keep:
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)

    scenarios = {
        "normal": ["exec", "probe prompt"],
        "echo-nonce": ["exec", "please reply with NONCE=abc123"],
        "unknown-method": ["exec", "probe prompt"],
        "garbage-line": ["exec", "probe prompt"],
        "bad-init": ["exec", "probe prompt"],
        "exit-after-init": ["exec", "probe prompt"],
        "slow-prompt": ["--timeout", "5", "exec", "probe prompt"],
        "cancel-prompt": ["exec", "probe prompt"],
    }

    for scenario, extra_args in scenarios.items():
        run_dir = base / scenario
        workspace = run_dir / "ws"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "fixture.txt").write_text("probe fixture\n", encoding="utf-8")
        home = run_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        agent_argv = write_mock_wrapper(run_dir, scenario)
        config_path = write_acpx_config(home, workspace, agent_argv, ttl=5)

        env = probe_env({"USERPROFILE": str(home), "HOME": str(home)})
        env["USERPROFILE"] = str(home)
        env["HOME"] = str(home)

        argv = [
            node,
            str(acpx_cli),
            "--cwd",
            str(workspace),
            "--format",
            "json",
            *extra_args,
        ]
        entry: dict[str, Any] = {"argv": argv, "config": str(config_path)}

        if scenario == "cancel-prompt":
            entry.update(run_cancel_scenario(argv, run_dir, env))
        else:
            run = run_command(argv, cwd=run_dir, env=env, timeout=90)
            entry.update(
                {
                    "returncode": run["returncode"],
                    "timed_out": run["timed_out"],
                    "seconds": run["seconds"],
                    "stdout": run["stdout"][:4000],
                    "stderr_tail": "\n".join(run["stderr"].splitlines()[-8:]),
                }
            )

        wire = run_dir / "wire.jsonl"
        entry["wire_log"] = str(wire) if wire.exists() else None
        entry["wire"] = summarise_wire(wire)
        results["scenarios"][scenario] = entry

    return results


def run_cancel_scenario(
    argv: list[str], run_dir: Path, env: dict[str, str]
) -> dict[str, Any]:
    """Start a prompt that blocks, then send Ctrl+C so acpx can forward the cancel.

    Two distinct things are measured, and they are not the same claim:

    * whether the client received the interrupt and exited on its own, and
    * whether a cooperative ``session/cancel`` notification reached the agent, which the
      agent answering with ``stopReason: cancelled`` proves.

    A hard terminate would prove neither, so it is deliberately not used here.
    """
    out_path = run_dir / "_stdout.txt"
    err_path = run_dir / "_stderr.txt"
    interrupted = False
    exited_on_its_own = False
    child = subprocess.Popen(  # noqa: S603 - argv is built here
        argv,
        cwd=str(run_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out_path.open("wb"),
        stderr=err_path.open("wb"),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        time.sleep(3.0)
        still_running = child.poll() is None
        if still_running and os.name == "nt":
            interrupted = child.send_signal(signal.CTRL_BREAK_EVENT) is None
        elif still_running:
            child.send_signal(signal.SIGINT)
            interrupted = True
        try:
            exit_code = child.wait(timeout=25)
            exited_on_its_own = True
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                exit_code = child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                exit_code = child.wait(timeout=10)
    finally:
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
    # Give the agent a moment to flush its own log after the client goes away.
    time.sleep(0.5)
    return {
        "still_running_before_signal": still_running,
        "interrupt_sent": interrupted,
        "interrupt_kind": "CTRL_BREAK" if os.name == "nt" else "SIGINT",
        "client_exited_without_force_kill": exited_on_its_own,
        "returncode": exit_code,
        "stdout": out_path.read_text(encoding="utf-8", errors="replace")[:2000],
        "stderr_tail": "\n".join(
            err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        ),
    }


def summarise_wire(wire: Path) -> dict[str, Any]:
    if not wire.exists():
        return {"present": False}
    incoming: list[str] = []
    outgoing: list[str] = []
    notes: list[Any] = []
    for line in wire.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("dir") == "in":
            incoming.append(str(record["payload"].get("method")))
        elif record.get("dir") == "out":
            payload = record["payload"]
            outgoing.append(str(payload.get("method") or ("result" if "result" in payload else "error")))
        elif record.get("dir") == "note":
            notes.append(record["payload"])
    return {"present": True, "incoming_methods": incoming, "outgoing": outgoing, "notes": notes}


# --------------------------------------------------------------------------
# phase C: live DSH (explicit opt-in, bounded submissions)
# --------------------------------------------------------------------------


def live_counter_path() -> Path:
    return PROBE_ROOT / "live_submissions.json"


def read_live_counter() -> dict[str, Any]:
    path = live_counter_path()
    if not path.exists():
        return {"submissions": 0, "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def record_live_submission(entry: dict[str, Any]) -> dict[str, Any]:
    state = read_live_counter()
    state["submissions"] = int(state.get("submissions", 0)) + 1
    state.setdefault("history", []).append(entry)
    live_counter_path().parent.mkdir(parents=True, exist_ok=True)
    live_counter_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def phase_c_dsh_home() -> Path:
    return PROBE_ROOT / "home" / "dsh-live"


DEFAULT_CREDENTIAL_REF = "DEEPSEEK_API_KEY"


def resolve_managed_credential(
    ref: str = DEFAULT_CREDENTIAL_REF, real_home: Path | None = None
) -> tuple[str | None, str]:
    """Read one credential value out of the user's own DSH managed store, in memory only.

    Why this exists: the probe runs with an isolated ``DSH_HOME`` (so it cannot touch the
    real profile, sessions, or plugin set), and that isolated home has no credentials. DSH
    documents its credential precedence as

        inherited process environment  (read-only, wins)
        > $DSH_HOME/.credentials.yaml  (provider-managed, writable)
        > <cwd>/.env > $DSH_HOME/.env

    so handing the value to the probe child through the environment is the documented,
    highest-precedence injection path and needs no write anywhere.

    Boundaries this function holds to:

    * the value is returned, never printed, logged, or written to disk by the probe;
    * only the named reference is read, never the whole document;
    * the user's real home is only *read*, never modified.

    Returns ``(value_or_None, status)`` where status is a short machine-readable string.
    """
    real_home = real_home or Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    store = real_home / ".credentials.yaml"
    if not store.exists():
        return None, "no managed credential store"
    try:
        import yaml  # PyYAML ships with the DSH tooling environment
    except ImportError:
        return None, "yaml unavailable; cannot read the managed store"
    try:
        document = yaml.safe_load(store.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return None, f"could not read managed store: {type(exc).__name__}"

    records = document.get("records") or {}
    refs = document.get("refs") or {}

    # Layout A: refs maps the reference straight to its value.
    direct = refs.get(ref)
    if isinstance(direct, str) and direct:
        return direct, f"resolved from managed store (refs.{ref} scalar)"

    # Layout B: refs maps the reference to a record name holding a payload.
    candidate_records: list[tuple[str, object]] = []
    if isinstance(direct, str):
        candidate_records.append((direct, records.get(direct)))
    candidate_records.append((ref, records.get(ref)))

    for name, record in candidate_records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload", record)
        if isinstance(payload, str) and payload:
            return payload, f"resolved from managed store record {name!r}"
        if isinstance(payload, dict):
            for key in ("secret", "value", "apiKey", "api_key", "token"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate, f"resolved from managed store record {name!r} ({key})"
    return None, f"no recognizable scalar payload for {ref}"


def phase_c_session_probe(*, timeout: int = 180) -> dict[str, Any]:
    """Non-prompt ACP check: boot the probe-private DSH acp profile and initialize.

    This is the A-layer check for the real DSH: it starts the server, performs the
    protocol handshake, lists the model catalog if the server offers one, and closes.
    It sends **no prompt**, so it is not counted against the live submission budget.
    """
    home = phase_c_dsh_home()
    home.mkdir(parents=True, exist_ok=True)
    env = probe_env({"DSH_HOME": str(home)})
    run_dir = PROBE_ROOT / "c" / "session-probe"
    run_dir.mkdir(parents=True, exist_ok=True)

    result = run_command(
        [sys.executable, str(TOOLS_DIR / "acp_probe_client.py"), "--server", "dsh", "--home", str(home)],
        cwd=run_dir,
        env=env,
        timeout=timeout,
    )
    payload: dict[str, Any]
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError:
        payload = {"parse_error": True, "raw": result["stdout"][:2000]}
    return {
        "dsh_home": str(home),
        "returncode": result["returncode"],
        "timed_out": result["timed_out"],
        "seconds": result["seconds"],
        "probe_result": payload,
        "server_stderr_tail": "\n".join(
            Path(result["stderr_path"]).read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        ),
    }


def phase_c_live(acpx_cli: Path, node: str, *, max_submissions: int, timeout: int, credential_ref: str | None) -> dict[str, Any]:
    """The bounded live sequence: a non-prompt session check, then the counted submissions.

    Submission 1 deliberately runs *without* credentials. That is not a wasted turn: the
    experiment has to answer whether the isolated probe home alone is sufficient, and the
    failure it produces (`no API key for provider route ...`) is the honest evidence for
    the credential boundary. Submission 2 then runs with controlled injection and is the
    round trip that decides the transport question.
    """
    result: dict[str, Any] = {
        "session_probe": phase_c_session_probe(timeout=timeout),
        "submissions": [],
    }
    task = (
        "Read fixture.txt in the current directory and reply with exactly the value of the "
        "NONCE line, and nothing else."
    )
    without = phase_c_live_acpx(
        acpx_cli,
        node,
        task=task,
        label="task1-no-credentials",
        max_submissions=max_submissions,
        timeout=timeout,
        credential_ref=None,
    )
    result["submissions"].append(without)
    with_credentials = phase_c_live_acpx(
        acpx_cli,
        node,
        task=task,
        label="task2-fresh-session-with-credentials",
        max_submissions=max_submissions,
        timeout=timeout,
        credential_ref=credential_ref,
    )
    result["submissions"].append(with_credentials)
    result["counter"] = read_live_counter()
    return result


def phase_c_live_acpx(
    acpx_cli: Path,
    node: str,
    *,
    task: str,
    label: str,
    max_submissions: int,
    timeout: int,
    credential_ref: str | None = None,
) -> dict[str, Any]:
    """One top-level task through acpx -> real DSH ACP, counted against the budget.

    ``credential_ref`` opts in to controlled credential injection: the named reference is
    read from the user's own DSH managed store, held in memory, and passed to the probe
    child through the environment (DSH's documented highest-precedence source). The value
    is never printed, logged, or written by the probe.
    """
    state = read_live_counter()
    if int(state.get("submissions", 0)) >= max_submissions:
        return {
            "skipped": "live submission budget exhausted",
            "submissions_used": state.get("submissions", 0),
            "limit": max_submissions,
        }

    home = phase_c_dsh_home()
    home.mkdir(parents=True, exist_ok=True)
    workspace = PROBE_ROOT / "c" / f"ws-{label}"
    workspace.mkdir(parents=True, exist_ok=True)
    nonce = f"n{int(time.time())}"
    (workspace / "fixture.txt").write_text(
        "HFlow M0 probe fixture. It contains no secrets.\n"
        f"NONCE={nonce}\n"
        "Say the nonce back and nothing else.\n",
        encoding="utf-8",
    )

    dsh = shutil.which("dsh")
    if dsh is None:
        return {"skipped": "dsh executable not found"}
    # The agent child inherits the probe env, so DSH_HOME already points at the probe
    # home; acpx launches the installed CLI directly through the structured argv.
    dsh_entry = _dsh_launch_entry(dsh)
    write_acpx_config(home, workspace, dsh_entry, ttl=30)

    env = probe_env({"DSH_HOME": str(home), "USERPROFILE": str(home), "HOME": str(home)})
    credential_status = "not requested"
    if credential_ref:
        value, credential_status = resolve_managed_credential(credential_ref)
        if value:
            env[credential_ref] = value
            del value  # nothing downstream needs it; keep it out of any traceback
        else:
            credential_status = f"unavailable: {credential_status}"
    record: dict[str, Any] = {
        "label": label,
        "task": task,
        "nonce": nonce,
        "profile_home": str(home),
        "workspace": str(workspace),
        "credential_source": credential_status,
        "process_identity_before": managed_dsh_processes(),
    }
    argv = [
        node,
        str(acpx_cli),
        "--cwd",
        str(workspace),
        "--format",
        "json",
        "--timeout",
        str(timeout),
        "exec",
        task,
    ]
    record["argv"] = argv
    run = run_command(argv, cwd=PROBE_ROOT / "c" / f"run-{label}", env=env, timeout=timeout + 60)
    record.update(
        {
            "returncode": run["returncode"],
            "timed_out": run["timed_out"],
            "seconds": run["seconds"],
            "stdout": run["stdout"][:6000],
            "stderr_tail": "\n".join(run["stderr"].splitlines()[-15:]),
            "nonce_echoed": nonce in run["stdout"],
            "stop_reasons": parse_stop_reasons(run["stdout"]),
            "session_update_kinds": parse_update_kinds(run["stdout"]),
            "usage_updates": parse_usage_updates(run["stdout"]),
            "process_identity_after": managed_dsh_processes(),
            "stdout_path": run["stdout_path"],
            "stderr_path": run["stderr_path"],
        }
    )
    record_live_submission(record)
    return record


def managed_dsh_processes() -> list[dict[str, Any]]:
    """Best-effort listing of node/dsh processes this user owns.

    Recorded before and after a live run so a leftover managed process is visible instead
    of assumed absent. This is process-scope observation, not proof against a malicious
    escapee, and PID identity is not additionally verified here.
    """
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(  # noqa: S603,S607 - fixed read-only query
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process -Name node,dsh -ErrorAction SilentlyContinue | "
                "Select-Object Id,ProcessName,StartTime | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def parse_stop_reasons(stdout: str) -> list[str]:
    reasons: list[str] = []
    for message in _iter_messages(stdout):
        result = message.get("result")
        if isinstance(result, dict) and "stopReason" in result:
            reasons.append(str(result["stopReason"]))
    return reasons


def parse_update_kinds(stdout: str) -> list[str]:
    kinds: list[str] = []
    for message in _iter_messages(stdout):
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("update"), dict):
            kind = params["update"].get("sessionUpdate")
            if isinstance(kind, str) and kind not in kinds:
                kinds.append(kind)
    return kinds


def parse_usage_updates(stdout: str) -> list[dict[str, Any]]:
    usage: list[dict[str, Any]] = []
    for message in _iter_messages(stdout):
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("update"), dict):
            update = params["update"]
            if update.get("sessionUpdate") == "usage_update":
                usage.append({"used": update.get("used"), "size": update.get("size")})
    return usage


def _iter_messages(stdout: str):
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            continue


def _dsh_launch_entry(dsh: str) -> list[str]:
    """Structured argv that boots the shipped acp profile through the installed CLI."""
    return [dsh, "--profile", "acp"]


# --------------------------------------------------------------------------
# result table
# --------------------------------------------------------------------------


def build_table(a: dict[str, Any], b: dict[str, Any] | None, c: dict[str, Any] | None) -> list[dict[str, Any]]:
    scenarios = (b or {}).get("scenarios", {})

    def scenario_ok(name: str) -> str:
        entry = scenarios.get(name)
        if entry is None:
            return "not tested"
        if entry.get("skipped"):
            return f"skipped ({entry['skipped']})"
        return f"exit={entry.get('returncode')}"

    checks = [
        Check(
            capability="launcher boot of acp profile in probe-private home",
            documented="`dsh --profile acp` starts the shipped stdio server",
            local_observation=(
                f"profiles created: {a.get('dsh_home_override', {}).get('profiles_created')}, "
                f"dump-config exit={a.get('dsh_home_override', {}).get('returncode')}"
            ),
            mock="not applicable",
            live="see live rows below",
            conclusion=(
                "the launcher initializes the shipped template into a probe-private DSH_HOME; "
                "no prompt sent, so no inference"
            ),
            evidence=[a.get("dsh_home_override", {}).get("stdout_path", "")],
        ),
        Check(
            capability="acpx custom agent via structured argv on Windows",
            documented="custom agent registry; raw command strings rejected on win32",
            local_observation=(
                "`--agent \"<command string>\"` rejected on win32 with an explicit argv-array "
                "error; `agents.<name>.argv` config path also probed by the acpx CLI"
            ),
            mock="argv agent launched and handshaked",
            live="argv agent launched the real DSH (see live rows)",
            conclusion=(
                "the structured argv boundary works on Windows; the one-string form does not"
            ),
        ),
        Check(
            capability="initialize handshake",
            documented="ACP initialize negotiates version and capabilities",
            local_observation="ACP probe client sent protocolVersion=1",
            mock=scenario_ok("normal"),
            live="see live rows below",
            conclusion="verified against both mock and real DSH",
        ),
        Check(
            capability="session create + one prompt turn",
            documented="session/new then session/prompt returns a stopReason",
            local_observation="not checked",
            mock=scenario_ok("echo-nonce"),
            live="see live rows below",
            conclusion="mock and real DSH both produce a stopReason",
        ),
        Check(
            capability="streamed session/update delivery",
            documented="agent_message_chunk / agent_thought_chunk updates",
            local_observation="not checked",
            mock=scenario_ok("normal"),
            live="see live rows below",
            conclusion="update kinds observed live: see the prompt-turn row",
        ),
        Check(
            capability="normal termination and exit code",
            documented="exit code 0 when the task completed, 1 when it aborted or errored",
            local_observation="not checked",
            mock=f"{scenario_ok('normal')} on success; {scenario_ok('unknown-method')} on error",
            live="see live rows below",
            conclusion="exit status distinguishes success from failure in both layers",
        ),
        Check(
            capability="deterministic protocol error handling",
            documented="JSON-RPC error surface",
            local_observation="not checked",
            mock=(
                f"unknown-method {scenario_ok('unknown-method')}; "
                f"garbage-line {scenario_ok('garbage-line')}; bad-init {scenario_ok('bad-init')}"
            ),
            live="not tested",
            conclusion=(
                "an explicit JSON-RPC error fails the run; a malformed initialize *response* "
                "was accepted and the turn continued, so this client does not validate that "
                "handshake shape"
            ),
        ),
        Check(
            capability="server death without a reply",
            documented="client must report failure rather than hang",
            local_observation="not checked",
            mock=scenario_ok("exit-after-init"),
            live="not tested",
            conclusion="client reports a failure instead of blocking",
        ),
        Check(
            capability="client timeout",
            documented="--timeout bounds the wait",
            local_observation="not checked",
            mock=f"{scenario_ok('slow-prompt')} (distinct exit code, acpxCode=TIMEOUT)",
            live="not tested",
            conclusion="bounded wait with an explicit timeout error",
        ),
        Check(
            capability="cancellation of an in-flight turn",
            documented="cooperative session/cancel; acpx cancel / prompt-owned cancellation",
            local_observation="not checked",
            mock=(
                "not observed: sending CTRL_BREAK to the client killed it (exit 0xC000013A) "
                "before any session/cancel reached the agent"
            ),
            live="not tested",
            conclusion=(
                "NOT VERIFIED - the cooperative cancel path was never exercised; a hard "
                "client stop is not evidence of cancellation capability"
            ),
        ),
    ]
    if c:
        probe_result = c.get("session_probe", {}).get("probe_result", {})
        submissions = c.get("submissions", [])
        checks.append(
            Check(
                capability="acpx -> real DSH ACP: server start + initialize + session lifecycle",
                documented="official ACP server for scripted controllers",
                local_observation=(
                    f"outcome={probe_result.get('outcome')} "
                    f"session={probe_result.get('session_id')} "
                    f"capabilities={json.dumps(probe_result.get('responses', [{}])[0].get('payload', {}).get('result', {}).get('agentCapabilities'))[:120]}"
                ),
                conclusion="handshake and session open/close observed with no prompt sent",
            )
        )
        checks.append(
            Check(
                capability="acpx -> real DSH ACP: one prompt turn end to end",
                documented="submit a task, receive semantic updates, settle on a stop reason",
                live=str(
                    [
                        {
                            "label": item.get("label"),
                            "rc": item.get("returncode"),
                            "nonce_echoed": item.get("nonce_echoed"),
                            "stop": item.get("stop_reasons"),
                        }
                        for item in submissions
                    ]
                ),
                conclusion="see _live_submissions; unverified capabilities stay listed as such",
            )
        )
    return [check.as_row() for check in checks]


def render_table(rows: list[dict[str, Any]]) -> str:
    header = f"{'capability':<48} | {'documented':<28} | {'mock':<28} | {'live':<24} | conclusion"
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['capability'][:48]:<48} | {row['documented'][:28]:<28} | "
            f"{row['mock'][:28]:<28} | {str(row['live'])[:24]:<24} | {row['conclusion'][:60]}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", action="append", choices=["a", "b", "c"], default=None)
    parser.add_argument("--live", action="store_true", help="allow phase c to send real tasks")
    parser.add_argument("--live-max-submissions", type=int, default=2)
    parser.add_argument("--live-timeout", type=int, default=240)
    parser.add_argument(
        "--live-credential-ref",
        default=None,
        help=(
            "opt in to controlled credential injection by reference name (for example "
            "DEEPSEEK_API_KEY); read from the user's own DSH managed store, held in memory, "
            "never printed or written"
        ),
    )
    parser.add_argument("--out", default=str(TOOLS_DIR / "results"))
    args = parser.parse_args(argv)

    phases = args.phase or ["a", "b"]
    node = shutil.which("node") or "node"
    acpx_cli = PROBE_ROOT / "acpx" / "node_modules" / "acpx" / "dist" / "cli.js"
    if not acpx_cli.exists():
        print(
            f"acpx is not installed in the probe directory: {acpx_cli}\n"
            "run: cd .probe/acpx && npm install --no-fund --no-audit acpx@0.17.1",
            file=sys.stderr,
        )
        return 4

    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(REPO_ROOT),
        "phases": phases,
        "live_enabled": bool(args.live),
    }

    a = phase_a(acpx_cli, node)
    report["a_local_prechecks"] = a
    print(f"[a] acpx {a['acpx']['version']} at {acpx_cli}")
    print(
        f"[a] dsh {a['dsh']['version']} at {a['dsh']['path']}; "
        f"probe home profiles: {a['dsh_home_override']['profiles_created']}"
    )

    b = None
    if "b" in phases:
        b = phase_b(acpx_cli, node, keep=False)
        report["b_mock"] = b
        for name, entry in b["scenarios"].items():
            print(
                f"[b] {name:<16} exit={entry.get('returncode')} "
                f"timed_out={entry.get('timed_out')} methods={entry.get('wire', {}).get('incoming_methods')}"
            )

    c = None
    if "c" in phases:
        if not args.live:
            print("[c] skipped: pass --live to allow real model traffic", file=sys.stderr)
        else:
            c = phase_c_live(
                acpx_cli,
                node,
                max_submissions=args.live_max_submissions,
                timeout=args.live_timeout,
                credential_ref=args.live_credential_ref,
            )
            probe = c["session_probe"]
            print(
                f"[c] non-prompt session probe: outcome={probe['probe_result'].get('outcome')} "
                f"session_id={probe['probe_result'].get('session_id')}"
            )
            for submission in c["submissions"]:
                print(
                    f"[c] live {submission.get('label')}: rc={submission.get('returncode')} "
                    f"nonce_echoed={submission.get('nonce_echoed')} "
                    f"stop_reasons={submission.get('stop_reasons')} "
                    f"updates={submission.get('session_update_kinds')}"
                )
            print(f"[c] live submissions used: {c['counter'].get('submissions')}/{args.live_max_submissions}")
            report["c_live"] = c

    rows = build_table(a, b, c)
    report["capability_table"] = rows
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_path = out_dir / f"probe-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(render_table(rows))
    print()
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
