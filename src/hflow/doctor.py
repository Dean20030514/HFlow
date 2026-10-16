"""``hflow doctor``: the read-only environment probe.

It reports what is *known* locally and marks the rest ``unknown``: which executables PATH holds
(it runs only ``git --version`` and ``node --version``, never ``dsh`` or the acpx client), where a
launch would find the acpx entry and ``dsh``, which credential sources an HFlow child could have,
whether this shell enables writes, the ledger's health, and - with ``--profile`` - whether each
role's binding resolves. It never calls a model, never boots a DSH profile, never installs
anything and never opens a ledger read-write. Its exit code is ``0``, or ``2`` when a named
profile cannot be used; every readiness line is a fact, never a reason to fail.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from . import profiles
from .cli import EXIT_OK, EXIT_REFUSED, _data_dir, _is_locked
from .contracts import CapabilityState, RefusedError, canonical_json
from .drivers.selected import default_refusal_reason, local_probe
from .hints import shell_word as _shell_word
from .paths import database_path
from .profiles import ENV_PROFILE


def _doctor_profile_section(
    data_dir: Path, requested: str, source: str
) -> tuple[dict[str, object], int]:
    """Resolve the named profile and report whether it is usable. No model, no launch.

    Three states stay distinguishable, which is the whole point of this section: no profile
    was named, a profile was named but cannot be used, or a profile was named and resolved.
    The third reports each role's agent, driver and the dependencies that driver needs - so
    "the config is selected" and "the config can actually start something" are separate,
    visible answers rather than one optimistic one.
    """
    section: dict[str, object] = {
        "requested": requested,
        "requested_via": source,
        "selected": bool(requested),
        "usable": False,
        "path": str(profiles.profile_path(data_dir, requested)) if requested else "",
        "detail": "no machine profile selected",
        "roles": {},
    }
    if not requested:
        return section, EXIT_OK
    try:
        profile = profiles.load_profile(data_dir, requested)
        bindings = profiles.resolve_role_bindings(profile)
    except RefusedError as exc:
        section["detail"] = exc.message
        return section, EXIT_REFUSED

    from .drivers.acpx_dsh import resolve_launch_config
    from .drivers.selected import build_driver, resolve_driver_id

    roles: dict[str, object] = {}
    #: problem -> the roles that have it: two roles sharing one launch share its problem, and it
    #: is said once.
    dependency_problems: dict[str, list[str]] = {}
    for role, (agent_id, binding) in bindings.items():
        entry: dict[str, object] = {
            "agent": agent_id,
            "harness": binding.harness,
            "driver": binding.driver,
            "model_selection": binding.model_selection,
            "capability_record": binding.capability_record,
        }
        try:
            driver_id = resolve_driver_id(binding)
            entry["driver_id"] = driver_id
        except RefusedError as exc:
            entry["driver_id"] = None
            entry["usable"] = False
            entry["detail"] = exc.message
            dependency_problems.setdefault(exc.message, []).append(role)
            roles[role] = entry
            continue
        if driver_id == "fake":
            # Building the offline driver would create its scratch directory; there is no
            # external dependency to prove either, so nothing is constructed here.
            entry["usable"] = True
            entry["dependencies"] = ["none (offline fake driver: no model, no external client)"]
            roles[role] = entry
            continue
        try:
            # The resolution the driver itself performs when built without a launch (and the one
            # ``prepare`` performs per role), done once here so its facts can be shown even when
            # the launch is not resolvable; the driver is then built from this very launch.
            launch = resolve_launch_config(data_dir=data_dir, binding=binding)
            entry["launch"] = _doctor_launch_facts(launch)
            instance = build_driver(binding, data_dir=data_dir, launch=launch)
        except Exception as exc:  # noqa: BLE001 - an unusable binding is a report, not a crash
            entry["usable"] = False
            entry["detail"] = f"{type(exc).__name__}: {exc}"
            dependency_problems.setdefault(str(exc), []).append(role)
        else:
            probe = instance.probe(binding)  # type: ignore[attr-defined]
            entry["usable"] = True
            entry["dependencies"] = list(probe.notes)
            entry["probe_only"] = probe.probe_only
            entry["live_tested"] = probe.live_tested
        roles[role] = entry

    section["roles"] = roles
    section["profile_id"] = profile.profile_id
    section["profile_digest"] = profiles.profile_digest(profile)
    section["security_mode"] = profile.security_mode
    section["limits"] = profile.limits.model_dump(mode="json")
    section["usable"] = not dependency_problems
    section["detail"] = (
        "every bound role resolved"
        if not dependency_problems
        else "; ".join(
            f"{', '.join(names)}: {problem}" for problem, names in dependency_problems.items()
        )
    )
    return section, (EXIT_OK if not dependency_problems else EXIT_REFUSED)


def _capability_section(probe: object) -> dict[str, object]:
    """Group the capability record so "verified" and "unknown" cannot be read as one list.

    The states keep their existing meaning and none of them is upgraded here: ``probed`` and
    ``enforced`` are the observed ones, ``documented`` is upstream documentation only,
    ``unsupported`` is a known absence and ``unknown`` was never observed. The record is a
    static table from the selected driver's ``probe``, so it is labelled as such - doctor
    itself observes executables and file presence, not a live model round.
    """
    states: dict[str, list[str]] = {state.value: [] for state in CapabilityState}
    for name, state in getattr(probe, "capabilities", {}).items():
        states[state.value].append(name)
    for names in states.values():
        names.sort()
    return {
        "record": "static capability table from the selected driver's probe",
        "source": "src/hflow/drivers/acpx_dsh.py::AcpxDshDriver.probe",
        "evidence": "docs/m0-results.md, docs/adr/0001-transport.md",
        "observed_here": ["probed", "enforced"],
        "documented_only": ["documented"],
        "known_absent": ["unsupported"],
        "not_observed": ["unknown"],
        "probe_only": getattr(probe, "probe_only", True),
        "live_tested": getattr(probe, "live_tested", False),
        "states": states,
        "meaning": {
            "probed": "behaviour observed locally on this machine; the observation is recorded "
            "in the evidence above, not made by this doctor run",
            "enforced": "observed and constrained by this code, not merely observed",
            "documented": "claimed by upstream documentation only; not observed here",
            "unsupported": "known not to work on this launch path",
            "unknown": "never observed; no value is claimed",
        },
    }


def _doctor_ledger_settlements(data_dir: Path) -> dict[str, object]:
    """How many ledger entries an operator settled by attestation. Read-only, never migrates.

    Opened with SQLite's read-only URI mode so doctor neither creates nor migrates a ledger; a
    file it cannot read that way is reported as such rather than opened read-write.
    """
    path = database_path(data_dir)
    result: dict[str, object] = {"consumed": None, "void": None}
    if not path.exists():
        result["detail"] = "no ledger file in this data dir"
        return result
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                "'invocation_settlements'"
            ).fetchone()
            if present is None:
                result["detail"] = (
                    "not recorded: this ledger predates operator settlement (opening it with "
                    "status/report migrates it)"
                )
                return result
            rows = dict(
                conn.execute(
                    "SELECT settled_as, COUNT(*) FROM invocation_settlements GROUP BY settled_as"
                ).fetchall()
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["detail"] = f"unreadable read-only ({exc}); not opened read-write by doctor"
        return result
    result["consumed"] = int(rows.get("consumed", 0))
    result["void"] = int(rows.get("void", 0))
    result["detail"] = (
        f"{result['consumed']} consumed, {result['void']} void - settled by operator attestation "
        "(not observed); billed usage for those entries stays unknown"
    )
    return result


#: How long doctor's ledger integrity check may run before it stops and reports "incomplete",
#: and every how many SQLite steps the clock is read.
DOCTOR_LEDGER_CHECK_SECONDS = 10.0
DOCTOR_LEDGER_CHECK_STEPS = 1000


def _doctor_ledger_health(data_dir: Path) -> dict[str, object]:
    """The ledger's storage version and a bounded integrity check, read-only. Never repairs.

    Opened with SQLite's read-only URI mode, so doctor never creates, migrates or rebuilds a
    ledger. ``PRAGMA quick_check(1)`` stops at its first finding, and a progress handler stops it
    after :data:`DOCTOR_LEDGER_CHECK_SECONDS`; a check a lock or that bound cut short is
    ``incomplete`` - never read as corruption. ``status`` is ``none`` (no ledger), ``ok``,
    ``problem`` (the first finding is shown), ``incomplete`` or ``unreadable`` (not opened
    read-only, or not a SQLite database). A fact line: it never changes doctor's exit code.
    """
    import time

    from .migrate import SUPPORTED_STORAGE_VERSION

    path = database_path(data_dir)
    section: dict[str, object] = {
        "path": str(path),
        "status": "none",
        "storage_version": None,
        "build_storage_version": SUPPORTED_STORAGE_VERSION,
        "quick_check": None,
    }
    if not path.exists():
        section["detail"] = "no ledger file in this data dir"
        return section
    deadline = time.monotonic() + DOCTOR_LEDGER_CHECK_SECONDS
    stopped = False

    def past_deadline() -> int:
        nonlocal stopped
        stopped = stopped or time.monotonic() > deadline
        return 1 if stopped else 0

    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        section.update(status="unreadable", detail=f"not opened read-only ({exc})")
        return section
    try:
        conn.set_progress_handler(past_deadline, DOCTOR_LEDGER_CHECK_STEPS)
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'storage_version'"
            ).fetchone()
            section["storage_version"] = int(row[0]) if row is not None else None
        except (sqlite3.Error, ValueError):
            section["storage_version"] = None
        findings = [str(item[0]) for item in conn.execute("PRAGMA quick_check(1)").fetchall()]
    except sqlite3.Error as exc:
        if stopped or _is_locked(exc) or "interrupt" in str(exc).lower():
            why = "locked by another process" if _is_locked(exc) else (
                f"stopped after {DOCTOR_LEDGER_CHECK_SECONDS:g}s"
            )
            section.update(
                status="incomplete",
                detail=f"{path}: integrity check incomplete ({why}); not a finding - run doctor "
                "again later",
            )
        else:
            section.update(status="unreadable", detail=f"{path}: unreadable read-only ({exc})")
        return section
    finally:
        conn.close()
    version = section["storage_version"]
    version_text = (
        f"storage version {version}" if version is not None else "storage version not recorded"
    )
    if isinstance(version, int) and version > SUPPORTED_STORAGE_VERSION:
        version_text += (
            f", newer than this build's {SUPPORTED_STORAGE_VERSION}: this build refuses to open it"
        )
    section["quick_check"] = findings[0] if findings else ""
    if findings == ["ok"]:
        section.update(status="ok", detail=f"{path}: quick_check ok, {version_text}")
    else:
        section.update(
            status="problem",
            detail=(
                f"{path}: quick_check found a problem ({section['quick_check'] or 'no answer'}), "
                f"{version_text}. Nothing was repaired; keep a copy before anything touches it"
            ),
        )
    return section


#: Executables doctor runs, with ``--version`` only and always from the absolute path PATH lookup
#: found. ``dsh`` is deliberately not one of them: the ``dsh`` on PATH may be the DSH Desktop app's
#: shim, and running it could start that app - doctor reports its path and nothing else.
_DOCTOR_VERSION_PROBES = frozenset({"git", "node"})
#: The DSH Desktop app's command-line shim, relative to ``%LOCALAPPDATA%`` (per-user install).
#: Doctor only checks that this file exists; it never runs it and never edits PATH.
DESKTOP_DSH_SHIM_RELATIVE = Path(
    "Programs", "DeepSeek Harness", "resources", "runtime", "cli", "bin", "dsh.cmd"
)


def _doctor_version(program: str) -> str:
    """The first line ``<program> --version`` prints, or why it could not be read."""
    try:
        completed = subprocess.run(  # noqa: S603 - an absolute path and a fixed metadata flag
            [program, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        return (completed.stdout or completed.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return f"probe failed: {exc}"


def _doctor_launch_facts(launch: object) -> dict[str, object]:
    """Which acpx entry (and its version), dsh and node a launch resolved. Nothing is executed.

    The version is the acpx package.json's ``version`` field (``observe_client_identity``), which
    is what the package says, not a compatibility proof.
    """
    from .drivers.dsh_surfaces import observe_client_identity

    client_entry = getattr(launch, "client_entry", "")
    version = ""
    if client_entry:
        try:
            version = observe_client_identity(launch).acpx_version  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - a record, never a reason doctor fails
            version = ""
    return {
        "client_entry": client_entry or None,
        "acpx_version": version or None,
        "dsh_executable": getattr(launch, "dsh_executable", "") or None,
        "node": getattr(launch, "node", "") or None,
        "resolvable": bool(getattr(launch, "resolvable", False)),
    }


def _doctor_integrate_readiness(git: dict[str, object]) -> dict[str, object]:
    """Whether ``hflow integrate`` can run with the Git on PATH, judged by its version only."""
    from .gitworkspace import MIN_INTEGRATION_GIT, GitError, parse_git_version

    needed = ".".join(str(part) for part in MIN_INTEGRATION_GIT)
    section: dict[str, object] = {
        "usable": None,
        "minimum_git": needed,
        "git": git.get("path"),
        "git_version": None,
    }
    if not git.get("path"):
        section["usable"] = False
        section["detail"] = (
            f"hflow integrate: NOT USABLE (needs Git {needed} or later; no git on PATH)"
        )
        return section
    reported = str(git.get("version") or "")
    try:
        version = parse_git_version(reported)
    except GitError:
        section["detail"] = (
            f"hflow integrate: unknown (needs Git {needed} or later; `git --version` gave "
            f"{reported[:80]!r})"
        )
        return section
    found = ".".join(str(part) for part in version)
    section["git_version"] = found
    section["usable"] = version[:2] >= MIN_INTEGRATION_GIT
    section["detail"] = (
        f"hflow integrate: usable (Git {found}; needs Git {needed} or later)"
        if section["usable"]
        else f"hflow integrate: NOT USABLE (needs Git {needed} or later; this Git is {found})"
    )
    return section


def _doctor_acpx_readiness(
    launch: object, on_path: dict[str, object], data_dir: Path
) -> dict[str, object]:
    """Where the acpx client entry resolves from - which is never PATH - and its version.

    When none is found, the detail also prints the install that puts one where HFlow looks
    (``<data-dir>/m0/acpx``) and the model-free check to run on it. Printed only: doctor never
    runs npm or the client.
    """
    from .drivers.acpx_dsh import DEV_ACPX_RELATIVE, ENV_ACPX_CLI, PROBE_ACPX_RELATIVE
    from .drivers.dsh_surfaces import RECORDED_ACPX_VERSION

    facts = _doctor_launch_facts(launch)
    order = (
        f"${ENV_ACPX_CLI}, then <data-dir>/{DEV_ACPX_RELATIVE.as_posix()}, then this HFlow "
        f"checkout's {PROBE_ACPX_RELATIVE.as_posix()}"
    )
    section: dict[str, object] = {
        "client_entry": facts["client_entry"],
        "acpx_version": facts["acpx_version"],
        "on_path": on_path.get("path"),
        "resolution_order": order,
    }
    if facts["client_entry"]:
        section["detail"] = (
            f"{facts['client_entry']} (acpx {facts['acpx_version'] or 'version unknown'}, read "
            "from its package.json; nothing executed)"
        )
        return section
    path_note = (
        f"the acpx on PATH ({on_path['path']}) is not used"
        if on_path.get("path")
        else "acpx is not on PATH either, and PATH is not where HFlow looks"
    )
    # The npm prefix the data-dir step looks under: the part of the entry before node_modules.
    # Absolute, so the printed commands work from any directory.
    base = Path(os.path.abspath(data_dir))
    parts = DEV_ACPX_RELATIVE.parts
    prefix = base.joinpath(*parts[: parts.index("node_modules")])
    entry = base / DEV_ACPX_RELATIVE
    install = f"npm install --prefix {_shell_word(str(prefix))} acpx@{RECORDED_ACPX_VERSION}"
    check = (
        f"python tools/m0_probe/real_client_checks.py all --acpx-cli {_shell_word(str(entry))}"
    )
    section["install_command"] = install
    section["check_command"] = check
    section["detail"] = (
        f"NOT FOUND - HFlow resolves the acpx entry from {order}; {path_note}. Set "
        f"{ENV_ACPX_CLI} to the dist/cli.js of an acpx package installed outside any project, "
        f"or install one where HFlow looks: `{install}`, then check it without a model "
        f"(from this HFlow checkout): `{check}`. A machine profile does not name the acpx "
        "entry in this build"
    )
    return section


def _doctor_dsh_readiness(on_path: dict[str, object], data_dir: Path) -> dict[str, object]:
    """Where a launch finds ``dsh``: PATH first, then the npm CLI under the data directory.

    The same lookup a launch makes (``acpx_dsh.resolve_dsh``). With neither, the detail names both
    places and prints the npm install (user ruling 2026-10-14). A DSH Desktop shim is reported
    with DSH #9626 and never recommended. Path and stat only: doctor never runs dsh.
    """
    from .drivers.acpx_dsh import DSH_SOURCE_DATA_DIR, DSH_SOURCE_PATH, find_in_directory
    from .drivers.dsh_surfaces import (
        DSH_NPM_PACKAGE,
        RECORDED_DSH_NPM_VERSION,
        dsh_npm_bin_dir,
        dsh_npm_install_command,
    )

    bin_dir = dsh_npm_bin_dir(data_dir)
    installed = find_in_directory("dsh", bin_dir, os.environ)
    section: dict[str, object] = {
        "on_path": on_path.get("path"),
        "data_dir_install": installed or None,
        "source": None,
        "desktop_shim": None,
    }
    never_run = "path only: doctor never runs dsh, which may start the DSH Desktop app"
    if on_path.get("path"):
        section["source"] = DSH_SOURCE_PATH
        section["detail"] = f"{on_path['path']} (dsh={DSH_SOURCE_PATH}; {never_run})" + (
            f"; the npm CLI under the data dir ({installed}) is not used while PATH has a dsh"
            if installed
            else ""
        )
        return section
    if installed:
        section["source"] = DSH_SOURCE_DATA_DIR
        section["detail"] = (
            f"{installed} (dsh={DSH_SOURCE_DATA_DIR}: PATH has no dsh, so a launch uses the "
            f"{DSH_NPM_PACKAGE}@{RECORDED_DSH_NPM_VERSION} install under the data directory; "
            f"{never_run})"
        )
        return section
    install = dsh_npm_install_command(data_dir)
    section["install_command"] = install
    detail = (
        "NOT FOUND on PATH (only absolute PATH entries are searched, never the current "
        f"directory) or in {bin_dir}: a real launch is not resolvable without one. Install the "
        f"npm CLI where HFlow looks: `{install}`"
    )
    local = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    if local:
        shim = Path(local) / DESKTOP_DSH_SHIM_RELATIVE
        if shim.is_file():  # stat only: the shim is never read or run here
            section["desktop_shim"] = str(shim)
            detail += (
                f". The DSH Desktop shim at {shim} is not that route: DSH #9626 (community "
                "reports, unconfirmed) makes its workspace-write sandbox - DSH's default - fail "
                "every shell tool call, while the npm CLI's sandbox runner is node.exe"
            )
    section["detail"] = detail
    return section


def _doctor_writes_readiness() -> dict[str, object]:
    """How a task with ``scope.write_allow`` gets its write opt-in, and the fallback this shell has.

    ``--allow-writes`` on ``prepare`` (recorded in the plan) or ``run --task``; without the flag
    those two read ``HFLOW_ALLOW_WRITES``, and ``approve --plan`` / ``run --plan`` never do. A
    fact line: it never changes doctor's exit code.
    """
    from .paths import ENV_ALLOW_WRITES
    from .prepare import writes_opted_in

    enabled = writes_opted_in()
    detail = (
        f"{ENV_ALLOW_WRITES} is enabled in this shell, so prepare and run --task opt in to "
        "writes even without --allow-writes. A plan records the opt-in it was prepared with; "
        "approve --plan and run --plan never read the variable"
        if enabled
        else "a task that changes files (scope.write_allow, which every `hflow init task` task "
        "has) needs --allow-writes on prepare - the plan records it, and approve --plan and run "
        f"--plan use the recorded value - or on run --task ({ENV_ALLOW_WRITES} is not enabled in "
        "this shell)"
    )
    return {"variable": ENV_ALLOW_WRITES, "enabled": enabled, "detail": detail}


def _doctor_credentials(child_kind: str, child_home: Path) -> dict[str, object]:
    """Which credential sources an HFlow child could have, and whether a real launch is refused.

    The facts are ``dsh_surfaces.credential_sources``' and the verdict is the launch rule's
    (``acpx_dsh.credential_source_problem``, user ruling 2026-10-07), applied to the child
    environment a launch would build from this one - so doctor cannot disagree with ``prepare``
    or the spawn gate. ``DEEPSEEK_API_KEY`` is reported by name: its value is never read, printed
    or hashed. A bound DSH home's stored-credentials file and ``.env`` are checked by stat only.
    A fact line: it never changes doctor's exit code.
    """
    from .drivers.acpx_dsh import child_environment, credential_source_problem
    from .drivers.dsh_surfaces import (
        DEEPSEEK_API_KEY_NAME,
        STORED_CREDENTIALS_NAME,
        credential_sources,
    )

    kind: Literal["bound", "per_invocation"] = (
        "bound" if child_kind == "bound" else "per_invocation"
    )
    child_env = child_environment(
        os.environ, extra_env={}, dsh_home=str(child_home) if kind == "bound" else ""
    )
    sources = credential_sources(child_env, dsh_home_kind=kind, dsh_home=child_home)
    key = "present" if sources.api_key_in_env else "absent"
    refused = bool(
        credential_source_problem(child_env, dsh_home_kind=kind, dsh_home=child_home)
    )
    section: dict[str, object] = {
        DEEPSEEK_API_KEY_NAME: key,
        "child_dsh_home_kind": child_kind,
        "dsh_home_files": dict(sources.home_files),
        "real_launch_refused": refused,
    }
    key_text = f"{DEEPSEEK_API_KEY_NAME} {key} in this environment (name only, value never read)"
    if kind == "bound":
        listed = ", ".join(f"{name} {state}" for name, state in sources.home_files)
        detail = (
            f"{key_text}; bound DSH home {child_home}: {listed} (stat only, never opened). DSH's "
            "documented precedence (not observed): the launch environment, then the home's "
            f"{STORED_CREDENTIALS_NAME}, then <cwd>/.env, then the home's .env"
        )
    else:
        detail = (
            f"{key_text}; DSH_HOME is not bound, so a child's DSH home is the per-invocation one, "
            "created empty - no stored credentials - and the launch environment is its only "
            "credential source (inferred from upstream source, not observed)"
        )
    section["detail"] = detail + (
        ". A real launch from this environment would be REFUSED before dispatch "
        "(no_credential_source): no credential source is visible"
        if refused
        else ". A real launch is not refused for credentials: a source is present (a presence "
        "check only - it does not prove the credential is valid)"
    )
    return section


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only environment probe. Never installs, never modifies global config.

    It runs ``git --version`` and ``node --version`` (from the absolute paths PATH lookup found)
    and nothing else: never ``dsh``, never the acpx client, never a model. The readiness lines it
    adds (integrate's Git minimum, where the acpx entry and ``dsh`` resolve from, which
    credential sources a child could have) are facts for the operator; the exit code stays
    ``0``, or ``2`` when a named profile cannot be used.
    """
    data_dir = _data_dir(args)
    profile_source = (
        "--profile"
        if args.profile
        else (ENV_PROFILE if os.environ.get(ENV_PROFILE) else "none")
    )
    requested_profile = profiles.requested_profile_id(args.profile)
    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "executables": {},
        "dsh_profiles": [],
        "data_dir": str(data_dir),
        "data_dir_writable": os.access(data_dir.parent if not data_dir.exists() else data_dir, os.W_OK),
        "notes": [
            "doctor performed no model calls, built no run and did not boot any DSH profile",
        ],
    }
    from .drivers.acpx_dsh import (
        INVOCATION_ID_PLACEHOLDER,
        child_home_for,
        effective_dsh_home,
        find_on_path,
        resolve_launch_config,
    )

    executables = report["executables"]
    assert isinstance(executables, dict)
    for name in ("python", "git", "dsh", "acpx", "node"):
        # Absolute PATH entries only, as a launch resolves its programs: ``shutil.which`` looks in
        # the current directory first on Windows and can return a relative path.
        found = find_on_path(name, os.environ)
        entry: dict[str, object] = {"path": found or None, "available": bool(found)}
        if found and name in _DOCTOR_VERSION_PROBES:
            entry["version"] = _doctor_version(found)
        elif found and name == "dsh":
            entry["note"] = "not run: doctor never starts dsh (it may be the DSH Desktop app)"
        elif name == "dsh":
            # A launch also looks in the data directory's npm install: see the launcher line.
            entry["note"] = "not on PATH; the dsh launcher line says what a launch uses"
        elif name == "acpx":
            # Reported for the record only: a launch never resolves acpx from PATH.
            entry["note"] = (
                "PATH lookup only, never used by a launch; the acpx client line says what is"
            )
        executables[name] = entry

    dsh_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    profiles_dir = dsh_home / "profiles"
    if profiles_dir.is_dir():
        report["dsh_profiles"] = sorted(
            p.name
            for p in profiles_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "node_modules"
        )
        report["dsh_home"] = str(dsh_home)
    # ``dsh_home`` above is the home *your* shell's dsh uses. An HFlow child gets the launch's
    # bound DSH_HOME or none at all, so its home is named separately (read-only resolution).
    # The same resolution says where the acpx entry comes from, which is never PATH.
    ambient_launch = resolve_launch_config(data_dir=data_dir)
    child_kind, child_home = effective_dsh_home(
        ambient_launch,
        child_home=child_home_for(data_dir.resolve(), INVOCATION_ID_PLACEHOLDER),
    )
    report["child_dsh_home"] = {
        "kind": child_kind,
        "path": str(child_home),
        "detail": (
            "DSH_HOME is set here, so every HFlow child gets it (bound when the launch is "
            "resolved)"
            if child_kind == "bound"
            else "DSH_HOME is not set here, so an HFlow child gets none: the driver removes it "
            "and points USERPROFILE/HOME at the invocation's own home, and DSH's default home is "
            "that home's .dsh, created empty for each invocation (no stored credentials, no "
            "cordis.patch.yml, no AGENTS.md, no skills, a fresh anonymous id) - inferred from "
            "upstream source, not observed"
        ),
    }

    report["ledger_health"] = _doctor_ledger_health(data_dir)
    report["ledger_settlements"] = _doctor_ledger_settlements(data_dir)
    # Facts an operator needs before a first real run or an integration. None of them changes
    # the exit code: they are reported, and `prepare` / the run gates still decide.
    report["readiness"] = {
        "integrate": _doctor_integrate_readiness(executables["git"]),
        "acpx": _doctor_acpx_readiness(ambient_launch, executables["acpx"], data_dir),
        "dsh": _doctor_dsh_readiness(executables["dsh"], data_dir),
        "credentials": _doctor_credentials(child_kind, child_home),
        "writes": _doctor_writes_readiness(),
    }

    probe = local_probe()
    report["capability_record"] = probe.model_dump(mode="json")
    report["capabilities"] = _capability_section(probe)

    profile_section, profile_exit = _doctor_profile_section(
        data_dir, requested_profile, profile_source
    )
    report["profile"] = profile_section
    if requested_profile and profile_section.get("usable"):
        implementer = (profile_section.get("roles") or {}).get("implementer", {})  # type: ignore[union-attr]
        report["selected_driver"] = implementer.get("driver_id") or "unresolved"
        report["driver_status"] = "RESOLVED_FROM_PROFILE"
    elif requested_profile:
        report["selected_driver"] = "unresolved"
        report["driver_status"] = "PROFILE_NOT_USABLE"
    else:
        # Not "unselected" as a defect: no profile was named. A live run needs one, and saying
        # so is different from saying a binding exists.
        report["selected_driver"] = "none"
        report["driver_status"] = "NO_PROFILE_SELECTED"
        report["notes"].append(  # type: ignore[union-attr]
            f"no machine profile selected: pass --profile <id> or set {ENV_PROFILE}. "
            "`--driver fake` remains the offline default."
        )
        # Why nothing runs unattended by default, stated where a reader looks for it.
        report["notes"].append(default_refusal_reason())  # type: ignore[union-attr]
    report["notes"].append(  # type: ignore[union-attr]
        "capability states are the recorded table, not a live compatibility proof; doctor "
        "found executables on the absolute PATH entries and ran only `git --version` and "
        "`node --version` (never dsh, never the acpx client), and observed file presence, the "
        "dsh shim's text (to classify its carrier), the version fields of package and manifest "
        "files, the SHA-256 of the launch entry files and, with --profile, of a bound DSH home's "
        "patch files and AGENTS.md - nothing else was read and no .env or credential file was "
        "opened (their presence is checked by stat only)"
    )
    report["notes"].append(  # type: ignore[union-attr]
        "dsh_home/dsh_profiles describe the DSH home your own shell's dsh uses; an HFlow child "
        "uses child_dsh_home"
    )

    if args.json:
        print(canonical_json(report))
    else:
        print("\n".join(_doctor_text_lines(report)))
    return profile_exit


def _doctor_text_lines(report: dict[str, Any]) -> list[str]:
    """``doctor``'s text form: the facts ``--json`` prints, one labelled line each."""
    lines = [f"python        {report['python']}"]
    for name, entry in report["executables"].items():
        version = entry.get("version", "")
        note = entry.get("note", "")
        suffix = f"  {version}" if version else (f"  ({note})" if note else "")
        # acpx on PATH is a record, not a dependency: "NOT FOUND" there is no problem.
        missing = "not on PATH" if name == "acpx" else "NOT FOUND"
        lines.append(f"{name:<13} {entry['path'] or missing}{suffix}")
    child = report["child_dsh_home"]
    readiness = report["readiness"]
    lines += [
        f"your dsh home {report.get('dsh_home', 'not found')}",
        f"child dsh home {child['kind']} {child['path']}",
        f"dsh profiles  {', '.join(report['dsh_profiles']) or 'none'}",
        f"data dir      {report['data_dir']} (writable={report['data_dir_writable']})",
        f"ledger health {report['ledger_health']['detail']}",
        f"ledger settle {report['ledger_settlements']['detail']}",
        f"integrate     {readiness['integrate']['detail']}",
        f"acpx client   {readiness['acpx']['detail']}",
        f"dsh launcher  {readiness['dsh']['detail']}",
        f"credentials   {readiness['credentials']['detail']}",
        f"writes        {readiness['writes']['detail']}",
    ]
    profile = report["profile"]
    roles = profile.get("roles") or {}
    lines.append(
        f"profile       {profile['requested'] or '(none selected)'} "
        f"[via {profile['requested_via']}] "
        f"{'usable' if profile['usable'] else 'NOT USABLE'}"
    )
    if not profile["usable"] and not roles:
        # With roles, each role's own line below says what is wrong; the profile's detail would
        # only repeat them.
        lines.append(f"  detail      {profile['detail']}")
    shown: dict[str, str] = {}
    for role, entry in roles.items():
        state = "usable" if entry.get("usable") else "NOT USABLE"
        lines.append(
            f"  {role:<11} agent={entry.get('agent')} driver={entry.get('driver')} "
            f"-> {entry.get('driver_id') or 'unresolved'} [{state}]"
        )
        detail = entry.get("detail")
        if detail and detail in shown:
            lines.append(f"               same as {shown[detail]}")
        elif detail:
            shown[detail] = role
            lines.append(f"               {detail}")
        launch = entry.get("launch")
        if launch:
            lines.append(
                f"               launch acpx={launch['client_entry'] or 'NOT FOUND'} "
                f"(acpx {launch['acpx_version'] or 'version unknown'}) "
                f"dsh={launch['dsh_executable'] or 'NOT FOUND'}"
            )
        lines += [f"               dep  {item}" for item in entry.get("dependencies") or []]
    lines.append(f"driver        {report['selected_driver']} [{report['driver_status']}]")
    capability_states = report["capabilities"]["states"]
    for state in ("probed", "enforced", "documented", "unsupported", "unknown"):
        lines.append(f"cap {state:<11} {', '.join(capability_states[state]) or '-'}")
    lines += [f"note          {note}" for note in report["notes"]]
    return lines
