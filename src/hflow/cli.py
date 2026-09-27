"""HFlow command line: doctor / run / status / report / cancel / resume / schema.

Everything here is deterministic and offline in M1. ``doctor`` never calls a model
and never boots a DSH profile; it reports what is *known* locally and marks the
rest ``unknown``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import (
    CapabilityState,
    InvocationOutcome,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    ResultReceipt,
    RunRequest,
    TaskSpec,
    TaskState,
    canonical_json,
    json_schema,
)

if TYPE_CHECKING:  # imported for annotations only: authorization.py is not a CLI dependency
    from .authorization import AuthorizationBinding, AuthorizationRecord
    from .contracts import RootBudgetLimits

from .controller import Controller, RunOutcome, inspect_run
from .drivers.fake import FakeDriver, FakeScript
from .drivers.acpx_dsh import DriverSetupError
from .drivers.selected import default_refusal_reason, local_probe
from .paths import database_path, default_data_dir
from . import profiles
from .profiles import ENV_PROFILE
from .report import report_json, report_text
from .runtime import controller_build
from .store import RunNotFound, Store
from .verify import CheckRunners

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BLOCKED = 3
EXIT_USAGE = 4


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RefusedError(RefusalCode.INVALID_SPEC, f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RefusedError(RefusalCode.INVALID_SPEC, f"{path} is not valid JSON: {exc}") from exc


def _write_out(payload: object, as_json: bool, path: Path | None = None) -> None:
    text = canonical_json(payload) if as_json else str(payload)
    if path is None:
        print(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(str(path))


def _open_store(args: argparse.Namespace) -> Store:
    data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else default_data_dir()
    return Store(database_path(data_dir))

def _outcome_payload(outcome: RunOutcome) -> dict[str, object]:
    notes = list(outcome.notes)
    notes.append(_invocation_note(outcome))
    drift = _drift_note(outcome)
    if drift:
        notes.append(drift)
    return {
        "run_id": outcome.run_id,
        "task_state": outcome.task_state.value,
        "phase": outcome.phase.value if outcome.phase else None,
        "delivery_state": outcome.delivery_state.value,
        "block_code": outcome.block_code.value if outcome.block_code else None,
        "block_reason": outcome.block_reason,
        "turns_reserved": outcome.turns_reserved,
        "turns_limit": outcome.turns_limit,
        # Split on purpose: implementer and reviewer are separate driver processes.
        "implementer_invocations": outcome.implementer_invocations,
        "reviewer_invocations": outcome.reviewer_invocations,
        "driver_invocations_total": outcome.driver_invocations,
        "workspace_matches_receipt": outcome.workspace_matches_receipt,
        "notes": notes,
        "receipt": outcome.receipt.model_dump(mode="json") if outcome.receipt else None,
    }


def _invocation_note(outcome: RunOutcome) -> str:
    return (
        f"driver invocations: implementer={outcome.implementer_invocations} "
        f"reviewer={outcome.reviewer_invocations}; turns reserved "
        f"{outcome.turns_reserved}/{outcome.turns_limit}; billed model requests unknown "
        "(not observable in this build)"
    )


def _drift_note(outcome: RunOutcome) -> str | None:
    if outcome.workspace_matches_receipt is None:
        return None
    if outcome.workspace_matches_receipt:
        return "workspace still matches the accepted candidate fingerprint"
    return (
        "WARNING: the scoped files changed after acceptance; the stored ACCEPTED status "
        "describes the historical candidate, not the current working tree"
    )


def _outcome_exit_code(outcome: RunOutcome) -> int:
    if outcome.task_state is TaskState.ACCEPTED:
        return EXIT_OK
    if outcome.task_state is TaskState.BLOCKED:
        return EXIT_BLOCKED
    return EXIT_REFUSED


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


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

    from .drivers.selected import build_driver, resolve_driver_id

    roles: dict[str, object] = {}
    dependency_problems: list[str] = []
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
            dependency_problems.append(f"{role}: {exc.message}")
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
            instance = build_driver(binding, data_dir=data_dir)
        except Exception as exc:  # noqa: BLE001 - an unusable binding is a report, not a crash
            entry["usable"] = False
            entry["detail"] = f"{type(exc).__name__}: {exc}"
            dependency_problems.append(f"{role}: {exc}")
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
        else "; ".join(dependency_problems)
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only environment probe. Never installs, never modifies global config."""
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
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
    executables = report["executables"]
    assert isinstance(executables, dict)
    for name in ("python", "git", "dsh", "acpx", "node"):
        found = shutil.which(name)
        entry: dict[str, object] = {"path": found, "available": bool(found)}
        if found and name in {"git", "dsh", "node"}:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed, read-only version probes
                    [found, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                entry["version"] = (completed.stdout or completed.stderr).strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError) as exc:
                entry["version"] = f"probe failed: {exc}"
        report["executables"][name] = entry  # type: ignore[index]

    dsh_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    profiles_dir = dsh_home / "profiles"
    if profiles_dir.is_dir():
        report["dsh_profiles"] = sorted(
            p.name
            for p in profiles_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "node_modules"
        )
        report["dsh_home"] = str(dsh_home)

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
        "observed only executables and file presence"
    )

    if args.json:
        print(canonical_json(report))
    else:
        print(f"python        {report['python']}")
        for name, entry in executables.items():  # type: ignore[union-attr]
            version = entry.get("version", "")
            print(f"{name:<13} {entry['path'] or 'NOT FOUND'}{('  ' + version) if version else ''}")
        print(f"dsh home      {report.get('dsh_home', 'not found')}")
        print(f"dsh profiles  {', '.join(report['dsh_profiles']) or 'none'}")  # type: ignore[arg-type]
        print(f"data dir      {report['data_dir']} (writable={report['data_dir_writable']})")
        print(
            f"profile       {profile_section['requested'] or '(none selected)'} "
            f"[via {profile_section['requested_via']}] "
            f"{'usable' if profile_section['usable'] else 'NOT USABLE'}"
        )
        if not profile_section["usable"]:
            print(f"  detail      {profile_section['detail']}")
        for role, entry in (profile_section.get("roles") or {}).items():  # type: ignore[union-attr]
            state = "usable" if entry.get("usable") else "NOT USABLE"
            print(
                f"  {role:<11} agent={entry.get('agent')} driver={entry.get('driver')} "
                f"-> {entry.get('driver_id') or 'unresolved'} [{state}]"
            )
            if entry.get("detail"):
                print(f"               {entry['detail']}")
            for dependency in entry.get("dependencies") or []:
                print(f"               dep  {dependency}")
        print(f"driver        {report['selected_driver']} [{report['driver_status']}]")
        capability_states = report["capabilities"]["states"]  # type: ignore[index]
        for state in ("probed", "enforced", "documented", "unsupported", "unknown"):
            names = ", ".join(capability_states[state]) or "-"
            print(f"cap {state:<11} {names}")
        for note in report["notes"]:  # type: ignore[union-attr]
            print(f"note          {note}")
    return profile_exit


def cmd_prepare(args: argparse.Namespace) -> int:
    """Zero-model resolution of one task: what would run, under which config, for how much.

    This command exists so a task can be read *before* it is approved. It resolves the same
    inputs `run` does - through the same function - and reports the effective configuration,
    the admission problems, the write scope, the approved checks, the budget the fixed loop
    needs and a preview of the implementer's input packet.

    It creates nothing: no run row, no workspace, no SQLite database, no authorization. The
    binding it prints is what an approval *would* have to cover; producing the approval itself
    is the user's action, and `creates_authorization` is pinned to false so that promise is
    checkable rather than asserted.

    With `--root-budget-file` the preview also reports the root this run would be spent
    against. That is still zero-write: the ledger path is computed, never created or opened.
    """
    from .prepare import (
        build_prepare_report,
        load_repair_policy,
        load_root_budget_plan,
        render_prepare_text,
        resolve_run,
    )

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    root_plan = (
        load_root_budget_plan(Path(args.root_budget_file)) if args.root_budget_file else None
    )
    # The explicit repair opt-in, read and validated before anything is resolved. An invalid
    # policy refuses here, with no report printed and nothing dispatched.
    repair_policy = (
        load_repair_policy(Path(args.repair_policy_file)) if args.repair_policy_file else None
    )
    resolved = resolve_run(
        task_path=Path(args.task),
        project_root=Path(args.project_root),
        data_dir=data_dir,
        project_path=Path(args.project) if args.project else None,
        profile_id=args.profile,
        driver=args.driver,
        base_commit=args.base_commit,
        workspace_mode=args.workspace,
        repair_policy=repair_policy,
    )
    report = build_prepare_report(
        resolved, authorization_mode=args.authorization_mode, root_budget_plan=root_plan
    )
    if args.json:
        print(canonical_json(report.model_dump(mode="json")))
    else:
        print(render_prepare_text(report))
    # "Will this run?" is the question prepare is asked, so anything the run itself would
    # refuse on has to reach the exit code: the admission gate *and* the dispatch gate. A
    # preview that reported success for a task a run refuses would be worse than no preview.
    return EXIT_OK if resolved.ready_to_dispatch else EXIT_REFUSED


#: Recorded on the in-memory artifact an offline root run charges its dispatches to. It is
#: deliberately explicit: a record that read as a user approval which never happened would be a
#: false statement in the ledger, and the ledger is the place those statements are read from.
OFFLINE_ROOT_APPROVAL_TEXT = (
    "OFFLINE FAKE DRIVER - NOT A USER APPROVAL. This run reaches no model. The record exists "
    "only because every charge against a root budget must name the artifact that bought it; "
    "`hflow run` minted it for the offline driver. It binds driver 'fake' and cannot authorize "
    "a real transport. Pass --authorization-file to charge an offline root run to your own "
    "artifact instead."
)


def offline_root_authorization(
    *, binding: AuthorizationBinding, limits: RootBudgetLimits
) -> AuthorizationRecord:
    """The labelled record an offline root run charges its dispatches to.

    ``store.reserve_dispatch`` refuses a root charge with no authorization id, on purpose: a root
    allowance nobody approved must not be spendable. The offline fake driver reaches no model and
    therefore has no approval to give, so the CLI mints one that says exactly that - it names the
    fake driver in its binding and carries a ``user_text`` stating it is not an approval. A real
    driver never reaches this path: it needs an artifact and is refused without one.
    """
    from .authorization import AuthorizationRecord
    from .ids import new_id, utc_now

    return AuthorizationRecord(
        authorization_id=new_id("AUTH-offline"),
        provided_by="user",
        user_text=OFFLINE_ROOT_APPROVAL_TEXT,
        authorized_at=utc_now(),
        # The artifact's own ceiling is capped at 4 by the contract; the root ledger remains the
        # real ceiling, so a smaller artifact cap cannot overspend anything.
        max_top_level_submissions=min(4, limits.max_top_level_submissions),
        binding=binding,
        root_limits=limits,
        # Not a user artifact, and the record says so in a field rather than only in prose:
        # ``verify_authorization`` refuses this origin for any driver but the offline fake, so a
        # sentence in ``user_text`` is no longer the only thing standing between a synthesized
        # record and a real run. ``provided_by`` stays "user" because the contract's only
        # provenance value is the user's - the honest statement of "nobody approved this" is
        # this field, and the CLI never pretends otherwise.
        origin="cli_offline_synthetic",
    )


def _zero_model_preflight(role_drivers: dict[str, object]):
    """A cheap, model-free check that every role's launch binding works, before any dispatch.

    For the selected transport this launches the installed client with a metadata argument -
    no session, no prompt, no credential - which is exactly the failure mode that cost a
    submission in an earlier round. It runs once per real invocation, and its result is
    returned rather than logged, so a failure refuses the run instead of warning about it.

    Every distinct role driver is checked, and the failure names the role: with per-role
    bindings the reviewer can use a different configuration than the implementer, and a broken
    reviewer binding must be discovered here rather than after the implementer has been paid for.
    """
    checked: list[tuple[str, object]] = []
    for role, instance in role_drivers.items():
        if any(instance is seen for _, seen in checked):
            continue  # same configuration, same object: one launch proves it
        checked.append((role, instance))

    def check() -> tuple[bool, str]:
        for role, driver in checked:
            readonly = getattr(driver, "readonly_client_check", None)
            if not callable(readonly):
                return False, (
                    f"driver {getattr(driver, 'driver_id', '?')!r} (role {role}) has no "
                    "zero-model probe"
                )
            result = readonly(["--version"], timeout_seconds=90)
            reported = str(result.get("stdout", "")).strip()
            if result.get("returncode") != 0 or not reported:
                return False, (
                    f"role {role}: the client did not report a version "
                    f"(rc={result.get('returncode')}, "
                    f"stderr={str(result.get('stderr', ''))[:160]!r})"
                )
            if not result.get("process_gone") or not result.get("boundary_empty"):
                return False, (
                    f"role {role}: the client process or its boundary did not settle after "
                    "the probe"
                )
        if not checked:
            return False, "no role driver was resolved, so no launch binding could be proven"
        return True, f"client reports a version for role(s) {', '.join(r for r, _ in checked)}"

    return check


def cmd_run(args: argparse.Namespace) -> int:
    from .prepare import (
        load_repair_policy,
        load_root_budget_plan,
        resolve_machine_bindings,
        resolve_run,
        role_drivers,
    )

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()

    # The authorization question is settled before the task is even read: refusing here means a
    # missing approval cannot be confused with a malformed task, and no file is touched first.
    # "Is this a real run?" comes from the resolved machine bindings - the profile when one is
    # named, the command line otherwise - which is the same resolution the run itself uses.
    machine = resolve_machine_bindings(
        data_dir=data_dir, profile_id=args.profile, driver=args.driver
    )
    is_real_driver = machine.is_real_driver
    if is_real_driver and not args.authorization_file:
        message = (
            "this run resolves to a real Harness driver and needs an explicit, bound user "
            "authorization file (--authorization-file). There is no flag that substitutes for "
            "one and no fallback to the fake driver."
        )
        _write_out({"refused": True, "reason": "live_authorization_missing", "detail": message}, args.json)
        print(f"refused: {message}", file=sys.stderr)
        return EXIT_REFUSED
    root_plan = (
        load_root_budget_plan(Path(args.root_budget_file)) if args.root_budget_file else None
    )
    # The task's explicit opt-in to one bounded repair. Read here, before the spec is resolved:
    # the policy becomes part of the effective TaskSpec, so the stored spec, its digest and the
    # authorization binding all cover the repair the task asked for. An unreadable or unsupported
    # document refuses before a run row, a workspace or an allowance exists.
    repair_policy = (
        load_repair_policy(Path(args.repair_policy_file)) if args.repair_policy_file else None
    )

    resolved = resolve_run(
        task_path=Path(args.task),
        project_root=Path(args.project_root),
        data_dir=data_dir,
        project_path=Path(args.project) if args.project else None,
        profile_id=args.profile,
        driver=args.driver,
        base_commit=args.base_commit,
        workspace_mode=args.workspace,
        repair_policy=repair_policy,
    )
    spec = resolved.spec
    project = resolved.project
    project_root = resolved.project_root
    task_path = resolved.spec_path

    if resolved.is_real_driver != is_real_driver:
        # The profile is read once to settle the authorization question and once by the full
        # resolution. If those disagree, the file changed underneath this command, and
        # continuing would mean dispatching under a configuration no gate has seen.
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "the machine configuration changed while this run was being resolved "
            f"(was real_driver={is_real_driver}, now {resolved.is_real_driver}). Nothing was "
            "dispatched and no allowance was consumed; re-run when the configuration is stable.",
        )

    if not resolved.admission.ok and not args.force:
        _write_out(
            {
                "refused": True,
                "issues": [
                    issue.model_dump(mode="json") for issue in resolved.admission.issues
                ],
                "warnings": resolved.admission.warnings,
            },
            args.json,
        )
        return EXIT_REFUSED

    # A real Harness run needs an explicit, bound, single-use authorization artifact. This is
    # checked before any credential is read, any workspace is created and any budget or
    # submission allowance is consumed, and it refuses rather than falling back to the fake
    # driver. A bare flag is deliberately not accepted: the same process that would run the
    # task must not be able to authorize itself with a word. The binding now covers the
    # *effective configuration*, so approving one profile does not approve another. A root
    # budget file extends the same artifact: the binding covers the root too, so the ledger
    # records which approval bought each dispatch.
    authorization = None
    authorization_binding = None
    root_binding = None
    root_limits = None
    offline_root_note = ""
    if is_real_driver or root_plan is not None:
        from .authorization import (
            current_binding,
            load_authorization,
            resolve_root_binding,
            verify_authorization,
        )

        if root_plan is not None:
            # Derived, not created: the binding names the ledger, and the store creates or
            # migrates it only when the dispatch transaction actually runs.
            root_binding = resolve_root_binding(
                project_id=project.project_id,
                request=resolved.request(),
                data_dir=data_dir,
            )
        authorization_binding = current_binding(
            mode=args.authorization_mode,
            driver=resolved.effective.role("implementer").driver,  # type: ignore[union-attr]
            project=project,
            request=resolved.request(),
            spec_path=task_path,
            effective=resolved.effective,
            root_binding=root_binding,
        )
        if args.authorization_file:
            authorization = load_authorization(Path(args.authorization_file))
            # Refuses with a specific mismatch list when the artifact covers a different task,
            # project, base commit, driver, execution mode, configuration or root.
            verify_authorization(authorization, expected=authorization_binding)
        if root_plan is not None:
            if authorization is not None:
                root_limits = authorization.root_limits
                # verify_authorization refused unless the artifact carries this exact root, and
                # the contract refuses a root artifact without its ceilings, so this is never
                # None here.
                assert root_limits is not None
                if root_limits != root_plan.limits:
                    raise RefusedError(
                        RefusalCode.RISK_DOWNGRADE,
                        "the root budget file and the authorization disagree about this root's "
                        f"ceilings: the file says top-level submissions "
                        f"{root_plan.limits.max_top_level_submissions}, repairs "
                        f"{root_plan.limits.max_repairs}, deadline "
                        f"{root_plan.limits.deadline_seconds}s, while the authorization says "
                        f"{root_limits.max_top_level_submissions}, {root_limits.max_repairs}, "
                        f"{root_limits.deadline_seconds}s. One of the two files is wrong; nothing "
                        "was dispatched and no allowance was consumed.",
                    )
            else:
                # Offline only: a real driver without an artifact was refused before this point.
                # The fake driver reaches no model, so there is no approval to ask for - but the
                # ledger still refuses to charge a root without naming the artifact that bought
                # it, so the run mints one that says exactly what it is.
                root_limits = root_plan.limits
                authorization = offline_root_authorization(
                    binding=authorization_binding, limits=root_limits
                )
                offline_root_note = (
                    "offline root run: no --authorization-file was given, so this run charged "
                    "its dispatches to a CLI-minted record "
                    f"({authorization.authorization_id}) whose text says it is not a user "
                    "approval. It binds the offline fake driver and cannot authorize a real one"
                )

    store = _open_store(args)
    try:
        if not is_real_driver:
            # The fake driver is a scripted stand-in for tests and examples. Its change comes
            # from a plan file so an offline run is reproducible from the CLI alone, without a
            # test harness. It is explicitly the test driver: it never pretends to be a real
            # Harness delivery. Both roles share it when a profile binds them to the same
            # offline agent, because there is nothing per-role to distinguish offline.
            script = FakeScript(outcome=InvocationOutcome.COMPLETED, agent_turns=1)
            if args.fake_write_plan:
                plan = _load_json(Path(args.fake_write_plan))
                if not isinstance(plan, dict):
                    raise RefusedError(
                        RefusalCode.INVALID_SPEC, "--fake-write-plan must be a JSON object of path -> text"
                    )
                script.write_plan = {str(key): str(value) for key, value in plan.items()}
                script.limitations = ["fake driver: no model was invoked; change came from a plan file"]
            implementer_driver: object = FakeDriver(project_root, script)
            drivers = {"implementer": implementer_driver, "reviewer": implementer_driver}
        else:
            try:
                drivers = role_drivers(resolved)
            except DriverSetupError as exc:
                # A launcher that cannot be resolved is a refusal with a reason, not a
                # traceback: it is knowable before anything is claimed, and `prepare` reports
                # the same condition as a dispatch precondition.
                raise RefusedError(
                    RefusalCode.NOT_IMPLEMENTED,
                    f"the launch for this configuration could not be resolved: {exc}. Nothing "
                    "was dispatched and no allowance was consumed.",
                ) from exc
            implementer_driver = drivers["implementer"]

        controller = Controller(
            store,
            implementer_driver,  # type: ignore[arg-type]
            reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
            controller_build=controller_build(),
            # Keep approved checks from scattering caches into the workspace under test: a
            # check should leave evidence, not untracked files that later look like unfrozen
            # changes. This does not weaken any check.
            runners=CheckRunners.offline_default(
                extra_env={"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}
            ),
            controller_id=args.controller_id,
            data_dir=data_dir,
            authorization=authorization,
            # Batch E1: the root this run is spent against, and the ceilings the approval
            # carried. Both are None for a run with no root budget file, which is the legacy
            # path. The dispatch transaction is what charges them.
            root_binding=root_binding,
            root_limits=root_limits,
            preflight=(
                _zero_model_preflight(drivers) if authorization is not None else None
            ),
            # `--driver fake` is the offline driver: it scripts its own change and its checks
            # are fake by construction. Everything else is a real delivery, and a real delivery
            # gets the stricter gates (real checks, isolated worktree, effective write
            # permission, a budget that covers the review it requires).
            production=is_real_driver,
            effective_config=resolved.effective,
        )
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
                controller_id=args.controller_id,
            )
        )
    finally:
        store.close()

    payload = _outcome_payload(outcome)
    if offline_root_note:
        notes = payload["notes"]
        assert isinstance(notes, list)
        notes.append(offline_root_note)
    if args.receipt_out:
        receipt = outcome.receipt
        _write_out(
            receipt.model_dump(mode="json") if receipt else payload,
            True,
            Path(args.receipt_out),
        )
    elif args.json:
        _write_out(payload, True)
    else:
        print(f"run        {outcome.run_id}")
        print(f"state      {outcome.task_state.value}" + (f" ({outcome.phase.value})" if outcome.phase else ""))
        print(
            f"turns      {outcome.turns_reserved}/{outcome.turns_limit} reserved "
            f"(implementer={outcome.implementer_invocations} "
            f"reviewer={outcome.reviewer_invocations})"
        )
        if outcome.block_code:
            print(f"blocked    {outcome.block_code.value}: {outcome.block_reason}")
        drift = _drift_note(outcome)
        if drift:
            print(f"candidate  {drift}")
        for note in outcome.notes:
            print(f"note       {note}")
        if offline_root_note:
            print(f"note       {offline_root_note}")
    return _outcome_exit_code(outcome)


def cmd_status(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        inspection = inspect_run(
            store, args.run_id, project_root=Path(args.project_root).resolve()
        )
    except RunNotFound:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()
    from .report import status_text

    if args.json:
        print(canonical_json(report_json(inspection)))
    else:
        print(status_text(inspection))
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        inspection = inspect_run(
            store, args.run_id, project_root=Path(args.project_root).resolve()
        )
    except RunNotFound:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()
    if args.json:
        print(canonical_json(report_json(inspection)))
    else:
        print(report_text(inspection))
    return EXIT_OK


def cmd_cancel(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        inspection = inspect_run(store, args.run_id)
        project_root = Path(args.project_root).resolve()
        controller = Controller(
            store,
            FakeDriver(project_root),
            controller_build=controller_build(),
            controller_id=args.controller_id,
        )
        receipt = controller.cancel(inspection.run.run_id)
    except RunNotFound:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()
    print(canonical_json(receipt.model_dump(mode="json")) if args.json else receipt.status)
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        observation = inspect_run(store, args.run_id)
        project_root = Path(args.project_root).resolve()
        controller = Controller(
            store,
            FakeDriver(project_root),
            controller_build=controller_build(),
            controller_id=args.controller_id,
        )
        outcome = controller.resume(observation.run.run_id)
    except RunNotFound:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()
    _write_out(_outcome_payload(outcome), args.json)
    return _outcome_exit_code(outcome)


def cmd_clean(args: argparse.Namespace) -> int:
    """Release a run's workspace: preview by default, remove only with --apply."""
    if args.apply and args.dry_run:
        print("refusing: --apply and --dry-run are contradictory", file=sys.stderr)
        return EXIT_USAGE
    from .cleanup import apply_cleanup, plan_cleanup, reconcile_cleanup

    store = _open_store(args)
    try:
        if args.reconcile:
            result = reconcile_cleanup(store, args.run_id)
            _write_out(result if args.json else f"{result['status']}: {result['detail']}", args.json)
            return EXIT_OK
        if args.apply:
            result = apply_cleanup(store, args.run_id, operator=args.controller_id)
            if args.json:
                print(canonical_json(result))
            else:
                detail = result.get("detail", "")
                print(f"{result['status']}: {detail}")
                if result.get("candidate_ref"):
                    print(f"kept          candidate ref {result['candidate_ref']}")
            # Idempotent success: "this workspace is not there" is the requested end state,
            # whether we removed it just now or a previous run did. Refusals, failures and
            # partial removals stay non-zero because the workspace is still present.
            if result.get("applied") or result.get("status") == "REMOVED":
                return EXIT_OK
            return EXIT_BLOCKED
        plan = plan_cleanup(store, args.run_id)
    except RunNotFound:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()

    if args.json:
        print(canonical_json(plan.as_dict()))
    else:
        print(f"run           {plan.run_id}")
        print(f"workspace     {plan.path or '(none)'}")
        print(f"state         {plan.workspace_state} (task {plan.task_state})")
        if plan.common_dir:
            print(f"git common    {plan.common_dir}")
            print(f"registered    {plan.registered}")
        if plan.head:
            print(f"head          {plan.head}")
            print(f"candidate     {plan.expected_candidate or '(no receipt)'}")
        if plan.candidate_ref:
            print(f"candidate ref {plan.candidate_ref} -> {plan.candidate_ref_target or '(missing)'}")
        if plan.tracked_changes:
            print(f"tracked       {', '.join(plan.tracked_changes[:5])}")
        if plan.ignored_paths:
            print(f"ignored       {', '.join(plan.ignored_paths[:5])}")
        if plan.unsupported:
            print(f"unsupported   {'; '.join(plan.unsupported[:3])}")
        print(f"decision      {'ALLOWED' if plan.allowed else 'REFUSED'}")
        for item in plan.refusals:
            print(f"  refuse      {item['reason']}: {item['detail']}")
        for reason in plan.reasons:
            print(f"  note        {reason}")
        for keep in plan.keeps:
            print(f"  keeps       {keep}")
        if plan.allowed:
            print("apply with:   hflow clean " + plan.run_id + " --apply")
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    """Print the generated JSON Schema. Generated, never a second hand-written copy.

    ``MachineProfile`` is here because a profile is a document a person writes by hand
    (``<data-dir>/profiles/<id>.json``), and ``PrepareReport`` because its output is consumed
    by scripts. Both are generated from the same models the loader validates against.
    """
    from .contracts import EffectiveConfig, MachineProfile, PrepareReport

    models = {
        "TaskSpec": TaskSpec,
        "ProjectConfig": ProjectConfig,
        "ResultReceipt": ResultReceipt,
        "RunRequest": RunRequest,
        "MachineProfile": MachineProfile,
        "EffectiveConfig": EffectiveConfig,
        "PrepareReport": PrepareReport,
    }
    payload = {name: json_schema(model) for name, model in models.items()}
    print(canonical_json(payload))
    return EXIT_OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def _add_store_args(parser: argparse.ArgumentParser) -> None:
    """`--data-dir` is registered per subcommand: it must work before and after the verb."""
    parser.add_argument(
        "--data-dir",
        default=None,
        help="runtime data directory (default: platform data dir, outside any repo)",
    )


def _add_repair_policy_arg(parser: argparse.ArgumentParser) -> None:
    """``--repair-policy-file`` on render-only and run commands: the one explicit repair opt-in.

    A bare ``RepairPolicy`` document, not a task file and not an authorization. It becomes part of
    the effective TaskSpec (and therefore of ``spec_digest`` and the authorization binding), so the
    repair it arms is the repair that was approved - and a task that already names a policy must
    agree with the file rather than have one of the two silently preferred.
    """
    parser.add_argument(
        "--repair-policy-file",
        default=None,
        help=(
            "JSON RepairPolicy document that explicitly opts this task into at most ONE bounded "
            "repair: {'max_attempts': 1, 'check_exit_codes': {check_id: [exit codes]}, "
            "'allow_reviewer_changes': bool}. It must fit this scope: a worktree task that is "
            "reviewed, with a run ceiling covering the worst case of 4 top-level dispatches"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hflow", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="read-only environment probe (no model calls)")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--profile",
        default=None,
        help=(
            f"machine profile id to resolve and report on (or {ENV_PROFILE}); without one, "
            "doctor says so instead of guessing a binding"
        ),
    )
    _add_store_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    prepare = sub.add_parser(
        "prepare",
        help="zero-model resolution: what a task would run, under which config, for how much",
    )
    prepare.add_argument("--task", required=True, help="path to task.json")
    prepare.add_argument("--project", default=None, help="path to .hflow/project.json")
    prepare.add_argument("--project-root", default=".", help="target project root (workspace)")
    prepare.add_argument("--profile", default=None, help=f"machine profile id (or {ENV_PROFILE})")
    prepare.add_argument(
        "--driver",
        default=None,
        help="driver override; must agree with --profile when both are given",
    )
    prepare.add_argument(
        "--base-commit",
        default=None,
        help="fix the base commit for a Git-worktree run (the same override `run` applies)",
    )
    prepare.add_argument(
        "--workspace",
        choices=["worktree", "in_place"],
        default=None,
        help="workspace mode override (the same override `run` applies)",
    )
    prepare.add_argument(
        "--authorization-mode",
        choices=["stop-trial", "m2-live-change"],
        default="m2-live-change",
        help="which authorized activity the pending binding would be for; modes are not interchangeable",
    )
    prepare.add_argument(
        "--root-budget-file",
        default=None,
        help=(
            "JSON root budget plan ({'limits': {...}, 'note': '...'}) for a run that spends "
            "against a root ledger. The preview derives the root binding and the ledger path; "
            "it creates no database"
        ),
    )
    prepare.add_argument("--json", action="store_true")
    _add_repair_policy_arg(prepare)
    _add_store_args(prepare)
    prepare.set_defaults(func=cmd_prepare)

    run = sub.add_parser("run", help="admit and run one task")
    run.add_argument("--task", required=True, help="path to task.json")
    run.add_argument("--project", default=None, help="path to .hflow/project.json")
    run.add_argument("--project-root", default=".", help="target project root (workspace)")
    run.add_argument(
        "--driver",
        default=None,
        help=(
            "driver id override: 'fake' (offline) or 'acpx-dsh' (the M0-selected transport). "
            "Without --profile the default is 'fake'. With --profile the profile supplies the "
            "per-role bindings and this flag must name the same driver or the run is refused."
        ),
    )
    run.add_argument(
        "--profile",
        default=None,
        help=(
            f"machine profile id, loaded from <data-dir>/profiles/<id>.json (or {ENV_PROFILE}). "
            "Binds the implementer and the reviewer independently"
        ),
    )
    run.add_argument("--controller-id", default="local-controller")
    run.add_argument("--receipt-out", default=None, help="write the result receipt to this path")
    run.add_argument(
        "--base-commit",
        default=None,
        help="fix the base commit for a Git-worktree run (overrides the TaskSpec before admission)",
    )
    run.add_argument(
        "--workspace",
        choices=["worktree", "in_place"],
        default=None,
        help="override workspace.mode before admission (worktree = isolated Git worktree)",
    )
    run.add_argument(
        "--fake-write-plan",
        default=None,
        help="JSON object of {relative path: file text} the fake driver should write",
    )
    run.add_argument(
        "--authorization-file",
        default=None,
        help=(
            "JSON authorization artifact (user text + binding) required by any real Harness "
            "driver; there is no flag substitute"
        ),
    )
    run.add_argument(
        "--authorization-mode",
        choices=["stop-trial", "m2-live-change"],
        default="m2-live-change",
        help="which authorized activity this run belongs to; modes are not interchangeable",
    )
    run.add_argument(
        "--root-budget-file",
        default=None,
        help=(
            "JSON root budget plan ({'limits': {...}, 'note': '...'}). Makes this run spend "
            "against a root ledger the authorization must bind. A real driver also needs "
            "--authorization-file with the same root; the offline driver mints a labelled "
            "offline record instead"
        ),
    )
    run.add_argument("--json", action="store_true")
    run.add_argument("--force", action="store_true", help="continue past admission issues (unsafe)")
    _add_repair_policy_arg(run)
    _add_store_args(run)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show a run's state from SQLite (no model calls)")
    status.add_argument("run_id")
    status.add_argument("--project-root", default=".", help="workspace used for the drift check")
    status.add_argument("--json", action="store_true")
    _add_store_args(status)
    status.set_defaults(func=cmd_status)

    report = sub.add_parser("report", help="render a run's receipt and evidence (no model calls)")
    report.add_argument("run_id")
    report.add_argument("--project-root", default=".", help="workspace used for the drift check")
    report.add_argument("--json", action="store_true")
    _add_store_args(report)
    report.set_defaults(func=cmd_report)

    cancel = sub.add_parser("cancel", help="cancel a run's live attempt")
    cancel.add_argument("run_id")
    cancel.add_argument("--project-root", default=".")
    cancel.add_argument("--controller-id", default="local-controller")
    cancel.add_argument("--json", action="store_true")
    _add_store_args(cancel)
    cancel.set_defaults(func=cmd_cancel)

    resume = sub.add_parser("resume", help="continue a run's state machine (never re-dispatches)")
    resume.add_argument("run_id")
    resume.add_argument("--project-root", default=".")
    resume.add_argument("--controller-id", default="local-controller")
    resume.add_argument("--json", action="store_true")
    _add_store_args(resume)
    resume.set_defaults(func=cmd_resume)

    schema = sub.add_parser("schema", help="print generated JSON Schema for the data contracts")
    schema.set_defaults(func=cmd_schema)

    clean = sub.add_parser(
        "clean", help="release a run's workspace: preview by default, remove with --apply"
    )
    clean.add_argument("run_id")
    clean.add_argument("--apply", action="store_true", help="actually remove the run's worktree")
    clean.add_argument(
        "--dry-run", action="store_true", help="explicit synonym for the default preview"
    )
    clean.add_argument(
        "--reconcile",
        action="store_true",
        help="after an interruption, decide this run's workspace state from recorded facts",
    )
    clean.add_argument("--controller-id", default="local-controller")
    clean.add_argument("--json", action="store_true")
    _add_store_args(clean)
    clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except RunNotFound as exc:
        print(f"unknown run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
