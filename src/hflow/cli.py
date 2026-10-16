"""HFlow command line.

Subcommands: doctor / prepare / approve / run / status / report / cancel / resume / schema /
clean / ledger settle / integrate (<run-id>, prepare, apply, reconcile, show) / init (project,
profile, task) / runs / diff.

The operator loop: ``init`` the starting files, ``approve --task T --allow-writes --run`` (or
``prepare --plan-out``, ``approve --plan``, ``run --plan`` step by step), ``status`` for the
inbox of runs that need you, ``integrate <run-id>`` for an accepted candidate.

``init`` writes one starting file that does not exist yet and never overwrites one: a project
contract with ``kind=command`` checks, the two-role machine profile, or a task whose reuse
decision is left for the operator (admission refuses it until it is made). It writes no budget,
plan or authorization. ``runs`` lists runs with the next command for each, and ``diff`` prints the
Git change from a run's recorded base to its frozen candidate commit; both read stored facts only.

``prepare --plan-out`` saves a task's resolution as a plan; ``approve --plan`` writes the
authorization artifact for it only for a person at an interactive terminal, who types back the
plan's approval code and then the approval text (never an argument, never piped stdin);
``run --plan`` resolves from the plan instead of repeating every flag. ``approve --task`` does
the first two in one process. A plan records the write opt-in (``--allow-writes``); ``approve
--plan`` and ``run --plan`` never read ``HFLOW_ALLOW_WRITES``. Neither ``approve`` nor
``prepare`` calls a model or opens the database.

``ledger settle`` closes an ``unknown`` / ``launch_unknown`` ledger entry by operator attestation;
it never calls a model, never re-dispatches and never changes a run's state.

``integrate`` takes an ``ACCEPTED`` run's candidate onto an existing local branch as a separate,
operator-approved delivery: ``integrate <run-id>`` continues it from whatever state it is in;
step by step, ``prepare`` builds and checks one commit without moving anything, ``apply`` moves
the branch by compare-and-set only (never a branch some worktree has checked out), ``reconcile``
settles an interrupted record from what Git shows. It never calls a model.

``status`` without a run id is the inbox; with one it reads that run. ``report`` and ``diff``
read one run: its id, or ``--last`` for the most recently created one (named first as ``run:
<id>``); a command that acts on a run always takes its id. A command that looks a run,
integration or invocation up never creates a ledger to look in.

``doctor``, ``prepare``, ``approve``, ``status``, ``report``, ``schema``, ``clean``, ``init``,
``runs`` and ``diff`` never call a model; ``doctor`` never boots a DSH profile or runs ``dsh``: it
reports what is *known* locally, marks the rest ``unknown``. ``resume`` reconciles and never
re-dispatches; on a live run it takes over only from an owner process proven gone, and that
takeover blocks the run ``owner_lost``.

A run's exclusive owner is the controller *process* (``hflow.ownership``): a random token, its
pid, creation time and host, and an OS file lock held for the process's lifetime.

``run`` is offline only with the ``fake`` driver (the default without a profile). With
``--driver acpx-dsh`` or a profile that binds it, ``run`` launches the real Harness and needs a
user authorization artifact.

Exit codes: 0 accepted (or the command succeeded), 2 refused at admission (no run state),
3 blocked after dispatch, 4 usage, 5 the run exists and is not finished - and, whatever the run's
own state, a ``resume`` that refused on the owner rule and wrote nothing (an ``outcome_unknown``
run whose owner may still be alive, or an ended run whose open ledger entries it may not close
yet: an owner that may be alive, or a pre-v6 owner nobody attested gone) or whose store write
failed, never the 3 or 0 that would read as done - and any command whose ledger is locked by
another process, 6 status/report found the run but a stored record of it no longer validates, or
any command hit a ledger error it does not map itself (a storage version newer than this build, a
file that is not a ledger, a write the ledger refused), 130 interrupted (Ctrl+C): what was
recorded before the interrupt is authoritative - read it with ``hflow status``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ValidationError

from .contracts import (
    CancellationReceipt,
    InvocationOutcome,
    ProjectConfig,
    ReconcileOutcome,
    ReconcileResult,
    RefusalCode,
    RefusedError,
    ResultReceipt,
    RunInspection,
    RunRequest,
    TaskSpec,
    TaskState,
    canonical_json,
    json_schema,
)

from .contracts import PlanArguments
from .plan import DEFAULT_PROJECT_ROOT, RESOLUTION_FLAGS

if TYPE_CHECKING:  # imported for annotations only
    from .authorization import AuthorizationBinding, AuthorizationRecord
    from .contracts import (
        PrepareReport,
        RootBudgetBinding,
        RootBudgetLimits,
        RootBudgetPlan,
        RunPlan,
    )
    from .prepare import Resolution, ResolvedRun

from .controller import Controller, RunOutcome, inspect_run
from .drivers.fake import FakeDriver, FakeScript
from .drivers.acpx_dsh import DriverSetupError
from .drivers.selected import FAKE_ALIASES
from .integrate import (
    IntegrationOutcome,
    apply_integration,
    prepare_integration,
    reconcile_integration,
)
from .contracts import INTEGRATION_ACTIVE_STATES, IntegrationRecord, IntegrationState
from .hints import shell_word as _shell_word  # a printed path, quoted for the operator's shell
from .paths import database_path, default_data_dir
from . import profiles
from .profiles import ENV_PROFILE
from .report import report_json, report_text
from .runtime import controller_build
from .store import RunNotFound, Store, StoredRecordUnreadable, StoreError
from .verify import CheckRunners

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BLOCKED = 3
EXIT_USAGE = 4
#: The run exists and is not finished (DRAFT / READY / RUNNING / CHECKING): a submission that
#: found the run claimed by another owner process (the payload's note names it), or a ``resume``
#: that refused a takeover because that owner may be alive. Also a ``resume`` that refused to act
#: on an ended run because its owner is not proven gone (``RunOutcome.waiting_on_owner``: the
#: reconcile of an ``outcome_unknown`` run, the closure of an ended run's open ledger entries) -
#: that owner and its agent may still be working on it - and a ``resume`` whose store write failed
#: (rolled back; run it again), and any command whose ledger another process holds locked
#: (:func:`_ledger_failure`). Never ``EXIT_REFUSED``, which promises that no run state was created.
EXIT_IN_PROGRESS = 5
#: ``status`` / ``report`` found the run but one of its stored records no longer validates (it was
#: written under another build's contracts, or edited); or any command hit a ledger error it does
#: not map itself - the ledger cannot be used as this build reads it (:func:`_ledger_failure`).
#: Never ``EXIT_REFUSED``; nothing is repaired or rewritten.
EXIT_RECORD_UNREADABLE = 6
#: Ctrl+C reached the command (a ``KeyboardInterrupt`` at the top level): 128 + SIGINT, the shell
#: convention. Not ``EXIT_USAGE`` - the arguments were fine - and not a run outcome: whatever the
#: command recorded before the interrupt is authoritative, and ``hflow status <run-id>`` reads it.
EXIT_INTERRUPTED = 130


def _text_value(value: object) -> str:
    """One value for text output: a string as it is, anything else as compact JSON."""
    return value if isinstance(value, str) else canonical_json(value)


def _text_lines(payload: object) -> str:
    """A payload for a reader: ``key: value`` lines, a list as ``- item`` lines below its key.

    Text mode only; ``--json`` prints the canonical JSON and is unaffected. Keys keep the
    payload's own order, and a nested object (a receipt, an admission issue) stays one compact
    JSON line rather than a Python ``repr``, so nothing is dropped or re-interpreted.
    """
    if not isinstance(payload, dict):
        return str(payload)
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}:" + ("" if value else " (none)"))
            lines.extend(f"  - {_text_value(item)}" for item in value)
        else:
            lines.append(f"{key}: {_text_value(value)}")
    return "\n".join(lines)


def _write_out(payload: object, as_json: bool, path: Path | None = None) -> None:
    text = canonical_json(payload) if as_json else _text_lines(payload)
    if path is None:
        print(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(str(path))


def _given_data_dir(args: argparse.Namespace) -> Path | None:
    """``--data-dir`` when it was given (before or after the verb), else ``None``."""
    return Path(args.data_dir) if getattr(args, "data_dir", None) else None


def _data_dir(args: argparse.Namespace) -> Path:
    """``--data-dir`` when it was given, else the default data directory."""
    return _given_data_dir(args) or default_data_dir()


def _open_store(args: argparse.Namespace) -> Store:
    """The ledger, created (or migrated) when needed: for ``run``, which records a new run."""
    return Store(database_path(_data_dir(args)))


def _existing_store(args: argparse.Namespace) -> Store | None:
    """The ledger, or ``None`` when there is none yet: a read-only view never creates one."""
    path = database_path(_data_dir(args))
    return Store(path) if path.is_file() else None


class UnknownTarget(Exception):
    """A command named a run, integration or invocation the ledger it looked in does not hold.

    Exit :data:`EXIT_USAGE`. The message names the ledger file that was looked in - and whether it
    exists at all - because the usual cause is a ``--data-dir`` that was mistyped or left out, and
    a lookup never creates a ledger to look in.
    """


def unknown_target_message(kind: str, ident: str, ledger: Path) -> str:
    """``unknown <kind> <id>``, the ledger it was looked for in, and the ``--data-dir`` hint."""
    where = (
        f"in the ledger {ledger}"
        if ledger.is_file()
        else f"- there is no ledger at {ledger} (nothing was created)"
    )
    return (
        f"unknown {kind} {ident} {where}. If it was recorded under another data directory, pass "
        "--data-dir <that directory>"
    )


def _lookup_store(args: argparse.Namespace, kind: str, ident: str) -> Store:
    """The existing ledger a lookup command reads, or :class:`UnknownTarget`; never creates one.

    ``status``, ``report``, ``cancel``, ``resume``, ``clean``, ``integrate`` and ``ledger settle``
    act on something a ledger already records, so a mistyped ``--data-dir`` must not leave an
    empty ledger (and its directory) behind and then report the id as unknown in it.
    """
    store = _existing_store(args)
    if store is None:
        raise UnknownTarget(unknown_target_message(kind, ident, database_path(_data_dir(args))))
    return store


def _os_user() -> str:
    """The OS user this process runs as - recorded, never authenticated - or ``unknown``."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser raises OSError/KeyError/ImportError by platform
        return "unknown"


#: The environment every approved check runs with, a run's own and an integration's alike: it
#: keeps a check from scattering caches into the workspace under test - a check should leave
#: evidence, not untracked files that later look like unfrozen changes. It weakens no check.
CHECK_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}


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


#: Every task state maps explicitly; a run outcome is never ``EXIT_REFUSED`` (that code means
#: admission refused and nothing was created). ``CANCELLED`` is not written by this build (an
#: operator stop ends ``BLOCKED`` with ``cancelled_by_operator``); a row that carries it has
#: ended, so it reads as blocked rather than as refused or in progress.
_STATE_EXIT_CODES: dict[TaskState, int] = {
    TaskState.ACCEPTED: EXIT_OK,
    TaskState.BLOCKED: EXIT_BLOCKED,
    TaskState.CANCELLED: EXIT_BLOCKED,
    TaskState.DRAFT: EXIT_IN_PROGRESS,
    TaskState.READY: EXIT_IN_PROGRESS,
    TaskState.RUNNING: EXIT_IN_PROGRESS,
    TaskState.CHECKING: EXIT_IN_PROGRESS,
}


def _outcome_exit_code(outcome: RunOutcome) -> int:
    return _STATE_EXIT_CODES[outcome.task_state]


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """``hflow doctor``: the read-only environment probe (:mod:`hflow.doctor`)."""
    from .doctor import cmd_doctor as doctor

    return doctor(args)


def _resolution_arguments(args: argparse.Namespace) -> PlanArguments:
    """The resolution inputs as given on the command line, with the documented defaults.

    The parser leaves every resolution flag ``None`` when it is absent (so ``run --plan`` can tell
    a flag that was given from a default); the defaults are applied here, once, for both commands.
    The fields are :data:`RESOLUTION_FLAGS`' dests plus the data directory - one list, so a flag
    cannot be added to one and forgotten in the other.
    """
    from .prepare import writes_opted_in

    values = {dest: getattr(args, dest, None) for dest, _flag in RESOLUTION_FLAGS}
    values["project_root"] = values["project_root"] or DEFAULT_PROJECT_ROOT
    # The write opt-in: --allow-writes, else HFLOW_ALLOW_WRITES - the fallback `run --task` has
    # always honoured, and `prepare` shares it so its preview is that run. A plan records the
    # result, and `approve --plan` / `run --plan` use that and never read the environment.
    values["allow_writes"] = bool(values["allow_writes"]) or writes_opted_in()
    return PlanArguments.model_validate({**values, "data_dir": str(_data_dir(args))})


def _plan_command(
    verb: str, plan_path: str | Path, authorization_file: str | Path | None = None
) -> str:
    """``hflow <verb> --plan PLAN`` (with ``--authorization-file FILE``), ready to paste.

    Never ``--data-dir``: a plan names its own data directory, and ``run --plan`` opens it.
    """
    from .hints import command

    words = [verb, "--plan", _shell_word(str(plan_path))]
    if authorization_file:
        words += ["--authorization-file", _shell_word(str(authorization_file))]
    return command(None, *words)


def _resolve_and_report(arguments: PlanArguments) -> tuple[Resolution, PrepareReport]:
    """Resolve ``arguments`` and build the preview of it: the one block ``prepare`` and
    ``approve`` share, so what is approved is what was previewed.

    The root budget file and the explicit repair opt-in are read and validated before anything
    else is resolved; an invalid one refuses with no report printed and nothing dispatched.
    """
    from .prepare import build_prepare_report, resolve_arguments

    resolution = resolve_arguments(arguments)
    report = build_prepare_report(
        resolution.resolved,
        authorization_mode=arguments.authorization_mode,
        root_budget_plan=resolution.root_plan,
    )
    return resolution, report


def _usage_error(command: str, message: str) -> int:
    """A usage error found after parsing: argparse's shape and :data:`EXIT_USAGE`."""
    print(f"hflow {command}: error: {message}", file=sys.stderr)
    return EXIT_USAGE


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

    With `--plan-out` it also writes these inputs and this report as a plan file
    (``hflow.plan``), which `approve --plan` and `run --plan` resolve again instead of repeating
    every flag. A plan is not an approval, and it is written only for a task that would get past
    both gates.
    """
    from .prepare import render_prepare_text

    arguments = _resolution_arguments(args)
    if args.plan_out:
        from .plan import check_plan_target, frozen_arguments

        # A target that exists and is not a plan (the task, an approval, the contract) is refused
        # before anything is resolved or printed; `write_plan` checks again when it writes.
        check_plan_target(Path(args.plan_out))
        # Resolved from the absolute form the plan records, so the report inside the plan is
        # exactly what `approve` and `run` compute from it.
        arguments = frozen_arguments(arguments)
    resolution, report = _resolve_and_report(arguments)
    if args.json:
        print(canonical_json(report.model_dump(mode="json")))
    else:
        print(render_prepare_text(report))
    # "Will this run?" is the question prepare is asked, so anything the run itself would
    # refuse on has to reach the exit code: the admission gate *and* the dispatch gate. A
    # preview that reported success for a task a run refuses would be worse than no preview.
    ready = resolution.resolved.ready_to_dispatch
    if args.plan_out:
        _write_prepared_plan(args, arguments, resolution, report, ready=ready)
    return EXIT_OK if ready else EXIT_REFUSED


def _write_prepared_plan(
    args: argparse.Namespace,
    arguments: PlanArguments,
    resolution: Resolution,
    report: PrepareReport,
    *,
    ready: bool,
) -> None:
    """``prepare --plan-out``: write the plan, or say why none was written.

    The lines follow the report. In ``--json`` mode stdout carries only the report, so they go to
    stderr there.
    """
    from .ids import utc_now
    from .plan import build_plan, write_plan

    stream = sys.stderr if args.json else sys.stdout
    if not ready:
        # The codes on this line itself: in --json mode it is on stderr, apart from the report.
        issues = [*report.admission.issues, *report.dispatch_preconditions]
        codes = ", ".join(dict.fromkeys(issue.code.value for issue in issues))
        print(
            f"plan          not written: `hflow run` would refuse this task "
            f"({codes + '; ' if codes else ''}see above); {args.plan_out} was left as it was",
            file=stream,
        )
        return
    plan = build_plan(
        arguments,
        resolution,
        report,
        created_at=utc_now(),
        controller_build=controller_build(),
    )
    path = write_plan(Path(args.plan_out), plan)
    print(f"plan          {path}", file=stream)
    if plan.prepare_report.authorization.required and not resolution.resolved.is_real_driver:
        # An offline plan with a root budget: `run --plan` needs no approval file - it charges
        # the root to a CLI-minted record that says it is not a user approval
        # (offline_root_authorization). `approve` would record the user's own instead.
        print(
            "plan          offline root run: it reaches no model, so `run --plan` can run it "
            "directly, charging the root to a CLI-minted record labelled as not a user approval",
            file=stream,
        )
        print(f"next: {_plan_command('run', path)}", file=stream)
        print(
            f"next: {_plan_command('approve', path)}  # optional, instead: charge the root to "
            "your own approval, then run --plan",
            file=stream,
        )
    elif plan.prepare_report.authorization.required:
        print(f"next: {_plan_command('approve', path)}", file=stream)
    else:
        print(
            "plan          needs no authorization (offline fake driver, no root budget)",
            file=stream,
        )
        print(f"next: {_plan_command('run', path)}", file=stream)


def cmd_approve(args: argparse.Namespace) -> int:
    """Turn a plan into an authorization artifact with the user's own words. No model, no DB.

    The plan's inputs are resolved again through the function `prepare` used. A plan whose
    binding changed since is refused as stale (exit 2); a plan that needs no authorization is
    said to need none (exit 0, nothing written); a task `run` would refuse is refused here too.
    Then standard input must be an interactive console - piped or redirected input is refused
    unread (exit 2). Only then are the summary and the approval code shown and the two lines
    read: the code typed back, then the approval in the person's own words; a wrong code or an
    empty approval writes nothing (exit 2). Neither is ever an argument. The summary, the code and
    both prompts go to stderr, the stream the prompts use, so redirecting stdout never hides what
    is approved; stdout carries only the result. The artifact is created as a new file: an
    existing file is never overwritten.

    With ``--run``, once all of that succeeded - and only then - the same process continues into
    exactly ``hflow run --plan PLAN`` with the file just written (:func:`_approve_then_run`); the
    exit code is the run's. Nothing about the approval itself changes, and nothing runs when it
    fails.

    With ``--task`` (and the resolution flags ``prepare`` takes) instead of ``--plan``, the plan
    is resolved and written first (:func:`_approve_task_plan`) and then approved exactly as
    ``approve --plan`` approves it: one process for ``prepare --plan-out`` plus ``approve --plan``.
    """
    from .ids import new_id, utc_now
    from .plan import (
        approval_code,
        approval_record,
        ask_approval,
        default_authorization_path,
        require_current,
        require_terminal,
        same_directory,
        write_authorization,
    )
    from .prepare import render_prepare_text

    if args.task:
        opened = _approve_task_plan(args)
    elif args.plan_out:
        return _usage_error("approve", "--plan-out goes with --task: --plan names the plan")
    else:
        conflict = _plan_flag_conflict(args, "approve")
        opened = conflict if conflict is not None else _open_plan(args, "approve")
    if isinstance(opened, int):
        return opened
    plan_path, plan = opened
    arguments = plan.arguments
    resolution, report = _resolve_and_report(arguments)
    require_current(
        plan,
        plan_path,
        resolution,
        approved=False,
        submissions_required=report.authorization.max_top_level_submissions_required,
    )
    # What is being approved and the questions about it share one stream, the one the prompts
    # use: `approve --plan p > log.txt` must not hide the summary while still asking.
    summary_stream = sys.stderr
    run_command = _plan_command("run", plan_path)
    if not report.authorization.required:
        result: dict[str, object] = {
            "authorization_required": False,
            "written": False,
            "detail": (
                "this plan uses the offline fake driver, which reaches no model, and binds "
                "no root budget, so it needs no authorization. Nothing was written"
            ),
            "next": run_command,
        }
        if args.run:
            return _approve_then_run(args, plan_path, result, authorization_file=None)
        _print_approve_result(result, args.json)
        return EXIT_OK
    if not resolution.resolved.ready_to_dispatch:
        # An approval nobody can spend is not worth asking for. The issues are the run's own
        # gates, printed the way `prepare` prints them.
        from .plan import prepare_command

        print(render_prepare_text(report), file=summary_stream)
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "`hflow run` would refuse this plan's task (admission or dispatch gate, listed "
            "above). Nothing was written; fix it and prepare the plan again: "
            + prepare_command(arguments, plan_path),
        )
    default_target = default_authorization_path(plan_path, plan.binding_digest)
    target = Path(os.path.abspath(args.out)) if args.out else default_target
    if target.exists():
        # Checked before asking, so nobody types an approval that cannot be stored; the write
        # itself is still an exclusive create. The existing file may be this plan's approval
        # already (the default name carries the binding digest), so the run that would use it
        # is printed; `run` verifies it like any other and refuses one that does not fit.
        existing = (
            run_command
            if same_directory(target, default_target)
            else _plan_command("run", plan_path, target)
        )
        # Both lines on stderr: a refusal puts nothing on stdout (`approve --run` least of all).
        print(
            f"refused: {target} already exists and an authorization file is never overwritten. "
            "Nothing was written; pass --out with a new path, or run with the existing approval\n"
            f"next: {existing}  # with the existing approval; the run verifies it and refuses "
            "one that does not fit this plan",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    # A person at a terminal, or nothing: piped input is refused before it is read and before the
    # summary or the code is shown.
    require_terminal(sys.stdin)
    pending = report.authorization
    code = approval_code(plan.binding_digest)
    print(render_prepare_text(report), file=summary_stream)
    print(
        f"approving     binding {pending.binding_digest} for up to "
        f"{pending.max_top_level_submissions_required} top-level submission(s)",
        file=summary_stream,
    )
    print(f"approval code {code}", file=summary_stream)
    summary_stream.flush()
    user_text = ask_approval(sys.stdin, summary_stream, code=code)
    record = approval_record(
        plan, user_text, authorization_id=new_id("AUTH"), authorized_at=utc_now()
    )
    written = write_authorization(target, record)
    # `run --plan` finds the file at the default path by itself; one written elsewhere (`--out`)
    # has to be named.
    if not same_directory(written, default_target):
        run_command = _plan_command("run", plan_path, written)
    result = {
        "authorization_required": True,
        "written": True,
        "authorization_file": str(written),
        "authorization_id": record.authorization_id,
        "binding_digest": record.binding_digest(),
        "max_top_level_submissions": record.max_top_level_submissions,
        "next": run_command,
    }
    if args.run:
        return _approve_then_run(args, plan_path, result, authorization_file=str(written))
    _print_approve_result(result, args.json)
    return EXIT_OK


def _approve_task_plan(args: argparse.Namespace) -> tuple[Path, RunPlan]:
    """``approve --task``: resolve the task as ``prepare --plan-out`` does and write its plan.

    Returns ``(plan path, plan)`` for :func:`cmd_approve` to approve exactly as ``approve --plan``
    does - which resolves the plan again and refuses it if it went stale in between. Refused, with
    nothing written: a task ``run`` would refuse (the report is printed, as ``approve --plan``
    prints it); standard input that is not a terminal when the plan needs an approval (before any
    file is written); an existing different plan at the default path, ``<task stem>.plan.json``
    next to the task, unless ``--plan-out`` names where it goes (an older plan there is then
    replaced, as ``prepare --plan-out`` replaces it). A plan already there that is this one is
    kept as it is. The ``plan`` line goes to stderr with the summary; stdout carries the result.
    """
    from .ids import utc_now
    from .plan import (
        build_plan,
        check_plan_target,
        default_plan_path,
        frozen_arguments,
        load_plan,
        require_terminal,
        write_plan,
    )
    from .prepare import render_prepare_text

    arguments = frozen_arguments(_resolution_arguments(args))
    explicit = args.plan_out is not None
    target = check_plan_target(
        Path(args.plan_out) if explicit else default_plan_path(Path(arguments.task))
    )
    resolution, report = _resolve_and_report(arguments)
    if not resolution.resolved.ready_to_dispatch:
        print(render_prepare_text(report), file=sys.stderr)
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "`hflow run` would refuse this task (admission or dispatch gate, listed above). "
            "Nothing was written: no plan and no approval",
        )
    if report.authorization.required:
        require_terminal(sys.stdin)  # before any file is written
    plan = build_plan(
        arguments, resolution, report, created_at=utc_now(), controller_build=controller_build()
    )
    if not explicit and target.exists():
        try:
            existing: RunPlan | None = load_plan(target)
        except RefusedError:
            existing = None
        if existing is not None and (existing.binding_digest, existing.arguments) == (
            plan.binding_digest,
            plan.arguments,
        ):
            print(f"plan          {target} (already this plan; kept as it is)", file=sys.stderr)
            return target, existing
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{target} already holds another plan (other inputs, or the same ones before they "
            "changed), and approve --task never replaces a different plan by default. Nothing "
            f"was written; pass --plan-out {_shell_word(str(target))} to replace it, or --plan-out "
            "with another path",
        )
    path = write_plan(target, plan)
    print(f"plan          {path}", file=sys.stderr)
    return path, plan


def _approve_result_lines(payload: dict[str, object]) -> list[str]:
    """What ``approve`` wrote (or that it needed nothing), as text lines."""
    if payload["written"]:
        return [
            f"authorization {payload['authorization_file']}",
            f"id            {payload['authorization_id']}",
        ]
    return [f"authorization not needed: {payload['detail']}"]


def _print_approve_result(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(canonical_json(payload))
        return
    print("\n".join([*_approve_result_lines(payload), f"next: {payload['next']}"]))


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


def _continuation_before_admission(
    resolved: ResolvedRun, *, data_dir: Path, root_plan: RootBudgetPlan | None,
) -> RunOutcome | None:
    """Return existing-run drift before new-run admission or approval mismatch can hide it.

    This only compares stored admission facts. A match still goes through every normal
    admission and authorization gate; it grants no approval and claims or dispatches nothing.
    An absent ledger is never created for this check, and role labels need no real driver.
    """
    path = database_path(data_dir)
    if not path.exists():
        return None
    store = Store(path)
    try:
        existing = store.find_run_by_spec_digest(resolved.project.project_id, resolved.spec.spec_digest())
        if existing is None or TaskState(existing["task_state"]) in {
            TaskState.ACCEPTED, TaskState.BLOCKED, TaskState.CANCELLED,
        }:
            return None
        run_id = str(existing["run_id"])
        if store.attempts_for(run_id) or store.invocations_for(run_id):
            return None
        root_binding = None
        if root_plan is not None:
            from .authorization import resolve_root_binding

            root_binding = resolve_root_binding(
                project_id=resolved.project.project_id, request=resolved.request(), data_dir=data_dir,
            )
        drivers = {}
        for role in ("implementer", "reviewer"):
            bound = resolved.effective.role(role)
            assert bound is not None
            drivers[role] = _NotOwnedDriver(bound.driver_id)
        controller = Controller(
            store, drivers["implementer"], reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
            controller_build=controller_build(), production=False,
            effective_config=resolved.effective, root_binding=root_binding,
            root_limits=root_plan.limits if root_plan is not None else None,
        )
        problem, _ = controller._continuation_problem(run_id, resolved.request())
        if not problem:
            return None
        return controller._outcome_for(run_id, notes=[
            f"continuation refused: {problem}. Nothing was claimed or dispatched and no allowance was consumed. "
            "Restore the original binding, or cancel this run and submit a new revision."
        ])
    finally:
        store.close()


def _write_run_outcome(
    outcome: RunOutcome,
    args: argparse.Namespace,
    *,
    offline_root_note: str = "",
    steps: list[str] | None = None,
) -> int:
    payload = _outcome_payload(outcome)
    if offline_root_note:
        notes = payload["notes"]
        assert isinstance(notes, list)
        notes.append(offline_root_note)
    if args.json:
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
        if steps:
            from .hints import next_lines

            for line in next_lines(steps):
                print(line)
    return _outcome_exit_code(outcome)


def _run_plan_inputs(
    args: argparse.Namespace,
) -> tuple[PlanArguments, RunPlan | None, Path | None, str | None] | int:
    """``run``'s resolution inputs - from ``--plan``, or from the flags - and its approval file.

    Returns ``(arguments, plan, plan_path, authorization_file)``, or an exit code for a usage
    error. With ``--plan`` every resolution input comes from the plan: a resolution flag given as
    well, or a ``--data-dir`` naming another directory, is a usage error rather than a silent
    override. The plan's data directory becomes this command's, so the run opens the ledger the
    plan's binding names. Without ``--authorization-file`` a plan that needs an approval uses the
    file ``approve`` writes by default for the plan's digest, when it exists.
    """
    if not args.plan:  # the parser requires exactly one of --task and --plan
        return _resolution_arguments(args), None, None, args.authorization_file
    from .plan import default_authorization_path

    conflict = _plan_flag_conflict(args, "run")
    if conflict is not None:
        return conflict
    opened = _open_plan(args, "run")
    if isinstance(opened, int):
        return opened
    plan_path, plan = opened
    args.data_dir = plan.arguments.data_dir
    authorization_file = args.authorization_file
    if not authorization_file and plan.prepare_report.authorization.required:
        candidate = default_authorization_path(plan_path, plan.binding_digest)
        if candidate.exists():
            authorization_file = str(candidate)
            print(f"note: using the approval of this plan at {candidate}", file=sys.stderr)
    return plan.arguments, plan, plan_path, authorization_file


def _plan_flag_conflict(args: argparse.Namespace, command: str) -> int | None:
    """A usage error when a resolution flag is given with ``--plan``, else ``None``.

    Every resolution input comes from the plan, so a flag given as well would be a silent
    override. Named before the plan is opened, so even an unreadable plan gets this answer; the
    command to change an input is printed when the plan can be read.
    """
    from .plan import load_plan, prepare_command

    given = [flag for dest, flag in RESOLUTION_FLAGS if getattr(args, dest, None) is not None]
    if not given:
        return None
    try:
        plan = load_plan(Path(os.path.abspath(args.plan)))
        again = (
            ". To change one, prepare the plan again with it changed: "
            + prepare_command(plan.arguments, Path(os.path.abspath(args.plan)))
        )
    except RefusedError:
        again = ". Re-run `hflow prepare ... --plan-out` to change one"
    return _usage_error(
        command,
        f"{', '.join(given)} cannot be combined with --plan: every resolution input comes "
        f"from the plan{again}",
    )


def _open_plan(args: argparse.Namespace, command: str) -> tuple[Path, RunPlan] | int:
    """``--plan`` for ``approve`` and ``run``: ``(absolute plan path, plan)``, or a usage error.

    Every resolution input comes from the plan, so a ``--data-dir`` naming another directory than
    the plan's is a usage error rather than a silent override.
    """
    from .plan import load_plan, same_directory

    plan_path = Path(os.path.abspath(args.plan))
    plan = load_plan(plan_path)
    if args.data_dir and not same_directory(args.data_dir, plan.arguments.data_dir):
        return _usage_error(
            command,
            f"--data-dir {args.data_dir} names another directory than the plan's "
            f"{plan.arguments.data_dir}; every resolution input comes from the plan",
        )
    return plan_path, plan


def _approve_then_run(
    args: argparse.Namespace,
    plan_path: Path,
    result: dict[str, object],
    *,
    authorization_file: str | None,
) -> int:
    """``approve --run``, after the approval succeeded: report it, then ``run --plan`` it.

    Called only once ``approve`` has done everything it does without ``--run`` - the plan current,
    the code and the approval typed at a terminal, the file created - or found that the plan needs
    no approval. The run is parsed from the argv ``hflow run --plan PLAN`` (plus the file just
    written, and ``--json`` / ``--data-dir`` as given to ``approve``) by the same parser and handed
    to :func:`cmd_run`, so it loads and resolves the plan again, refuses it if it went stale in
    between, verifies the approval, takes the owner lease and prints its ``next:`` lines exactly
    as that command typed next would; a Ctrl+C reaches :func:`main` the same way. The file is
    named explicitly, never looked up again, so a plan re-prepared in between cannot pick up
    another approval - the one just written is refused for it instead. The approval's result line
    says ``running`` rather than ``next:``: the run is not a step left to the operator. With
    ``--json`` stdout carries two JSON lines, the approval's and then the run's.
    """
    result.pop("next", None)
    command = _plan_command("run", plan_path, authorization_file)
    result["running"] = command
    if args.json:
        print(canonical_json(result))
    else:
        print("\n".join([*_approve_result_lines(result), f"running       {command}"]))
    sys.stdout.flush()
    argv = ["--data-dir", args.data_dir] if args.data_dir else []
    argv += ["run", "--plan", str(plan_path)]
    if authorization_file:
        argv += ["--authorization-file", authorization_file]
    if args.json:
        argv.append("--json")
    run_args = build_parser().parse_args(argv)
    return cmd_run(run_args)


@dataclass(frozen=True)
class _RunApproval:
    """What a run is charged to: the approval, the root it is spent against, and its ceilings.

    All ``None`` (and no note) for a rootless offline run, which needs no authorization.
    """

    authorization: AuthorizationRecord | None = None
    root_binding: RootBudgetBinding | None = None
    root_limits: RootBudgetLimits | None = None
    #: Said in the run's output when the run charges a root to a CLI-minted offline record.
    offline_root_note: str = ""


def _run_authorization(
    resolution: Resolution,
    arguments: PlanArguments,
    *,
    authorization_file: str | None,
    plan_path: Path | None,
    is_real_driver: bool,
) -> _RunApproval:
    """The approval a run is charged to, checked before anything is created; or a refusal.

    A real Harness run needs an explicit, bound, single-use authorization artifact. This is
    checked before any credential is read, any workspace is created and any budget or submission
    allowance is consumed, and it refuses rather than falling back to the fake driver. A bare flag
    is deliberately not accepted: the same process that would run the task must not be able to
    authorize itself with a word. The binding covers the *effective configuration*, so approving
    one profile does not approve another. A root budget file extends the same artifact: the
    binding covers the root too, so the ledger records which approval bought each dispatch. An
    offline root run with no artifact is charged to a CLI-minted record that says it is not an
    approval (:func:`offline_root_authorization`).
    """
    resolved, root_plan = resolution.resolved, resolution.root_plan
    if not (is_real_driver or root_plan is not None):
        return _RunApproval()
    from .authorization import load_authorization, verify_authorization
    from .prepare import pending_binding, root_binding_for

    # Derived, not created: the binding names the ledger, and the store creates or migrates it
    # only when the dispatch transaction actually runs.
    root_binding = root_binding_for(resolved) if root_plan is not None else None
    # The same call `prepare` makes for the binding it prints.
    authorization_binding = pending_binding(
        resolved, authorization_mode=arguments.authorization_mode, root_binding=root_binding
    )
    authorization = None
    if authorization_file:
        authorization = load_authorization(Path(authorization_file))
        # Refuses with a specific mismatch list when the artifact covers a different task,
        # project, base commit, driver, execution mode, configuration or root.
        try:
            verify_authorization(authorization, expected=authorization_binding)
        except RefusedError as exc:
            if plan_path is None:
                raise
            # The plan itself is current (checked before), so this approval was written for
            # another plan, or for this one before its inputs changed and it was re-prepared.
            raise RefusedError(
                exc.code,
                f"{exc.message} - this authorization was not issued for plan {plan_path} as "
                "it resolves now. Nothing was dispatched; approve this plan with "
                f"`{_plan_command('approve', plan_path)}`",
            ) from exc
    if root_plan is None:
        return _RunApproval(authorization=authorization)
    if authorization is None:
        # Offline only: a real driver without an artifact was refused before this point. The
        # fake driver reaches no model, so there is no approval to ask for - but the ledger
        # still refuses to charge a root without naming the artifact that bought it, so the run
        # mints one that says exactly what it is.
        minted = offline_root_authorization(binding=authorization_binding, limits=root_plan.limits)
        return _RunApproval(
            authorization=minted,
            root_binding=root_binding,
            root_limits=root_plan.limits,
            offline_root_note=(
                "offline root run: no --authorization-file was given, so this run charged "
                "its dispatches to a CLI-minted record "
                f"({minted.authorization_id}) whose text says it is not a user "
                "approval. It binds the offline fake driver and cannot authorize a real one"
            ),
        )
    root_limits = authorization.root_limits
    # verify_authorization refused unless the artifact carries this exact root, and the
    # contract refuses a root artifact without its ceilings, so this is never None here.
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
    return _RunApproval(
        authorization=authorization, root_binding=root_binding, root_limits=root_limits
    )


def _run_drivers(
    args: argparse.Namespace, resolved: ResolvedRun, *, is_real_driver: bool
) -> dict[str, object]:
    """The driver each role is dispatched to: ``{"implementer": ..., "reviewer": ...}``.

    The fake driver is a scripted stand-in for tests and examples. Its change comes from a plan
    file (``--fake-write-plan``) so an offline run is reproducible from the CLI alone, without a
    test harness. It is explicitly the test driver: it never pretends to be a real Harness
    delivery. Both roles share it, because there is nothing per-role to distinguish offline. A
    real configuration gets each role's own driver, and a launcher that cannot be resolved is a
    refusal with a reason, not a traceback.
    """
    if not is_real_driver:
        script = FakeScript(outcome=InvocationOutcome.COMPLETED, agent_turns=1)
        if args.fake_write_plan:
            writes = profiles.load_json_file(Path(args.fake_write_plan), what="fake write plan")
            if not isinstance(writes, dict):
                raise RefusedError(
                    RefusalCode.INVALID_SPEC,
                    "--fake-write-plan must be a JSON object of path -> text",
                )
            script.write_plan = {str(key): str(value) for key, value in writes.items()}
            script.limitations = [
                "fake driver: no model was invoked; change came from a plan file"
            ]
        offline = FakeDriver(resolved.project_root, script)
        return {"implementer": offline, "reviewer": offline}
    from .prepare import role_drivers

    try:
        return dict(role_drivers(resolved))
    except DriverSetupError as exc:
        # Knowable before anything is claimed, and `prepare` reports the same condition as a
        # dispatch precondition.
        raise RefusedError(
            RefusalCode.NOT_IMPLEMENTED,
            f"the launch for this configuration could not be resolved: {exc}. Nothing "
            "was dispatched and no allowance was consumed.",
        ) from exc


def cmd_run(args: argparse.Namespace) -> int:
    from .plan import require_current
    from .prepare import resolve_arguments, resolve_machine_bindings

    inputs = _run_plan_inputs(args)
    if isinstance(inputs, int):
        return inputs
    arguments, plan, plan_path, authorization_file = inputs
    data_dir = Path(arguments.data_dir)

    # The authorization question is settled before the task is even read: refusing here means a
    # missing approval cannot be confused with a malformed task, and no file is touched first.
    # "Is this a real run?" comes from the resolved machine bindings - the profile when one is
    # named, the command line otherwise - and those same bindings are what the full resolution
    # below uses (it is handed them rather than reading the profile a second time), so the
    # configuration that answered this question is the one that runs.
    machine = resolve_machine_bindings(
        data_dir=data_dir, profile_id=arguments.profile, driver=arguments.driver
    )
    is_real_driver = machine.is_real_driver
    if is_real_driver and not authorization_file:
        if plan is not None:
            # A plan whose inputs moved (an edited task, a profile now set in the environment)
            # is named stale before anything says "approve it": approving a stale plan would only
            # be refused again. Resolving writes nothing.
            assert plan_path is not None
            require_current(
                plan,
                plan_path,
                resolve_arguments(arguments, machine=machine),
                approved=plan.prepare_report.authorization.required,
            )
        message = (
            "this run resolves to a real Harness driver and needs an explicit, bound user "
            "authorization file (--authorization-file). There is no flag that substitutes for "
            "one and no fallback to the fake driver."
        )
        if plan_path is not None:
            message += (
                f" Approve the plan first: {_plan_command('approve', plan_path)}"
            )
        _write_out({"refused": True, "reason": "live_authorization_missing", "detail": message}, args.json)
        print(f"refused: {message}", file=sys.stderr)
        return EXIT_REFUSED
    # The root budget file and the task's explicit repair opt-in are read before the spec is
    # resolved: the policy becomes part of the effective TaskSpec, so the stored spec, its digest
    # and the authorization binding all cover the repair the task asked for. An unreadable or
    # unsupported document refuses before a run row, a workspace or an allowance exists.
    resolution = resolve_arguments(arguments, machine=machine)
    resolved = resolution.resolved

    continuation = _continuation_before_admission(
        resolved, data_dir=data_dir, root_plan=resolution.root_plan
    )
    if continuation is not None:
        return _write_run_outcome(continuation, args)

    if plan is not None:
        # A plan runs what it planned, or nothing: inputs that now resolve to another binding (an
        # edited task or contract, a moved branch, another configuration) refuse here. This is
        # in addition to every check below, not instead of one.
        assert plan_path is not None
        require_current(
            plan, plan_path, resolution, approved=plan.prepare_report.authorization.required
        )

    if not resolved.admission.ok:
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

    approval = _run_authorization(
        resolution,
        arguments,
        authorization_file=authorization_file,
        plan_path=plan_path,
        is_real_driver=is_real_driver,
    )

    store = _open_store(args)
    try:
        drivers = _run_drivers(args, resolved, is_real_driver=is_real_driver)
        controller = Controller(
            store,
            drivers["implementer"],  # type: ignore[arg-type]
            reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
            controller_build=controller_build(),
            # Keep approved checks from scattering caches into the workspace under test: a
            # check should leave evidence, not untracked files that later look like unfrozen
            # changes. This does not weaken any check.
            runners=CheckRunners.offline_default(extra_env=CHECK_ENV),
            data_dir=data_dir,
            authorization=approval.authorization,
            # Batch E1: the root this run is spent against, and the ceilings the approval
            # carried. Both are None for a run with no root budget file, which is the legacy
            # path. The dispatch transaction is what charges them.
            root_binding=approval.root_binding,
            root_limits=approval.root_limits,
            preflight=(
                _zero_model_preflight(drivers) if approval.authorization is not None else None
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
                task=resolved.spec,
                project=resolved.project,
                project_root=resolved.project_root,
                workspace_root=resolved.project_root,
                # The commit this command resolved the base to, once, and checked the approval
                # against: the controller starts the worktree from it instead of resolving the
                # task's ref again after the branch may have moved.
                base_commit=resolved.base_commit,
            )
        )
        steps: list[str] = []
        if not args.json:
            from .hints import next_steps

            steps = next_steps(store, outcome.run_id, project_file=arguments.project)
    finally:
        store.close()

    return _write_run_outcome(
        outcome, args, offline_root_note=approval.offline_root_note, steps=steps
    )


def _unreadable_record(run_id: str, exc: ValueError | StoredRecordUnreadable) -> int:
    """One line for a stored record that no longer validates, with a code that is not ``2``.

    ``EXIT_REFUSED`` promises that no run state exists; this run exists, with attempts and ledger
    charges, so the answer is :data:`EXIT_RECORD_UNREADABLE`. ``cancel`` and ``resume`` read the
    run row instead and keep working on such a run.
    """
    from .listing import unreadable_reason

    print(f"stored record unreadable for run {run_id}: {unreadable_reason(exc)}", file=sys.stderr)
    return EXIT_RECORD_UNREADABLE


def _read_store(args: argparse.Namespace) -> Store:
    """The existing ledger a read-only command reads, with ``args.run_id`` set for ``--last``.

    ``--last`` is the most recently created run in that ledger, and the command says which one
    (``run: <id>``, first - on stderr with ``--json``, so stdout stays one document). Only the
    read-only commands take it: a command that acts on a run is always given its id.
    """
    if not args.last:
        return _lookup_store(args, "run", args.run_id)
    store = _existing_store(args)
    ledger = database_path(_data_dir(args))
    if store is None:
        raise UnknownTarget(
            f"no run for --last: there is no ledger at {ledger} (nothing was created). If runs "
            "were recorded under another data directory, pass --data-dir <that directory>"
        )
    try:
        rows = store.list_runs(limit=1)
    except BaseException:
        store.close()
        raise
    if not rows:
        store.close()
        raise UnknownTarget(f"no run for --last: the ledger {ledger} records no run")
    args.run_id = str(rows[0]["run_id"])
    print(f"run: {args.run_id}", file=sys.stderr if getattr(args, "json", False) else sys.stdout)
    sys.stdout.flush()
    return store


def _drift_root(store: Store, args: argparse.Namespace) -> Path:
    """The workspace ``status``/``report`` compare for drift.

    ``--project-root`` when given; otherwise the project root the run recorded at admission (its
    admission binding, else its workspace provenance) while that directory still exists; otherwise
    the current directory, as before such records existed. An unreadable record falls back the
    same way: choosing a default must never fail the read-only command.
    """
    if args.project_root is not None:
        return Path(args.project_root).resolve()
    recorded = ""
    try:
        binding = store.admission_binding_for(args.run_id)
        if binding is not None:
            recorded = binding.project_root
        else:
            provenance = store.workspace_provenance_for(args.run_id)
            recorded = provenance.project_root if provenance is not None else ""
    except StoredRecordUnreadable:
        recorded = ""
    if recorded and Path(recorded).is_dir():
        return Path(recorded).resolve()
    return Path(".").resolve()


def _inspect_command(args: argparse.Namespace, render: Callable[[RunInspection], str]) -> int:
    """``status`` / ``report``: one read of the run, rendered as text by ``render`` or as JSON.

    An unknown run reaches :func:`main` (exit 4); a stored record that no longer validates is
    :func:`_unreadable_record` (exit 6).
    """
    from .hints import next_lines, next_steps
    from .report import integration_lines

    store = _read_store(args)
    try:
        inspection = inspect_run(store, args.run_id, project_root=_drift_root(store, args))
        integrations = store.integrations_for(inspection.run.run_id)
        steps = [] if args.json else next_steps(store, inspection.run.run_id)
    except (ValidationError, json.JSONDecodeError, StoredRecordUnreadable) as exc:
        return _unreadable_record(args.run_id, exc)
    finally:
        store.close()
    if args.json:
        print(canonical_json(_with_integrations(report_json(inspection), integrations)))
    else:
        print(
            _join_lines(render(inspection), [*integration_lines(integrations), *next_lines(steps)])
        )
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """One run's state (``status R-...``); without a run id, the inbox (:func:`_status_inbox`)."""
    if args.run_id is None:
        return _status_inbox(args)
    from .report import status_text

    args.last = False  # status reads the run it names; ``--last`` is report's and diff's
    return _inspect_command(args, status_text)


def _status_inbox(args: argparse.Namespace) -> int:
    """Every run whose stored facts name something for you, grouped Running / Ready / Attention,
    each with one line of facts and one ``next:`` command (``listing.inbox``).

    Ledger reads and the clock only: no model, no driver, no process probe, no Git, no write, and
    no ledger created when there is none. Exit 0 - whether or not anything needs you - or 6 when a
    run could not be read (named on stderr, and in ``--json`` as an entry with an ``error``), as
    ``runs`` answers.
    """
    from .listing import inbox, inbox_json, render_inbox

    if args.project_root is not None:
        return _usage_error("status", "--project-root reads one run's workspace: name the run id")
    store = _existing_store(args)
    if store is None:
        entries, unreadable = [], []
    else:
        try:
            entries, unreadable = inbox(store)
        finally:
            store.close()
    if args.json:
        print(canonical_json(inbox_json(entries, unreadable)))
    else:
        print(render_inbox(entries, unreadable=bool(unreadable)))
    return _name_unreadable(unreadable)


def cmd_report(args: argparse.Namespace) -> int:
    return _inspect_command(args, report_text)


def _with_integrations(payload: dict[str, object], integrations: list) -> dict[str, object]:
    """The run's integration records, beside - never inside - its own delivery receipt."""
    return {**payload, "integrations": [record.model_dump(mode="json") for record in integrations]}


def _join_lines(text: str, extra: list[str]) -> str:
    return text if not extra else text.rstrip("\n") + "\n" + "\n".join(extra)


# --------------------------------------------------------------------------
# integrate (batch I2) - no model calls, no dispatch, no budget
# --------------------------------------------------------------------------

#: The states in which each integrate subcommand did what it was asked. Anything else is ``3``
#: (the operator has something to decide: a conflict, a failed check, a moved target, a hand-off
#: because the branch is checked out, a ref update that failed), except a record another process
#: is still working on, which is ``5``. ``apply`` succeeds only when the branch contains the commit.
_INTEGRATION_DONE: dict[str, frozenset[str]] = {
    "prepare": frozenset({"ready"}),
    "apply": frozenset({"integrated"}),
    "reconcile": frozenset({"ready", "integrated"}),
}
_INTEGRATION_ACTIVE = frozenset({"preparing", "checking", "applying"})


def _integration_exit(outcome: IntegrationOutcome, command: str) -> int:
    state = outcome.record.state.value
    if state in _INTEGRATION_DONE[command]:
        return EXIT_OK
    if state in _INTEGRATION_ACTIVE:
        return EXIT_IN_PROGRESS
    return EXIT_BLOCKED


def _integration_error(exc: Exception) -> int:
    """One mapping for every integrate subcommand, so no failure escapes as a traceback."""
    from .gitworkspace import GitError
    from .integrate import IntegrationBusy
    from .store import IntegrationConflict, IntegrationNotFound

    if isinstance(exc, IntegrationNotFound):
        print(f"unknown integration: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if isinstance(exc, IntegrationBusy):
        print(f"in progress: {exc}", file=sys.stderr)
        return EXIT_IN_PROGRESS
    if isinstance(exc, (IntegrationConflict, GitError)):
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if isinstance(exc, (StoredRecordUnreadable, ValidationError, json.JSONDecodeError)):
        print(f"stored integration record unreadable: {exc}", file=sys.stderr)
        return EXIT_RECORD_UNREADABLE
    raise exc


def _run_integration(
    store: Store,
    args: argparse.Namespace,
    command: str,
    call: Callable[[], IntegrationOutcome],
    *,
    project_file: str | None = None,
    integration_id: str | None = None,
) -> int:
    """Run one integrate operation on the open ``store``, then print it and what comes next.

    Its failures map through :func:`_integration_error`; anything that does not map there (a
    refusal other than :class:`~hflow.integrate.IntegrationBusy`, an unknown run) re-raises to
    :func:`main`. ``integration_id`` names the record the operation acts on, read first so that
    an integration this call brings to ``integrated`` is told from one that already was
    (:func:`_finish_integration`).
    """
    try:
        before = store.integration(integration_id) if integration_id else None
        outcome = call()
    except Exception as exc:  # noqa: BLE001 - mapped explicitly, anything else re-raised
        return _integration_error(exc)
    return _finish_integration(
        store, args, outcome, command, before=before, project_file=project_file
    )


def _finish_integration(
    store: Store,
    args: argparse.Namespace,
    outcome: IntegrationOutcome,
    command: str,
    *,
    before: IntegrationRecord | None,
    project_file: str | None = None,
    clean: bool | None = None,
) -> int:
    """Print ``outcome`` and what comes next; return ``command``'s exit code for it.

    An integration that reached ``integrated`` in this process (``before`` was not) releases the
    run's own worktree right here, through exactly the guarded cleanup ``hflow clean <run>
    --apply`` runs (:func:`_auto_clean`) - unless ``--keep-worktree``. ``clean`` overrides that
    decision (``integrate <run-id>`` also releases a worktree an earlier integration left). What
    the cleanup did is printed, and the ``next:`` lines are worked out after it; a refused,
    partial or failed cleanup never changes the exit code, which is the integration's.
    """
    integrated = outcome.record.state is IntegrationState.INTEGRATED
    if clean is None:
        clean = integrated and (before is None or before.state is not IntegrationState.INTEGRATED)
    cleanup = _auto_clean(store, args, outcome.record.run_id) if integrated and clean else None
    steps = _integration_next(store, outcome, args, project_file=project_file)
    _print_integration(outcome, args.json, steps, cleanup=cleanup)
    return _integration_exit(outcome, command)


def _auto_clean(store: Store, args: argparse.Namespace, run_id: str) -> dict[str, object]:
    """Release ``run_id``'s worktree once its change reached a branch: ``clean --apply`` in-process.

    The same guards, the same refusals, no force, ``PARTIAL`` reported as such; the cleanup's
    owner label is ``apply_cleanup``'s default. ``--keep-worktree`` skips it. A cleanup that
    raises is reported, never raised: the integration it follows is recorded already.
    """
    if getattr(args, "keep_worktree", False):
        return {
            "applied": False,
            "status": "KEPT",
            "detail": "--keep-worktree: the run's worktree was left in place",
        }
    from .cleanup import apply_cleanup

    try:
        return apply_cleanup(store, run_id)
    except Exception as exc:  # noqa: BLE001 - never changes the integration's outcome
        return {
            "applied": False,
            "status": "ERROR",
            "detail": f"the cleanup could not be completed ({type(exc).__name__}: {exc})",
        }


def _cleanup_lines(result: dict[str, object]) -> list[str]:
    """What the cleanup after an integration did, as text lines (the refusals included)."""
    lines = [f"cleanup       {result.get('status', '')}: {result.get('detail', '')}"]
    plan = result.get("plan")
    if isinstance(plan, dict):
        for item in plan.get("refusals") or []:
            lines.append(f"  refuse      {item['reason']}: {item['detail']}")
    return lines


def _integration_next(
    store: Store,
    outcome: IntegrationOutcome | None,
    args: argparse.Namespace,
    project_file: str | None = None,
) -> list[str]:
    """What the operator runs next after an integrate command (text output only).

    The command's own hand-off when it has one - it alone knows from Git whether the target is
    checked out somewhere - else the run's next steps from stored facts (:mod:`hflow.hints`).
    """
    if outcome is None or args.json:
        return []
    from .hints import next_steps

    return list(outcome.handoff) or next_steps(
        store, outcome.record.run_id, project_file=project_file
    )


def _print_integration(
    outcome: IntegrationOutcome,
    as_json: bool,
    steps: list[str] | None = None,
    *,
    cleanup: dict[str, object] | None = None,
) -> None:
    from .hints import next_lines
    from .report import integration_record_lines

    if as_json:
        payload = outcome.as_dict()
        if cleanup is not None:
            payload["cleanup"] = cleanup
        print(canonical_json(payload))
        return
    lines = integration_record_lines(outcome.record)
    for note in outcome.notes:
        if note != outcome.record.detail:  # the record's own detail line already says it
            lines.append(f"note          {note}")
    if outcome.receipt is not None:
        receipt = outcome.receipt
        lines.append(f"delivery      {receipt.delivery_state.value} ({receipt.basis})")
        for limitation in receipt.limitations:
            lines.append(f"limitation    {limitation}")
    if cleanup is not None:
        lines.extend(_cleanup_lines(cleanup))
    lines.extend(next_lines(list(outcome.handoff) if steps is None else steps))
    print("\n".join(lines))


def _integration_contract(
    store: Store, args: argparse.Namespace, run_id: str
) -> ProjectConfig | None:
    """The project contract an integration of ``run_id`` is checked with: ``--project``, else the
    run's recorded default (``integrate.recorded_project_file``). ``None``, after saying why on
    stderr, when neither resolves: the caller exits ``EXIT_USAGE`` with nothing recorded."""
    from .integrate import recorded_project_file
    from .prepare import load_project_contract

    project_file = Path(args.project) if args.project else recorded_project_file(store, run_id)
    if project_file is None:
        print(
            f"refusing: pass --project for run {run_id}: it recorded no project "
            "contract digest, its admission record cannot be read, or <project "
            "root>/.hflow/project.json is missing, unreadable or no longer the contract the "
            "run was admitted under",
            file=sys.stderr,
        )
        return None
    return load_project_contract(project_file)


def cmd_integrate_prepare(args: argparse.Namespace) -> int:
    """Build and check an integration commit for an accepted run. Never moves a branch."""
    data_dir = _data_dir(args)
    store = _lookup_store(args, "run", args.run_id)
    try:
        # An unknown run is named as one (by `main`), never as a missing --project.
        store.get_run(args.run_id)
        project = _integration_contract(store, args, args.run_id)
        if project is None:
            return EXIT_USAGE
        return _run_integration(
            store,
            args,
            "prepare",
            lambda: prepare_integration(
                store,
                run_id=args.run_id,
                target_branch=args.target,
                project=project,
                runners=CheckRunners.offline_default(extra_env=CHECK_ENV),
                data_dir=data_dir,
                controller_build=controller_build(),
            ),
            project_file=args.project,
        )
    finally:
        store.close()


def cmd_integrate_apply(args: argparse.Namespace) -> int:
    """The operator's approval: move the target branch by compare-and-set, or hand off."""
    applied_by = _os_user()
    store = _lookup_store(args, "integration", args.integration_id)
    try:
        return _run_integration(
            store,
            args,
            "apply",
            lambda: apply_integration(
                store,
                integration_id=args.integration_id,
                expect_target=args.expect_target,
                applied_by=applied_by,
                controller_build=controller_build(),
            ),
            integration_id=args.integration_id,
        )
    finally:
        store.close()


#: The longest ``integrate reconcile --owner-gone --attest`` text: it is recorded inside the run's
#: audit note (``store.record_note``, 1000 characters with the note's own words), so a longer one
#: is refused rather than cut.
INTEGRATION_ATTEST_MAX_CHARS = 500


def _integration_attestation(args: argparse.Namespace, command: str) -> str | None:
    """``attested_by`` for a reconcile (the OS user, with ``--attest``'s words under
    ``--owner-gone``), or ``None`` after saying on stderr why the attestation is refused."""
    from .controller import attestation_problem

    attestation = (args.attest or "").strip()
    if args.owner_gone:
        if not attestation:
            problem: str | None = "--owner-gone needs --attest with what you know and why"
        elif len(attestation) > INTEGRATION_ATTEST_MAX_CHARS:
            problem = (
                f"the attestation is {len(attestation)} characters; {command} records "
                f"at most {INTEGRATION_ATTEST_MAX_CHARS} (in the run's audit note). Shorten it - "
                "nothing is ever cut to fit"
            )
        else:
            problem = attestation_problem(args.attest)
        if problem is not None:
            print(f"refusing: {problem}", file=sys.stderr)
            return None
    attested_by = _os_user()
    if args.owner_gone:
        attested_by = f"{attested_by}: {attestation}"
    return attested_by


def cmd_integrate_reconcile(args: argparse.Namespace) -> int:
    """Settle an integration record from what Git shows; never re-runs an update or a check."""
    attested_by = _integration_attestation(args, "integrate reconcile")
    if attested_by is None:
        return EXIT_USAGE
    store = _lookup_store(args, "integration", args.integration_id)
    try:
        return _run_integration(
            store,
            args,
            "reconcile",
            lambda: reconcile_integration(
                store,
                integration_id=args.integration_id,
                controller_build=controller_build(),
                owner_gone_attested=args.owner_gone,
                attested_by=attested_by,
            ),
            integration_id=args.integration_id,
        )
    finally:
        store.close()


def cmd_integrate_show(args: argparse.Namespace) -> int:
    """Print one integration record and its receipt. Read-only."""
    store = _lookup_store(args, "integration", args.integration_id)
    try:
        record = store.integration(args.integration_id)
        receipt = store.integration_receipt(args.integration_id) if record is not None else None
        outcome = (
            IntegrationOutcome(record=record, receipt=receipt) if record is not None else None
        )
        steps = _integration_next(store, outcome, args)
    except Exception as exc:  # noqa: BLE001 - mapped explicitly, anything else re-raised
        return _integration_error(exc)
    finally:
        store.close()
    if outcome is None:
        print(
            unknown_target_message(
                "integration", args.integration_id, database_path(_data_dir(args))
            ),
            file=sys.stderr,
        )
        return EXIT_USAGE
    _print_integration(outcome, args.json, steps)
    return EXIT_OK


# --------------------------------------------------------------------------
# integrate <run-id>: one re-entrant command for a run's whole integration (ruling 2026-10-16)
# --------------------------------------------------------------------------

#: The parser key of ``hflow integrate <run-id>``. Never typed: a first word after ``integrate``
#: that is a run id rather than a subcommand name selects it (:class:`_IntegrateCommands`).
_INTEGRATE_RUN = "<run-id>"


def _current_integration(
    records: list[IntegrationRecord], target_ref: str | None
) -> IntegrationRecord | None:
    """The integration ``integrate <run-id>`` continues, from the run's records (oldest first).

    The one that reached a branch, when one did; else the newest. With ``--target``, only the
    records for that branch count. ``None``: nothing to continue, so the next step is a prepare.
    """
    if target_ref is not None:
        records = [record for record in records if record.target_ref == target_ref]
    integrated = [record for record in records if record.state is IntegrationState.INTEGRATED]
    if integrated:
        return integrated[-1]
    return records[-1] if records else None


def cmd_integrate_run(args: argparse.Namespace) -> int:
    """``hflow integrate <run-id>``: continue the run's integration from whatever state it is in.

    Every step is a subcommand's own operation, with its refusals and its exit code:

    * no integration yet, or the newest ended ``stale`` / ``interrupted`` / ``failed`` /
      ``superseded``: what ``integrate prepare`` does, the target being ``--target``, else the
      branch of that newest integration, else the task's recorded base branch;
    * ``ready`` - prepared just now or earlier: first what ``integrate reconcile`` does (a hand
      merge after a hand-off is recorded ``integrated``, a moved target ``stale``); still
      ``ready``, then at an interactive terminal the integration is shown with a confirmation
      code to type back, and only the right code applies it - ``integrate apply`` with the tip
      just shown as ``--expect-target`` (compare-and-set; a checked-out branch is handed off with
      the merge to run yourself, and ``next: hflow integrate <run-id>``). Without a terminal it
      stops at ``ready`` (exit 0, as ``prepare``) and says to run it at one;
    * ``preparing`` / ``checking`` / ``applying``: what ``integrate reconcile`` does, with
      ``--owner-gone --attest`` passed through;
    * ``integrated``: reported. ``conflict`` / ``checks_failed``: reported (exit 3) and never
      prepared again - that decision is the operator's (``integrate prepare``).

    Whenever the integration ends ``integrated``, the run's worktree is released as ``clean
    --apply`` would (:func:`_auto_clean`) unless ``--keep-worktree``; that never changes the exit
    code.
    """
    attested_by = _integration_attestation(args, "integrate")
    if attested_by is None:
        return EXIT_USAGE
    store = _lookup_store(args, "run", args.run_id)
    try:
        return _continue_integration(store, args, attested_by=attested_by)
    finally:
        store.close()


def _continue_integration(store: Store, args: argparse.Namespace, *, attested_by: str) -> int:
    from .hints import _PREPARE_AGAIN, hflow_command, word

    run_id = args.run_id
    store.get_run(run_id)  # an unknown run is named as one (by `main`)
    rerun = hflow_command(store, "integrate", word(run_id))
    before: IntegrationRecord | None = None
    try:
        record = _current_integration(
            store.integrations_for(run_id),
            f"refs/heads/{args.target}" if args.target else None,
        )
        if record is None or record.state in _PREPARE_AGAIN:
            prepared = _prepare_for_run(store, args, run_id, record)
            if isinstance(prepared, int):
                return prepared
            outcome, command = prepared, "prepare"
        elif record.state is IntegrationState.READY or record.state in INTEGRATION_ACTIVE_STATES:
            outcome = reconcile_integration(
                store,
                integration_id=record.integration_id,
                controller_build=controller_build(),
                owner_gone_attested=args.owner_gone,
                attested_by=attested_by,
            )
            command, before = "reconcile", record
        else:
            outcome = IntegrationOutcome(
                record=record,
                receipt=store.integration_receipt(record.integration_id),
                notes=[_settled_note(record)],
            )
            command, before = "reconcile", record
    except Exception as exc:  # noqa: BLE001 - mapped explicitly, anything else re-raised
        return _continue_error(store, exc, args)
    if outcome.record.state is IntegrationState.READY:
        return _confirm_and_apply(store, args, outcome, rerun)
    outcome.handoff = _continuation(outcome, rerun)
    return _finish_integration(
        store,
        args,
        outcome,
        command,
        before=before,
        clean=outcome.record.state is IntegrationState.INTEGRATED,
    )


def _settled_note(record: IntegrationRecord) -> str:
    if record.state is IntegrationState.INTEGRATED:
        return "already integrated; nothing was written"
    return (
        f"the newest integration of this run ended {record.state.value}; preparing it again "
        "against the same tip cannot end otherwise, so `integrate <run-id>` does not - whether to "
        "prepare again (`hflow integrate prepare`, for instance once the branch has moved) or to "
        "start a new task revision is your decision. Nothing was written"
    )


def _prepare_for_run(
    store: Store,
    args: argparse.Namespace,
    run_id: str,
    latest: IntegrationRecord | None,
) -> IntegrationOutcome | int:
    """``integrate prepare`` for ``integrate <run-id>``; an exit code when it cannot start."""
    from .hints import integrable, run_facts

    target = args.target or (latest.target_ref.removeprefix("refs/heads/") if latest else "")
    if not target:
        facts = run_facts(store, run_id)
        target = facts.base_branch
        # A run that is not an accepted Git candidate gets prepare's own refusal, which names why.
        if not target and integrable(facts.task_state, facts.delivery_state, facts.git_candidate):
            print(
                f"refusing: pass --target <branch> for run {run_id}: its task's base is not a "
                "local branch of the repository the run recorded (a commit id, or a branch that "
                "no longer exists), so there is no default target. Nothing was recorded",
                file=sys.stderr,
            )
            return EXIT_USAGE
    project = _integration_contract(store, args, run_id)
    if project is None:
        return EXIT_USAGE
    return prepare_integration(
        store,
        run_id=run_id,
        target_branch=target,
        project=project,
        runners=CheckRunners.offline_default(extra_env=CHECK_ENV),
        data_dir=_data_dir(args),
        controller_build=controller_build(),
    )


def _continuation(outcome: IntegrationOutcome, rerun: str) -> list[str]:
    """The hand-off of an ``integrate <run-id>`` outcome: ``integrate <run-id>`` where the
    subcommands would print ``integrate apply`` / ``reconcile`` / ``prepare``.

    Commands the operator runs outside HFlow - the merge of a hand-off - are kept as printed.
    ``[]`` where ``integrate <run-id>`` has nothing to continue (``integrated``, ``conflict``,
    ``checks_failed``): the run's next steps then come from :mod:`hflow.hints`.
    """
    from .hints import _PREPARE_AGAIN, _with_comment

    state = outcome.record.state
    if outcome.handoff:
        return [*(step for step in outcome.handoff if not step.startswith("hflow ")), rerun]
    if state in _PREPARE_AGAIN:
        return [_with_comment(rerun, "prepares again against the branch's current tip")]
    if state in INTEGRATION_ACTIVE_STATES:
        return [
            _with_comment(rerun, "settles it once the process working on it has exited")
        ]
    return []


def _confirm_and_apply(
    store: Store, args: argparse.Namespace, outcome: IntegrationOutcome, rerun: str
) -> int:
    """A ``ready`` integration: at a terminal, the confirmation code and then the apply.

    The facts, the code and the prompt go to stderr - the stream the prompt uses, as for
    ``approve`` - so redirecting stdout never hides what is being confirmed; stdout carries the
    result. The apply binds ``--expect-target`` to the tip shown, never to a fresh read, so a
    branch that moved in between is ``stale``, not silently moved from somewhere else.
    """
    from .hints import _with_comment
    from .integrate import confirmation_code
    from .plan import is_terminal
    from .report import integration_record_lines

    record = outcome.record
    if not is_terminal(sys.stdin):
        outcome.handoff = [
            _with_comment(
                rerun,
                "run it at an interactive terminal: it shows this integration and asks you to "
                "type its confirmation code before anything moves",
            )
        ]
        return _finish_integration(store, args, outcome, "prepare", before=None)
    code = confirmation_code(record)
    stream = sys.stderr
    print("\n".join(integration_record_lines(record)), file=stream)
    print(
        f"integrating   {record.target_ref} {record.target_tip} -> {record.integration_commit} "
        "by compare-and-set; a branch checked out in any worktree is not moved, you get the "
        "merge to run yourself",
        file=stream,
    )
    print(f"confirm code  {code}", file=stream)
    stream.write(f"Type the confirmation code shown above ({len(code)} characters) to integrate: ")
    stream.flush()
    typed = sys.stdin.readline().strip()
    if typed.lower() != code.lower():
        print(
            f"refused: the confirmation code did not match, so nothing was applied and nothing "
            f"was written to {record.target_ref}. Run it again at a terminal and type the code it "
            f"shows once you have read the integration\nnext: {rerun}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        applied = apply_integration(
            store,
            integration_id=record.integration_id,
            expect_target=record.target_tip,
            applied_by=_os_user(),
            controller_build=controller_build(),
        )
    except Exception as exc:  # noqa: BLE001 - mapped explicitly, anything else re-raised
        return _continue_error(store, exc, args)
    applied.handoff = _continuation(applied, rerun)
    return _finish_integration(
        store,
        args,
        applied,
        "apply",
        before=record,
        clean=applied.record.state is IntegrationState.INTEGRATED,
    )


def _continue_error(store: Store, exc: Exception, args: argparse.Namespace) -> int:
    """:func:`_integration_error`, plus the ``integrate <run-id>`` to run again when another
    process may still be working on the integration (exit 5)."""
    from .hints import ATTEST_PLACEHOLDER, _with_comment, hflow_command, word
    from .integrate import IntegrationBusy

    code = _integration_error(exc)
    if isinstance(exc, IntegrationBusy):
        if "--owner-gone" in str(exc) and not args.owner_gone:
            # HFlow cannot tell whether that process runs (another host, no creation time): the
            # attestation is the operator's, never fabricated.
            step = _with_comment(
                hflow_command(
                    store, "integrate", word(args.run_id), "--owner-gone", "--attest",
                    ATTEST_PLACEHOLDER,
                ),
                "only if you know that process has exited; recorded as your attestation, never "
                "as an observation",
            )
        else:
            step = _with_comment(
                hflow_command(store, "integrate", word(args.run_id)),
                "once the process working on it has exited",
            )
        print(f"next: {step}", file=sys.stderr)
    return code


class _NotOwnedDriver:
    """Answers for a run's recorded driver from a process that started none of its invocations.

    ``hflow cancel`` and ``hflow resume`` run in their own process. A driver's handles are memory
    of the process that launched the child, so whatever driver dispatched the run, this process
    can neither stop that child nor observe it: the only true answer to both questions is
    ``unknown``. The id is the *recorded* one, so the run's record names the driver the run
    actually used - never an offline stand-in - and nothing here can start anything.
    """

    def __init__(self, driver_id: str) -> None:
        self.driver_id = driver_id

    def _refuse(self) -> RefusedError:
        return RefusedError(
            RefusalCode.NOT_IMPLEMENTED,
            f"driver {self.driver_id} is only observed from this process; it starts nothing",
        )

    def probe(self, binding: object) -> object:
        raise self._refuse()

    def start(self, request: object) -> object:
        raise self._refuse()

    def cancel(self, invocation_id: str) -> CancellationReceipt:
        return CancellationReceipt(
            invocation_id=invocation_id,
            status="unknown",
            mechanism="none",
            detail=(
                f"this process did not start invocation {invocation_id} and holds no "
                f"{self.driver_id} handle for it; nothing was stopped. The controller that owns "
                "it must stop it, or `hflow resume` reconciles the run once that controller has "
                "exited"
            ),
        )

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        return ReconcileResult(
            invocation_id=invocation_id,
            outcome=ReconcileOutcome.UNKNOWN,
            detail=(
                f"this process did not start invocation {invocation_id} and holds no "
                f"{self.driver_id} handle for it, so it cannot tell whether the work ran; "
                "staying unknown"
            ),
        )


#: The write-once run note an observer leaves when the run's stored effective configuration
#: cannot be decoded (``cancel`` at once; ``resume`` only once it has written something itself).
STORED_CONFIG_UNREADABLE_PREFIX = "stored_config_unreadable: "


def _note_stored_config_unreadable(store: Store, run_id: str) -> None:
    store.record_note(
        run_id,
        f"{STORED_CONFIG_UNREADABLE_PREFIX}effective_config could not be decoded; role driver "
        "identity remains unrecorded and this observer holds no process handle",
        only_if_absent_prefix=STORED_CONFIG_UNREADABLE_PREFIX,
    )


def _observer(store: Store, args: argparse.Namespace, run_id: str) -> tuple[Controller, bool]:
    """A controller for ``cancel`` / ``resume``, answering through each role's recorded driver.

    The roles are labelled from the run's recorded effective configuration. A role recorded as
    the offline fake gets a fresh ``FakeDriver``, which reports ``unknown`` for any invocation it
    did not start itself; every other role - a production driver, or a run whose configuration
    was never recorded - gets :class:`_NotOwnedDriver`. Neither can confirm a stop of a process
    another controller owns, which is the point.

    Writes nothing to the store and reads no workspace: the offline driver here only answers
    ``unknown`` for invocations it did not start, so it is given no project root to work in. The
    second value says the stored configuration could not be decoded, which the caller records
    (:func:`_note_stored_config_unreadable`) when it acts.
    """
    config_unreadable = False
    try:
        effective = store.effective_config_for(run_id)
    except StoredRecordUnreadable:
        effective = None
        config_unreadable = True
    offline = FakeDriver(Path("."))  # never reads it: it starts nothing from this process
    drivers: dict[str, object] = {}
    for role in ("implementer", "reviewer"):
        bound = effective.role(role) if effective is not None else None
        if bound is not None and bound.driver_id in FAKE_ALIASES:
            drivers[role] = offline
        else:
            drivers[role] = _NotOwnedDriver(bound.driver_id if bound is not None else "unrecorded")
    controller = Controller(
        store,
        drivers["implementer"],  # type: ignore[arg-type]
        reviewer_driver=drivers["reviewer"],  # type: ignore[arg-type]
        controller_build=controller_build(),
        effective_config=effective,
    )
    return controller, config_unreadable


def cmd_cancel(args: argparse.Namespace) -> int:
    """Record a stop intent and stop what this process can; print the receipt.

    The text form is the receipt's status word, its detail, and the ``next:`` lines (a stop this
    process could not confirm leaves the run for ``hflow resume`` once its controller is gone).
    """
    from .hints import next_lines, next_steps

    store = _lookup_store(args, "run", args.run_id)
    try:
        # The run row, not the read projection: a stored record status/report cannot read (a
        # malformed dsh_context or repair note) must not stop a run from being stopped.
        run_id = str(store.get_run(args.run_id)["run_id"])
        controller, config_unreadable = _observer(store, args, run_id)
        if config_unreadable:  # cancel records it at once; resume only once it wrote something
            _note_stored_config_unreadable(store, run_id)
        receipt = controller.cancel(run_id)
        steps = [] if args.json else next_steps(store, run_id)
    finally:
        store.close()
    if args.json:
        print(canonical_json(receipt.model_dump(mode="json")))
        return EXIT_OK
    print(receipt.status)
    if receipt.detail:
        print(f"detail        {receipt.detail}")
    for line in next_lines(steps):
        print(line)
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    """Reconcile, take over, or close an ended run's open entries; never re-dispatches.

    Exit code: the run's own (``_outcome_exit_code``; a refused takeover of a live run is ``5`` by
    its state), except ``EXIT_IN_PROGRESS`` (5) whenever ``resume`` refused to act on an ended run
    because its owner is not proven gone and wrote nothing (``RunOutcome.waiting_on_owner``: the
    reconcile of an ``outcome_unknown`` run whose owner may still be alive, or the closure of an
    ended run's open ledger entries) - so a caller never reads "blocked" (3) or "accepted" (0) as
    "resume did it".

    ``--legacy-owner-gone`` mirrors ``hflow ledger settle``'s flag: the operator attests that the
    controller a pre-v6 run recorded (a pid with no host) has exited. It needs ``--attest`` with
    what the operator knows and why (the settle bounds); a blank, oversized or NUL-bearing text,
    or ``--attest`` without the flag, is a usage error (4) and nothing is read or written. The OS
    user is recorded as ``attested_by`` - recorded, not authenticated.

    A store error (``database is locked``, for one) is reported on stderr and exits
    ``EXIT_IN_PROGRESS`` (5) - never a traceback, and never the 3 or 0 that would read as done.
    The failed transaction was rolled back whole (an ended run's entry closure and the
    attestation it records commit together); what was recorded before it is authoritative.

    An undecodable stored effective configuration is noted (``stored_config_unreadable``) only
    when this resume wrote something itself: a resume that wrote nothing - a refusal on the owner
    rule says "Nothing was written" - leaves the run's notes as they were.
    """
    from .controller import LegacyOwnerAttestation, attestation_problem
    from .hints import next_lines, next_steps

    legacy_owner_gone: LegacyOwnerAttestation | None = None
    if args.attest is not None and not args.legacy_owner_gone:
        print(
            "refusing: --attest is read only with --legacy-owner-gone; nothing was done",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.legacy_owner_gone:
        problem = (
            "--legacy-owner-gone needs --attest with what you know and why"
            if args.attest is None
            else attestation_problem(args.attest)
        )
        if problem is not None:
            print(f"refusing: {problem}; nothing was done", file=sys.stderr)
            return EXIT_USAGE
        legacy_owner_gone = LegacyOwnerAttestation(
            attested_by=_os_user().strip() or "unknown", attestation=args.attest
        )
    store = _lookup_store(args, "run", args.run_id)
    try:
        # As in ``cmd_cancel``: resolved from the run row, so an unreadable stored record fails
        # status/report only and never keeps a run from being reconciled.
        run_id = str(store.get_run(args.run_id)["run_id"])
        controller, config_unreadable = _observer(store, args, run_id)
        changes = store.total_changes()
        outcome = controller.resume(run_id, legacy_owner_gone=legacy_owner_gone)
        if config_unreadable and store.total_changes() != changes:
            _note_stored_config_unreadable(store, run_id)
        steps = [] if args.json else next_steps(store, run_id)
    except RunNotFound:
        print(
            unknown_target_message("run", args.run_id, database_path(_data_dir(args))),
            file=sys.stderr,
        )
        return EXIT_USAGE
    except (StoreError, sqlite3.Error) as exc:
        print(
            f"resume failed on a store error ({type(exc).__name__}: {exc}). A failed transaction "
            "is rolled back whole - an ended run's entry closure and the attestation it records "
            "commit together or not at all - and what was recorded before it is authoritative: "
            f"read `hflow status {args.run_id}`, then run `hflow resume` again",
            file=sys.stderr,
        )
        return EXIT_IN_PROGRESS
    finally:
        store.close()
    _write_out(_outcome_payload(outcome), args.json)
    if steps:
        print("\n".join(next_lines(steps)))
    if outcome.waiting_on_owner:
        return EXIT_IN_PROGRESS
    return _outcome_exit_code(outcome)


#: What ``ledger settle --legacy-owner-gone`` appends to the operator's attestation.
LEGACY_OWNER_GONE_SUFFIX = " [operator attests the pre-v6 owner of this run is gone]"


def _stored_attestation_problem(text: str, *, suffix: str) -> str | None:
    """The store's bounds on the attestation as stored, worded for what the operator typed."""
    from .store import ATTESTATION_MAX_CHARS, attestation_bounds_problem

    problem = attestation_bounds_problem(text)
    if problem is None or len(text) <= ATTESTATION_MAX_CHARS or not suffix:
        return problem
    return (
        f"the attestation is {len(text) - len(suffix)} characters and --legacy-owner-gone adds "
        f"{len(suffix)} more; the bound on the stored text is {ATTESTATION_MAX_CHARS}. Shorten it "
        f"to at most {ATTESTATION_MAX_CHARS - len(suffix)} characters (it is a statement, not a "
        "log)"
    )


def cmd_ledger_settle(args: argparse.Namespace) -> int:
    """Close one ``unknown`` / ``launch_unknown`` ledger entry by operator attestation.

    Never calls a model, never dispatches, never changes a run's state. The identity recorded is
    the OS user this process runs as - recorded, not authenticated. Before the store's own guards,
    :func:`hflow.controller.operator_settle_refusal` refuses while the run's owner or the entry's
    recorded child process may still be alive.
    """
    from .controller import attestation_problem, operator_settle_refusal
    from .hints import next_lines, next_steps
    from .store import InvocationNotFound, SettlementRefused

    # The text is checked before the ledger is opened: a placeholder, blank, oversized or
    # NUL-bearing attestation is a usage error (4), as it is for `resume` and `integrate
    # reconcile`, whatever the invocation. The placeholder rule reads the operator's own words,
    # before the flag's suffix is added (a pasted placeholder plus that suffix is still not their
    # words); the bounds read the text that is stored, suffix included, so a text within them is
    # never refused later by the store for a length the operator did not type.
    attestation = args.attest
    if args.legacy_owner_gone:
        attestation = f"{attestation}{LEGACY_OWNER_GONE_SUFFIX}"
    problem = attestation_problem(args.attest) or _stored_attestation_problem(
        attestation, suffix=LEGACY_OWNER_GONE_SUFFIX if args.legacy_owner_gone else ""
    )
    if problem is not None:
        print(f"refusing: {problem}; nothing was done", file=sys.stderr)
        return EXIT_USAGE
    attested_by = _os_user()
    store = _lookup_store(args, "invocation", args.invocation_id)
    try:
        alive = operator_settle_refusal(
            store, args.invocation_id, legacy_owner_gone_attested=args.legacy_owner_gone
        )
        if alive is not None:
            raise SettlementRefused(alive)
        settlement = store.settle_by_operator(
            args.invocation_id,
            settled_as=args.settled_as,
            attested_by=attested_by,
            attestation=attestation,
        )
        usage = store.root_budget_view(settlement.root_id) if settlement.root_id else None
        steps = [] if args.json else next_steps(store, settlement.run_id)
    except InvocationNotFound as exc:
        print(f"unknown invocation: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SettlementRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        store.close()
    statement = (
        "this is an operator attestation, not an observation: what the launch did stays unknown, "
        "billed usage stays unknown, the run's state and outcome are unchanged and nothing was "
        "re-dispatched. A new revision may now reserve against the root, and still needs its own "
        "explicit approval"
    )
    if args.json:
        payload: dict[str, object] = {
            "settlement": settlement.model_dump(mode="json"),
            "root_used_top_level_submissions": usage.used_top_level_submissions if usage else None,
            "root_used_repairs": usage.used_repairs if usage else None,
            "note": statement,
        }
        print(canonical_json(payload))
        return EXIT_OK
    print(
        f"settled       {settlement.invocation_id} (run {settlement.run_id}) as "
        f"{settlement.settled_as}, by operator attestation (not observed)"
    )
    print(f"prior state   {settlement.prior_state} -> operator_settled")
    if settlement.settled_as == "void":
        print(
            f"returned      {settlement.returned_top_level_submissions} top-level submission(s) "
            f"and {settlement.returned_repairs} repair(s) to root {settlement.root_id}; the "
            "authorization's counter and the run's reserved turns are not returned"
        )
    else:
        print("returned      nothing: every counter stays spent")
    if usage is not None:
        print(
            f"root now      submissions used {usage.used_top_level_submissions}/"
            f"{usage.limits.max_top_level_submissions}, repairs used {usage.used_repairs}/"
            f"{usage.limits.max_repairs}"
        )
    print(f"attested_by   {settlement.attested_by} (OS user; recorded, not authenticated)")
    print(f"attested_at   {settlement.attested_at}")
    print(f"note          {statement}")
    for line in next_lines(steps):
        print(line)
    return EXIT_OK


def cmd_clean(args: argparse.Namespace) -> int:
    """Release a run's workspace: preview by default, remove only with --apply.

    ``--apply``, ``--dry-run`` and ``--reconcile`` exclude each other (the parser refuses two).
    """
    from .cleanup import apply_cleanup, plan_cleanup, reconcile_cleanup

    store = _lookup_store(args, "run", args.run_id)
    try:
        if args.reconcile:
            result = reconcile_cleanup(store, args.run_id)
            _write_out(result if args.json else f"{result['status']}: {result['detail']}", args.json)
            return EXIT_OK
        if args.apply:
            result = apply_cleanup(store, args.run_id)
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
            from .hints import hflow_command

            # The ledger this preview read, so the printed command opens the same one.
            print("apply with:   " + hflow_command(store, "clean", plan.run_id, "--apply"))
    return EXIT_OK


def cmd_schema(args: argparse.Namespace) -> int:
    """Print the generated JSON Schema. Generated, never a second hand-written copy.

    ``MachineProfile`` is here because a profile is a document a person writes by hand
    (``<data-dir>/profiles/<id>.json``), and so are ``AuthorizationRecord``
    (``--authorization-file``), ``RootBudgetPlan`` (``--root-budget-file``) and ``RepairPolicy``
    (``--repair-policy-file``). ``PrepareReport``, ``CancellationReceipt`` (``hflow cancel
    --json``) and ``IntegrationRecord`` / ``IntegrationReceipt`` (``hflow integrate ... --json``)
    are here because their output is consumed by scripts. All are generated from the
    same models the loaders validate against.
    """
    from .authorization import AuthorizationRecord
    from .contracts import (
        EffectiveConfig,
        IntegrationReceipt,
        IntegrationRecord,
        MachineProfile,
        PrepareReport,
        RepairPolicy,
        RootBudgetPlan,
    )

    models = {
        "TaskSpec": TaskSpec,
        "ProjectConfig": ProjectConfig,
        "ResultReceipt": ResultReceipt,
        "RunRequest": RunRequest,
        "MachineProfile": MachineProfile,
        "EffectiveConfig": EffectiveConfig,
        "PrepareReport": PrepareReport,
        "AuthorizationRecord": AuthorizationRecord,
        "RootBudgetPlan": RootBudgetPlan,
        "RepairPolicy": RepairPolicy,
        "CancellationReceipt": CancellationReceipt,
        "IntegrationRecord": IntegrationRecord,
        "IntegrationReceipt": IntegrationReceipt,
    }
    payload = {name: json_schema(model) for name, model in models.items()}
    print(canonical_json(payload))
    return EXIT_OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


_DATA_DIR_HELP = "runtime data directory (default: platform data dir, outside any repo)"
_DRIFT_ROOT_HELP = (
    "workspace used for the drift check (default: the project root the run recorded, while it "
    "exists; else the current directory)"
)


def _add_store_args(parser: argparse.ArgumentParser) -> None:
    """`--data-dir` works before and after the verb.

    The top-level parser owns the default (``None``); the per-subcommand copy uses
    ``argparse.SUPPRESS`` so that, when it is not given after the verb, it leaves a value given
    before the verb in place instead of overwriting it. Given in both places, the later one wins.
    """
    parser.add_argument("--data-dir", default=argparse.SUPPRESS, help=_DATA_DIR_HELP)


class _Parser(argparse.ArgumentParser):
    """Usage errors exit ``EXIT_USAGE`` (4), not argparse's own 2 (which is ``EXIT_REFUSED``).

    Subparsers inherit the class (``add_subparsers`` defaults ``parser_class`` to the parent's
    type), so a missing required option, an unknown flag, a bad choice and an unknown subcommand
    all land here. ``--help`` goes through ``exit(0)`` and is unchanged.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


class _RunIdOrSubcommand:
    """``integrate``'s choices: its subcommand names, and any run id (``R-...``, ``ids``)."""

    def __init__(self, names: dict[str, argparse.ArgumentParser]) -> None:
        self._names = names

    def __contains__(self, value: object) -> bool:
        return value in self._names or (isinstance(value, str) and value.startswith("R-"))

    def __iter__(self):  # noqa: ANN204 - the names, for usage and "choose from" messages
        return iter(self._names)


class _IntegrateCommands(argparse._SubParsersAction):
    """``integrate``'s subcommands, and ``integrate <run-id>``.

    A first word that is a run id rather than a subcommand name selects the ``<run-id>`` parser
    (:func:`cmd_integrate_run`) with that word as its run id. Any other word stays a usage error
    that lists the choices.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.choices = _RunIdOrSubcommand(self._name_parser_map)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001,ANN204
        if values and values[0] not in self._name_parser_map:
            values = [_INTEGRATE_RUN, *values]
        super().__call__(parser, namespace, values, option_string)


def _add_keep_worktree(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help=(
            "when the integration ends integrated, keep the run's worktree instead of releasing it "
            "as `hflow clean <run-id> --apply` would (same guards, never forced)"
        ),
    )


def _add_resolution_args(
    parser: argparse.ArgumentParser,
    *,
    plan_alternative: str | None = None,
    task_help: str = "path to task.json",
) -> None:
    """The inputs `prepare` and `run` resolve a task from: one definition for both commands.

    Every one defaults to ``None`` so ``run --plan`` can tell a given flag from an absent one;
    :func:`_resolution_arguments` applies the documented default (``--project-root .``). The
    task file carries its workspace mode and base commit itself (``init task`` writes both), so
    no flag overrides them. With ``plan_alternative`` (the help of ``--plan``)
    exactly one of ``--task`` and ``--plan`` is required, and argparse enforces it.
    """
    if plan_alternative is None:
        parser.add_argument("--task", required=True, default=None, help="path to task.json")
    else:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--task", default=None, help=task_help)
        source.add_argument("--plan", default=None, help=plan_alternative)
    parser.add_argument(
        "--project",
        default=None,
        help="path to the project contract (default: <project-root>/.hflow/project.json)",
    )
    parser.add_argument(
        "--project-root", default=None, help="target project root (workspace; default: .)"
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            f"machine profile id, loaded from <data-dir>/profiles/<id>.json (or {ENV_PROFILE}). "
            "Binds the implementer and the reviewer independently"
        ),
    )
    parser.add_argument(
        "--driver",
        default=None,
        help=(
            "driver id override: 'fake' (offline) or 'acpx-dsh' (the M0-selected transport). "
            "Without --profile the default is 'fake'. With --profile the profile supplies the "
            "per-role bindings and this flag must name the same driver or the run is refused"
        ),
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        default=None,
        help=(
            "let the implementer change the task's scope.write_allow paths (in its isolated "
            "worktree); a task that declares write paths is refused without it. Without the flag "
            "HFLOW_ALLOW_WRITES=1 in the environment counts. A plan records the opt-in, so "
            "`approve --plan` and `run --plan` use the recorded value, not the environment"
        ),
    )
    parser.add_argument(
        "--root-budget-file",
        default=None,
        help=(
            "JSON root budget plan ({'limits': {...}, 'note': '...'}): the run spends against a "
            "root ledger the authorization must bind. `prepare` derives the binding and the ledger "
            "path and creates no database; a real `run` needs an authorization with the same "
            "root, the offline driver mints a labelled offline record instead"
        ),
    )
    # The one explicit repair opt-in, on `prepare` and `run`: a bare RepairPolicy document, not a
    # task file and not an authorization. It becomes part of the effective TaskSpec (and so of
    # `spec_digest` and the authorization binding), so the repair it arms is the repair that was
    # approved; a task that already names a different policy is refused, never silently preferred.
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


# --------------------------------------------------------------------------
# init / runs / diff (batch L): starting files and read-only views for one operator
# --------------------------------------------------------------------------


def _print_scaffold(scaffold) -> int:  # noqa: ANN001 - a scaffold.Scaffold
    print("\n".join(scaffold.lines()))
    return EXIT_OK


def cmd_init_project(args: argparse.Namespace) -> int:
    """Write ``<project-root>/.hflow/project.json``; never overwrites, never a ``fake`` check."""
    from .scaffold import init_project

    return _print_scaffold(
        init_project(
            Path(args.project_root),
            checks=args.check,
            project_id=args.project_id,
            data_dir=_given_data_dir(args),
        )
    )


def cmd_init_profile(args: argparse.Namespace) -> int:
    """Write ``<data-dir>/profiles/<id>.json``: the two-role DSH profile; never overwrites."""
    from .scaffold import init_profile

    return _print_scaffold(init_profile(args.profile_id, data_dir=_given_data_dir(args)))


def cmd_init_task(args: argparse.Namespace) -> int:
    """Write ``<id>.task.json`` with its reuse decision left for the operator; never overwrites."""
    from .scaffold import init_task

    return _print_scaffold(
        init_task(
            args.task_id,
            project_root=Path(args.project_root),
            goal=args.goal or "",
            allow=args.allow,
            project_file=Path(args.project) if args.project else None,
            out=Path(args.out) if args.out else None,
            data_dir=_given_data_dir(args),
        )
    )


def _name_unreadable(unreadable: list) -> int:
    """``runs`` and the inbox: one stderr line per run that could not be read, and exit 6 if any."""
    for item in unreadable:
        print(f"stored record unreadable for run {item.run_id}: {item.reason}", file=sys.stderr)
    return EXIT_RECORD_UNREADABLE if unreadable else EXIT_OK


def cmd_runs(args: argparse.Namespace) -> int:
    """List runs with their first ``next:`` command, from SQLite only; no model, no probe."""
    from .listing import list_runs, render_runs

    store = _existing_store(args)
    if store is None:
        lines, unreadable = [], []
    else:
        try:
            lines, unreadable = list_runs(store, limit=args.limit)
        finally:
            store.close()
    if args.json:
        # A run that cannot be read is an entry too (``run_id`` and ``error``), not only a
        # stderr line: a script reading stdout must not take "not listed" for "not there".
        entries = [line.as_json() for line in lines] + [item.as_json() for item in unreadable]
        print(canonical_json(entries))
    else:
        print(render_runs(lines, unreadable=bool(unreadable)))
    return _name_unreadable(unreadable)


def cmd_diff(args: argparse.Namespace) -> int:
    """Print the Git diff from a run's recorded base to its frozen candidate commit."""
    from .gitworkspace import GitError
    from .listing import (
        UNREADABLE_ERRORS,
        NoCandidate,
        candidate_range,
        diff_header,
        run_diff,
        unreadable_reason,
    )

    store = _read_store(args)
    try:
        rng = candidate_range(store, args.run_id)
    except NoCandidate as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except UNREADABLE_ERRORS as exc:
        print(
            f"stored record unreadable for run {args.run_id}: {unreadable_reason(exc)}",
            file=sys.stderr,
        )
        return EXIT_RECORD_UNREADABLE
    finally:
        store.close()
    try:
        returncode, output, error = run_diff(rng, stat=args.stat)
    except (NoCandidate, GitError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if returncode != 0:
        print(
            f"refused: git diff of run {rng.run_id}'s recorded commits failed: "
            f"{error.strip()[:300]}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print("\n".join(diff_header(rng)))
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(output)  # Git's bytes as Git wrote them
        buffer.flush()
    else:
        sys.stdout.write(output.decode("utf-8", "replace"))
    return EXIT_OK


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of at least 1")
    return value


def _add_read_target(parser: argparse.ArgumentParser) -> None:
    """A read-only command's run: an id, or ``--last`` - exactly one (checked in :func:`main`)."""
    parser.add_argument("run_id", nargs="?", default=None)
    parser.set_defaults(read_parser=parser)
    parser.add_argument(
        "--last",
        action="store_true",
        help=(
            "read the most recently created run (printed first as `run: <id>`) instead of "
            "naming one"
        ),
    )


class _RemovedSpelling(argparse.Action):
    """A spelling that was removed: recognised only to name what replaced it, never an alias.

    Given anywhere on the line, it ends parsing with one stderr line and :data:`EXIT_USAGE`
    before anything opens; it is left out of ``--help``.
    """

    def __init__(self, option_strings: list[str], dest: str, *, replacement: str) -> None:
        super().__init__(
            option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )
        self.replacement = replacement

    def __call__(self, parser, namespace, values, option_string=None) -> NoReturn:  # noqa: ANN001
        parser.exit(
            EXIT_USAGE,
            f"{parser.prog}: error: {self.option_strings[0]} was removed; {self.replacement}\n",
        )


def _verb(
    sub: argparse._SubParsersAction,
    name: str,
    func,  # noqa: ANN001 - a cmd_* function
    help: str,  # noqa: A002 - argparse's own keyword
    *,
    json: bool = True,
    store: bool = True,
) -> argparse.ArgumentParser:
    """One subcommand with the flags most share: ``--json`` and ``--data-dir``."""
    parser = sub.add_parser(name, help=help)
    if json:
        parser.add_argument("--json", action="store_true")
    if store:
        _add_store_args(parser)
    parser.set_defaults(func=func)
    return parser


#: ``hflow --help``: one line; the operator path and the exit codes follow the verb list.
_DESCRIPTION = (
    "Run one coding task through an agent harness under an explicit approval, a budget and the "
    "project's approved checks, and keep the evidence."
)
_EPILOG = """\
operator path:
  hflow doctor                          what this machine can run (no model calls)
  hflow init project | profile | task   write the starting files; never overwrites
  hflow approve --task T --allow-writes --run
                                        resolve the task, save it as a plan, approve it at a
                                        terminal (type the code, then your own words), run it;
                                        --allow-writes lets the task change its files
                                        (step by step: prepare --plan-out P, approve --plan P,
                                        run --plan P)
  hflow status                          the inbox: every run that needs you, each with its
                                        next: command (hflow status R for one run)
  hflow integrate R                     take run R's accepted candidate onto its branch, at a
                                        terminal; then the run's worktree is released

A run reaches the real Harness (acpx-dsh) only under a machine profile and an approval; without
one it runs offline with the fake driver.

exit codes: 0 done, 2 refused (nothing created), 3 blocked, 4 usage, 5 in progress or another
process holds the ledger, 6 a stored record or the ledger cannot be read, 130 interrupted.
Each command's own --help says more; docs/operations.md is the operator guide.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="hflow",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = _verb(sub, "doctor", cmd_doctor, "read-only environment probe (no model calls)")
    doctor.add_argument(
        "--profile",
        default=None,
        help=(
            f"machine profile id to resolve and report on (or {ENV_PROFILE}); without one, "
            "doctor says so instead of guessing a binding"
        ),
    )

    prepare = _verb(
        sub, "prepare", cmd_prepare,
        "zero-model resolution: what a task would run, under which config, for how much",
    )
    _add_resolution_args(prepare)
    prepare.add_argument(
        "--plan-out",
        default=None,
        help=(
            "also write these inputs (paths made absolute) and this report as a plan file, for "
            "`hflow approve --plan` and `hflow run --plan`; an existing plan file there is "
            "replaced, any other existing file is refused and left as it was. Not written when "
            "the run would be refused. A plan is not an approval"
        ),
    )

    approve = _verb(
        sub, "approve", cmd_approve,
        "turn a plan - or, with --task, a task resolved and saved as a plan first - into an "
        "authorization file, at an interactive terminal only: type the approval code it shows, "
        "then your own approval text (no model calls, no database)",
    )
    _add_resolution_args(
        approve,
        plan_alternative=(
            "plan file written by `prepare --plan-out`, instead of --task: every resolution input "
            "comes from it, so no other resolution flag may be given with it"
        ),
        task_help=(
            "path to task.json, instead of --plan: resolve it with the flags below as `prepare` "
            "does, write the plan (see --plan-out), then approve that plan"
        ),
    )
    approve.add_argument(
        "--plan-out",
        default=None,
        help=(
            "with --task: the plan file to write (an older plan there is replaced). Default: "
            "<task stem>.plan.json next to the task, where a different existing plan is refused"
        ),
    )
    approve.add_argument(
        "--out",
        default=None,
        help=(
            "authorization file to create (never overwritten); default: next to the plan, "
            "<plan stem>.<12 hex of the binding digest>.authorization.json"
        ),
    )
    approve.add_argument(
        "--run",
        action="store_true",
        help=(
            "after the approval is written (the code and your approval still typed at the "
            "terminal), continue in this process with exactly `hflow run --plan PLAN` using that "
            "file; the exit code is the run's. Nothing runs when the approval fails"
        ),
    )

    run = _verb(sub, "run", cmd_run, "admit and run one task")
    _add_resolution_args(
        run,
        plan_alternative=(
            "plan file written by `prepare --plan-out`, instead of --task: every resolution input "
            "comes from it, so no other resolution flag may be given with it. Without "
            "--authorization-file the approval `hflow approve --plan` wrote by default for this "
            "plan is used when it exists"
        ),
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

    status = _verb(
        sub, "status", cmd_status,
        "with a run id, that run's state; without one, every run that needs you, grouped "
        "Running / Ready / Attention, each with its next command (SQLite only: no model calls, "
        "no process probe)",
    )
    status.add_argument(
        "run_id", nargs="?", default=None, help="the run to show; leave it out for the inbox"
    )
    status.add_argument(
        "--last",
        action=_RemovedSpelling,
        replacement=(
            "read the newest run with `hflow report --last`, or name it: `hflow status <run-id>`"
        ),
    )
    status.add_argument("--project-root", default=None, help=_DRIFT_ROOT_HELP)

    report = _verb(
        sub, "report", cmd_report, "render a run's receipt and evidence (no model calls)"
    )
    _add_read_target(report)
    report.add_argument("--project-root", default=None, help=_DRIFT_ROOT_HELP)

    cancel = _verb(sub, "cancel", cmd_cancel, "cancel a run's live attempt")
    cancel.add_argument("run_id")

    resume = _verb(
        sub, "resume", cmd_resume,
        "reconcile a run, or take a live one over from a dead owner (never re-dispatches)",
    )
    resume.add_argument("run_id")
    resume.add_argument(
        "--legacy-owner-gone",
        action="store_true",
        help=(
            "for an ended run written before owner identity existed that left ledger entries "
            "open: attest that the controller process it recorded has exited (HFlow cannot check "
            "it; recorded as your attestation, never as an observation). Needs --attest"
        ),
    )
    resume.add_argument(
        "--attest",
        default=None,
        help="with --legacy-owner-gone: what you know and why (required, at most 2000 characters)",
    )

    _verb(
        sub, "schema", cmd_schema, "print generated JSON Schema for the data contracts",
        json=False, store=False,
    )

    clean = _verb(
        sub, "clean", cmd_clean,
        "release a run's workspace: preview by default, remove with --apply",
    )
    clean.add_argument("run_id")
    # One of three things, or the preview: argparse refuses any two together (exit 4).
    clean_mode = clean.add_mutually_exclusive_group()
    clean_mode.add_argument(
        "--apply", action="store_true", help="actually remove the run's worktree"
    )
    clean_mode.add_argument(
        "--dry-run", action="store_true", help="explicit synonym for the default preview"
    )
    clean_mode.add_argument(
        "--reconcile",
        action="store_true",
        help="after an interruption, decide this run's workspace state from recorded facts",
    )

    ledger = sub.add_parser("ledger", help="operator actions on the dispatch ledger (no model calls)")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    settle = _verb(
        ledger_sub, "settle", cmd_ledger_settle,
        "close an unknown / launch_unknown ledger entry by operator attestation; never "
        "re-dispatches and never changes the run's state",
    )
    settle.add_argument("invocation_id")
    settle.add_argument(
        "--as",
        dest="settled_as",
        choices=["consumed", "void"],
        default="consumed",
        help=(
            "consumed (default): every counter stays spent. void: launch_unknown only - returns "
            "this dispatch's root charge"
        ),
    )
    settle.add_argument(
        "--attest",
        required=True,
        help="what the operator attests and why (required, at most 2000 characters)",
    )
    settle.add_argument(
        "--legacy-owner-gone",
        action="store_true",
        help=(
            "for a run written before owner identity existed: attest that the controller "
            "process it recorded has exited (HFlow cannot check it; recorded with the settlement)"
        ),
    )

    integrate = sub.add_parser(
        "integrate",
        help=(
            "integrate an accepted candidate into a local branch: `integrate <run-id>` continues "
            "it from whatever state it is in; or step by step, prepare (build + check), apply "
            "(compare-and-set), reconcile, show. No model calls"
        ),
    )
    integrate_sub = integrate.add_subparsers(
        dest="integrate_command", required=True, action=_IntegrateCommands
    )
    integrate_run = integrate_sub.add_parser(
        _INTEGRATE_RUN,
        prog="hflow integrate",
        help=(
            "continue run <run-id>'s integration from whatever state it is in: prepare it, show "
            "it at an interactive terminal and apply it once you type its confirmation code (a "
            "checked-out branch is handed to you to merge), record that merge when run again, "
            "then release the run's worktree"
        ),
    )
    integrate_run.add_argument("run_id", metavar="run-id")
    integrate_run.add_argument("--json", action="store_true")
    _add_store_args(integrate_run)
    integrate_run.add_argument(
        "--target",
        default=None,
        help=(
            "an existing local branch name. Default: the branch of the run's newest integration, "
            "else the task's base branch when that is a local branch"
        ),
    )
    integrate_run.add_argument(
        "--project",
        default=None,
        help="as for `integrate prepare` (used only when an integration is prepared)",
    )
    integrate_run.add_argument(
        "--owner-gone",
        action="store_true",
        help="as for `integrate reconcile`: attest that an active integration's process has exited",
    )
    integrate_run.add_argument(
        "--attest",
        default="",
        help="what you know and why (required with --owner-gone, at most 500 characters)",
    )
    _add_keep_worktree(integrate_run)
    integrate_run.set_defaults(func=cmd_integrate_run)
    integrate_prepare = _verb(
        integrate_sub, "prepare", cmd_integrate_prepare,
        "build one integration commit on the target's current tip and run the run's approved "
        "checks on it in a worktree of its own; never moves the branch",
    )
    integrate_prepare.add_argument("run_id")
    integrate_prepare.add_argument(
        "--target", required=True, help="an existing local branch name, e.g. main"
    )
    integrate_prepare.add_argument(
        "--project",
        default=None,
        help=(
            "the project contract the run was accepted under: its checks digest, and for a run "
            "that recorded one its whole contract digest, must match. Default: <project "
            "root>/.hflow/project.json when it still digests to the contract the run recorded"
        ),
    )
    integrate_apply = _verb(
        integrate_sub, "apply", cmd_integrate_apply,
        "move the target branch to a ready integration commit by compare-and-set; a branch "
        "checked out in any worktree is never moved (you get the merge command instead)",
    )
    integrate_apply.add_argument("integration_id")
    integrate_apply.add_argument(
        "--expect-target",
        required=True,
        help="the full target tip the integration was checked against (printed by prepare)",
    )
    _add_keep_worktree(integrate_apply)
    integrate_reconcile = _verb(
        integrate_sub, "reconcile", cmd_integrate_reconcile,
        "settle an integration from what Git shows (an interrupted apply, a hand merge, a "
        "moved target); never re-runs the update or a check",
    )
    integrate_reconcile.add_argument("integration_id")
    integrate_reconcile.add_argument(
        "--owner-gone",
        action="store_true",
        help=(
            "attest that the process recorded on an active integration has exited when HFlow "
            "cannot tell (another host, no creation time); recorded as an attestation, never as "
            "an observation. A process HFlow sees running is never overridden"
        ),
    )
    integrate_reconcile.add_argument(
        "--attest",
        default="",
        help="what you know and why (required with --owner-gone, at most 500 characters)",
    )
    _add_keep_worktree(integrate_reconcile)
    integrate_show = _verb(
        integrate_sub, "show", cmd_integrate_show, "print one integration record"
    )
    integrate_show.add_argument("integration_id")

    init = sub.add_parser(
        "init",
        help=(
            "write one starting file that does not exist yet - a project contract, a machine "
            "profile or a task; never overwrites one (no model calls, no database)"
        ),
    )
    init_sub = init.add_subparsers(dest="init_command", required=True)
    init_project = _verb(
        init_sub, "project", cmd_init_project,
        "write <project-root>/.hflow/project.json with kind=command checks inferred from the "
        "repository (pytest, npm test) or named with --check",
        json=False,
    )
    init_project.add_argument(
        "--project-root", default=".", help="the repository to describe (default: .)"
    )
    init_project.add_argument(
        "--check",
        action="append",
        default=None,
        metavar="ID=COMMAND",
        help=(
            "an approved check, repeatable; replaces inference. COMMAND is split like a POSIX "
            "shell line, or read as a JSON array of strings when it starts with '['; it runs "
            "without a shell"
        ),
    )
    init_project.add_argument(
        "--project-id", default=None, help="project_id (default: the project root's name)"
    )
    init_profile = _verb(
        init_sub, "profile", cmd_init_profile,
        "write <data-dir>/profiles/<id>.json: implementer and reviewer bound to DSH over "
        "acpx-dsh, model_selection native_profile",
        json=False,
    )
    init_profile.add_argument("profile_id", metavar="id")
    init_task = _verb(
        init_sub, "task", cmd_init_task,
        "write <id>.task.json: acceptance wired to the contract's command checks, an isolated "
        "worktree at the current branch, and the reuse decision left for you (admission refuses "
        "the task until you make it)",
        json=False,
    )
    init_task.add_argument("task_id", metavar="id")
    init_task.add_argument("--goal", default=None, help="what the change must achieve")
    init_task.add_argument(
        "--allow",
        action="extend",
        nargs="+",
        default=None,
        metavar="PATH",
        help="a literal file or directory path the worker may change (scope.write_allow)",
    )
    init_task.add_argument(
        "--project-root", default=".", help="the repository the task runs in (default: .)"
    )
    init_task.add_argument(
        "--project",
        default=None,
        help=(
            "the project contract to wire checks from (default: "
            "<project-root>/.hflow/project.json)"
        ),
    )
    init_task.add_argument(
        "--out", default=None, help="the task file to create (default: ./<id>.task.json)"
    )

    runs = _verb(
        sub, "runs", cmd_runs,
        "list recent runs and the next command for each, from SQLite (no model calls, no "
        "process probe)",
    )
    runs.add_argument(
        "--limit", type=_positive_int, default=20, help="how many runs to list (default: 20)"
    )
    runs.add_argument(
        "--needs-you",
        action=_RemovedSpelling,
        replacement="what needs you is `hflow status` (no run id)",
    )

    diff = _verb(
        sub, "diff", cmd_diff,
        "print the Git diff from a run's recorded base commit to its frozen candidate commit "
        "(never a working tree; the Git identity, not the content fingerprint)",
        json=False,
    )
    _add_read_target(diff)
    diff.add_argument("--stat", action="store_true", help="print git's --stat summary instead")

    return parser


def forbid_current_directory_program_search() -> None:
    """Stop Windows from running a program it finds in this process's current directory.

    ``CreateProcess`` given a bare program name - ``subprocess.run(["git", ...])`` - searches the
    *parent* process's current directory before ``PATH``, unless the parent's environment holds
    ``NoDefaultCurrentDirectoryInExePath``. HFlow starts bare ``git`` for every worktree, freeze
    and integration step (``gitworkspace``) and runs an approved check's argv exactly as approved
    (``verify``), so whoever can write the directory ``hflow`` is started in - typically a
    checkout - could plant ``git.exe`` there and have it run with this process's rights. That was
    observed on Windows 11 with Python 3.14.7: a planted ``git.exe`` ran instead of Git until
    the variable was set in the parent. Children started with this process's environment inherit
    it; the driver sets it again on every agent child (``acpx_dsh.child_environment``).

    Set in exactly that spelling after every other spelling is removed, as the driver does. It is
    process-wide on purpose and harmless off Windows, where nothing reads it.
    """
    from .drivers.acpx_dsh import NO_CWD_EXE_SEARCH_ENV

    for key in [key for key in os.environ if key.upper() == NO_CWD_EXE_SEARCH_ENV.upper()]:
        del os.environ[key]
    os.environ[NO_CWD_EXE_SEARCH_ENV] = "1"


#: What a Ctrl+C that reaches the top level prints. The process stopped where it was; it did not
#: roll anything back, so the recorded state is the answer, not this message.
INTERRUPTED_MESSAGE = (
    "interrupted: this command stopped where it was. What it recorded before the interrupt is "
    "authoritative - read it with `hflow status <run-id>` (or `hflow integrate show "
    "<integration-id>`); nothing was retried"
)


def main(argv: list[str] | None = None) -> int:
    # First, before anything can start a process (see the function's docstring).
    forbid_current_directory_program_search()
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "last") and (args.run_id is None) == (not args.last):
        # status / report / diff read one run: an id or --last, exactly one. A usage error like
        # any other argument error (exit 4, with the subcommand's usage), before anything opens.
        args.read_parser.error("name a run id, or pass --last for the newest run (not both)")
    try:
        return int(args.func(args))
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except UnknownTarget as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except RunNotFound as exc:  # every command but `resume`, which maps it before StoreError
        ledger = database_path(_data_dir(args))
        print(unknown_target_message("run", str(exc), ledger), file=sys.stderr)
        return EXIT_USAGE
    except (StoreError, sqlite3.Error) as exc:
        return _ledger_failure(exc)
    except KeyboardInterrupt:
        print(INTERRUPTED_MESSAGE, file=sys.stderr)
        return EXIT_INTERRUPTED
    # No catch-all for pydantic's ValidationError here: the input loaders raise RefusedError
    # themselves, and mapping any other one to EXIT_REFUSED would tell a caller "no run state"
    # about a run that exists (review G, R12). status/report handle an unreadable stored record.


def _is_locked(exc: BaseException) -> bool:
    """Whether ``exc`` (or what it wraps) is SQLite's "database is locked" / busy answer."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, sqlite3.OperationalError) and "locked" in str(seen).lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _ledger_failure(exc: StoreError | sqlite3.Error) -> int:
    """One stderr line for a ledger error no command mapped itself; never a traceback.

    ``database is locked``: another process holds the ledger - :data:`EXIT_IN_PROGRESS`, as
    ``resume`` answers it; run the command again. Anything else - a storage version newer than
    this build, a file that is not a ledger, a write the ledger refused (an authorization id
    already recorded with another binding) - is :data:`EXIT_RECORD_UNREADABLE`: what is stored
    cannot be used as this build reads it, and nothing was repaired. A failed transaction is
    rolled back whole, so what was recorded before it stands.
    """
    if _is_locked(exc):
        print(
            f"in progress: the ledger is locked by another process ({type(exc).__name__}: {exc}). "
            "Nothing more was written; run the command again once that process is done",
            file=sys.stderr,
        )
        return EXIT_IN_PROGRESS
    print(
        f"ledger error ({type(exc).__name__}): {str(exc).splitlines()[0] if str(exc) else exc!r}. "
        "Any write it interrupted was rolled back whole; what was recorded before it stands",
        file=sys.stderr,
    )
    return EXIT_RECORD_UNREADABLE


if __name__ == "__main__":
    raise SystemExit(main())
