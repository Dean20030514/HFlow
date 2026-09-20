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

from .admission import validate_task_spec
from .contracts import (
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
from .controller import Controller, RunOutcome, inspect_run
from .drivers.fake import FakeDriver, FakeScript
from .drivers.selected import default_refusal_reason, local_probe
from .paths import database_path, default_data_dir
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only environment probe. Never installs, never modifies global config."""
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "executables": {},
        "dsh_profiles": [],
        "data_dir": str(data_dir),
        "data_dir_writable": os.access(data_dir.parent if not data_dir.exists() else data_dir, os.W_OK),
        "selected_driver": "unselected",
        "driver_status": "NOT_LIVE_TESTED",
        "notes": [
            default_refusal_reason(),
            "doctor performed no model calls and did not boot any DSH profile",
        ],
    }
    executables = report["executables"]
    assert isinstance(executables, dict)
    for name in ("python", "git", "dsh", "acpx", "node"):
        found = shutil.which(name)
        entry: dict[str, object] = {"path": found}
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
        print(f"driver        {report['selected_driver']} [{report['driver_status']}]")
        for note in report["notes"]:  # type: ignore[union-attr]
            print(f"note          {note}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    task_path = Path(args.task)
    spec = TaskSpec.model_validate(_load_json(task_path))
    project_root = Path(args.project_root).resolve()
    project_path = Path(args.project) if args.project else project_root / ".hflow" / "project.json"
    project = ProjectConfig.model_validate(_load_json(project_path))

    # Command-line overrides become part of the *effective* TaskSpec before admission, so the
    # stored spec, its digest and the run's identity all describe the same thing.
    overrides: dict[str, object] = {}
    if args.base_commit or args.workspace:
        mode = args.workspace or spec.workspace.mode
        overrides["workspace"] = {
            "mode": mode,
            "base_commit": args.base_commit or spec.workspace.base_commit,
            "keep": True if mode == "worktree" else spec.workspace.keep,
        }
    if overrides:
        spec = TaskSpec.model_validate({**spec.model_dump(mode="json"), **overrides})

    validation = validate_task_spec(spec, project, project_root)
    if not validation.ok and not args.force:
        _write_out(
            {
                "refused": True,
                "issues": [issue.model_dump(mode="json") for issue in validation.issues],
                "warnings": validation.warnings,
            },
            args.json,
        )
        return EXIT_REFUSED

    # Real drivers need an explicit, current authorization. The check happens before any
    # credential is read, before any workspace is created and before any budget is reserved,
    # and it refuses rather than silently falling back to the fake driver.
    if args.driver != "fake" and not args.live_authorized:
        message = (
            f"driver {args.driver!r} is a real Harness driver and no live authorization is "
            "recorded for this invocation. Pass --live-authorized only with an explicit, "
            "current user authorization; there is no fallback to the fake driver."
        )
        _write_out({"refused": True, "reason": "live_authorization_missing", "detail": message}, args.json)
        print(f"refused: {message}", file=sys.stderr)
        return EXIT_REFUSED

    store = _open_store(args)
    try:
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
        if args.driver == "fake":
            # The fake driver is a scripted stand-in for tests and examples. Its change comes
            # from a plan file so an offline run is reproducible from the CLI alone, without a
            # test harness. It is explicitly the test driver: `--driver fake` never pretends
            # to be a real Harness delivery.
            script = FakeScript(outcome=InvocationOutcome.COMPLETED, agent_turns=1)
            if args.fake_write_plan:
                plan = _load_json(Path(args.fake_write_plan))
                if not isinstance(plan, dict):
                    raise RefusedError(
                        RefusalCode.INVALID_SPEC, "--fake-write-plan must be a JSON object of path -> text"
                    )
                script.write_plan = {str(key): str(value) for key, value in plan.items()}
                script.limitations = ["fake driver: no model was invoked; change came from a plan file"]
            driver = FakeDriver(project_root, script)
        else:
            from .contracts import AgentBinding
            from .drivers.selected import build_driver

            driver = build_driver(
                AgentBinding(harness="dsh", driver=args.driver), data_dir=data_dir
            )
        controller = Controller(
            store,
            driver,  # type: ignore[arg-type]
            controller_build=controller_build(),
            # Keep approved checks from scattering caches into the workspace under test: a
            # check should leave evidence, not untracked files that later look like unfrozen
            # changes. This does not weaken any check.
            runners=CheckRunners.offline_default(
                extra_env={"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}
            ),
            controller_id=args.controller_id,
            data_dir=data_dir,
        )
        request = RunRequest(
            task=spec,
            project=project,
            project_root=project_root,
            workspace_root=project_root,
            controller_id=args.controller_id,
        )
        outcome = controller.run_task(request)
    finally:
        store.close()

    payload = _outcome_payload(outcome)
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
    """Print the generated JSON Schema. Generated, never a second hand-written copy."""
    models = {
        "TaskSpec": TaskSpec,
        "ProjectConfig": ProjectConfig,
        "ResultReceipt": ResultReceipt,
        "RunRequest": RunRequest,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hflow", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="read-only environment probe (no model calls)")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--profile",
        default=None,
        help="accepted for interface stability; this build has no profile binding yet",
    )
    _add_store_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="admit and run one task")
    run.add_argument("--task", required=True, help="path to task.json")
    run.add_argument("--project", default=None, help="path to .hflow/project.json")
    run.add_argument("--project-root", default=".", help="target project root (workspace)")
    run.add_argument(
        "--driver",
        default="fake",
        help="driver id: 'fake' (offline) or 'acpx-dsh' (the M0-selected transport)",
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
        "--live-authorized",
        action="store_true",
        help="assert an explicit, current user authorization for a real Harness driver",
    )
    run.add_argument("--json", action="store_true")
    run.add_argument("--force", action="store_true", help="continue past admission issues (unsafe)")
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
