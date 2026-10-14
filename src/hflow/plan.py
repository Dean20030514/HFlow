"""Saved plans: ``prepare --plan-out``, ``approve --plan`` and ``run --plan``.

Without a plan, a real task needs the operator to copy ``authorization.binding`` out of
``prepare --json`` into a hand-built artifact and then repeat every ``prepare`` flag, identically,
to ``run``. A plan writes the resolution inputs and the preview down once:

* ``prepare --plan-out PLAN`` records the inputs (paths made absolute) and the full report; it
  replaces an older plan at ``PLAN`` and refuses to replace any other file;
* ``approve --plan PLAN`` resolves those inputs again through the same function, refuses a plan
  whose binding changed since (stale), and then asks a person at an interactive terminal: it
  shows the summary and an *approval code* (the first 8 hex of the binding digest), reads that
  code back, then reads the approval in the person's own words, and only then writes an
  authorization artifact;
* ``run --plan PLAN`` takes every resolution input from the plan and checks the approval exactly
  as a run with flags does.

Approving stays a separate human step. Nothing is taken from an argument, and a standard input
that is not a terminal (a pipe, a file) is refused without being read, so ``echo ok | hflow
approve`` approves nothing; a wrong code - ``no``, ``y``, an empty line - approves nothing either.
This is a deliberate-act gate, not authentication: a process that drives a pseudo-terminal and
reads the code off the screen can still type both lines, which is the provenance limit
``docs/operations.md`` states for every authorization artifact. A plan is not an authorization: it
carries no ``user_text`` and no ``provided_by``, and ``AuthorizationRecord`` forbids its fields,
so a plan handed to ``--authorization-file`` is refused. Nothing here calls a model or opens the
ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from pydantic import ValidationError

from .contracts import (
    PlanArguments,
    PrepareReport,
    RefusalCode,
    RefusedError,
    RootBudgetLimits,
    RunPlan,
)
from .prepare import Resolution, load_json_file, pending_binding, root_binding_for
from .profiles import requested_profile_id, summarize_validation_error

if TYPE_CHECKING:  # annotations only
    from .authorization import AuthorizationBinding, AuthorizationRecord

#: The stem suffix a plan file conventionally carries (``task.plan.json``); dropped from the
#: default authorization file name so that reads ``task.<digest>.authorization.json``.
PLAN_STEM_SUFFIX = ".plan"


def frozen_arguments(
    arguments: PlanArguments, env: Mapping[str, str] | None = None
) -> PlanArguments:
    """``arguments`` as a plan records them: every path absolute, the selected profile named.

    Paths are made absolute against this process's working directory, so ``approve`` and ``run``
    find the same files from anywhere. The profile is the one this resolution selected
    (``--profile``, else ``HFLOW_PROFILE``) and is recorded by name, so once a plan names one a
    later ``HFLOW_PROFILE`` is not consulted (``--profile`` beats it). A plan that selected *no*
    profile records ``None``, and then a later ``HFLOW_PROFILE`` does change what ``approve`` and
    ``run --plan`` resolve. That is not a way round the plan: the binding names the profile, so the
    new resolution is refused as a stale plan, or - an offline plan now resolving to a real driver
    - as a run with no authorization. ``base_commit`` stays as given: a ref resolves again, and a
    moved branch then shows as a changed binding.
    """

    def absolute(value: str | None) -> str | None:
        return os.path.abspath(value) if value else value

    return arguments.model_copy(
        update={
            "task": os.path.abspath(arguments.task),
            "project": absolute(arguments.project),
            "project_root": os.path.abspath(arguments.project_root),
            "profile": requested_profile_id(arguments.profile, env) or None,
            "root_budget_file": absolute(arguments.root_budget_file),
            "repair_policy_file": absolute(arguments.repair_policy_file),
            "data_dir": os.path.abspath(arguments.data_dir),
        }
    )


def resolution_binding(resolution: Resolution, *, authorization_mode: str) -> AuthorizationBinding:
    """The binding this resolution amounts to - for an approval, or for a staleness check.

    For a plan that needs an approval it is the binding ``prepare`` prints and ``run`` verifies;
    for an offline plan it is computed the same way, so an edited task, contract or base commit
    makes an offline plan stale too.
    """
    resolved = resolution.resolved
    return pending_binding(
        resolved,
        authorization_mode=authorization_mode,
        root_binding=root_binding_for(resolved) if resolution.root_plan is not None else None,
    )


def build_plan(
    arguments: PlanArguments,
    resolution: Resolution,
    report: PrepareReport,
    *,
    created_at: str,
    controller_build: str,
) -> RunPlan:
    binding = resolution_binding(resolution, authorization_mode=arguments.authorization_mode)
    return RunPlan(
        created_at=created_at,
        controller_build=controller_build,
        arguments=arguments,
        prepare_report=report,
        binding=binding.model_dump(mode="json"),
        binding_digest=binding.digest(),
        root_limits=resolution.root_plan.limits if resolution.root_plan is not None else None,
    )


def check_plan_target(path: Path) -> Path:
    """``path`` made absolute, or a refusal when writing a plan there would destroy another file.

    :func:`write_plan` replaces its target whole, so ``--plan-out task.json`` - or an
    authorization file, or the project contract - would silently overwrite the file it names. A
    plan may therefore only go where nothing exists yet or where an older plan is: a JSON object
    whose ``kind`` is ``"hflow-plan"`` (any schema version - a stale or outdated plan is exactly
    what re-preparing replaces). A directory, a file that cannot be read as JSON, and every other
    document are refused and left as they were.
    """
    target = Path(os.path.abspath(path))
    if not os.path.lexists(target):
        return target

    def refused(why: str) -> RefusedError:
        return RefusedError(
            RefusalCode.INVALID_SPEC,
            f"--plan-out {target} {why}; a plan only ever replaces an older plan. Nothing was "
            "written and the file was left as it was; choose another path for the plan",
        )

    if target.is_dir():
        raise refused("is a directory")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise refused(
            f"exists and could not be read as an HFlow plan ({type(exc).__name__})"
        ) from exc
    if not isinstance(raw, dict) or raw.get("kind") != "hflow-plan":
        raise refused('exists and is not an HFlow plan (no "kind": "hflow-plan")')
    return target


def write_plan(path: Path, plan: RunPlan) -> Path:
    """Write ``plan`` to ``path`` atomically; only an older plan there is ever replaced.

    The text goes to a temporary file in the same directory first and is moved over ``path`` in
    one ``os.replace``, so a reader never sees half a plan. Replacing a plan is allowed on purpose:
    a plan is a preview, and re-preparing is how a stale one is refreshed. Any other existing file
    is refused (:func:`check_plan_target`). Approvals are never replaced
    (:func:`write_authorization`).
    """
    target = check_plan_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(plan.model_dump_json(indent=2) + "\n")
        os.replace(handle.name, target)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return target


def load_plan(path: Path) -> RunPlan:
    """Read one plan, refusing anything that is not a valid plan of this schema."""
    raw = load_json_file(Path(path), what="plan file")
    if not isinstance(raw, dict) or raw.get("kind") != "hflow-plan":
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{path} is not an HFlow plan (no \"kind\": \"hflow-plan\"); write one with "
            "`hflow prepare ... --plan-out <file>`",
        )
    try:
        return RunPlan.model_validate(raw)
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{path} is not a valid plan: {summarize_validation_error(exc)}",
        ) from exc


def plan_drift(
    plan: RunPlan,
    *,
    binding: AuthorizationBinding,
    root_limits: RootBudgetLimits | None,
    submissions_required: int | None = None,
) -> list[str]:
    """What differs between ``plan`` and a fresh resolution of its arguments; empty when current."""
    differences: list[str] = []
    current = binding.model_dump(mode="json")
    if binding.digest() != plan.binding_digest:
        for key in sorted(set(plan.binding) | set(current)):
            planned, now = plan.binding.get(key), current.get(key)
            if planned != now:
                differences.append(f"{key}: planned {planned!r} != now {now!r}")
        if not differences:
            differences.append(
                f"binding digest: planned {plan.binding_digest} != now {binding.digest()}"
            )
    if root_limits != plan.root_limits:
        planned_limits = plan.root_limits.model_dump(mode="json") if plan.root_limits else None
        now_limits = root_limits.model_dump(mode="json") if root_limits else None
        differences.append(f"root_limits: planned {planned_limits!r} != now {now_limits!r}")
    planned_count = plan.prepare_report.authorization.max_top_level_submissions_required
    if submissions_required is not None and submissions_required != planned_count:
        differences.append(
            f"required top-level submissions: planned {planned_count} != now {submissions_required}"
        )
    return differences


def stale_plan_error(plan_path: Path, differences: list[str], *, approved: bool) -> RefusedError:
    """The refusal for a plan whose inputs no longer resolve to what was planned."""
    again = (
        "re-run `hflow prepare ... --plan-out` and `hflow approve --plan` for the new resolution"
        if approved
        else "re-run `hflow prepare ... --plan-out` and review the new plan"
    )
    return RefusedError(
        RefusalCode.RISK_DOWNGRADE,
        f"plan {plan_path} is stale: its inputs no longer resolve to what was prepared ("
        + "; ".join(differences)
        + f"). Nothing was written or dispatched; {again}.",
    )


def default_authorization_path(plan_path: Path, binding_digest: str) -> Path:
    """Where ``approve`` writes, and ``run --plan`` looks for, this plan's approval by default.

    ``<plan stem>.<first 12 hex of the binding digest>.authorization.json`` next to the plan (a
    trailing ``.plan`` is dropped from the stem). The digest is in the name so a re-prepared plan
    whose binding changed never collides with, or picks up, an approval of the old one.
    """
    plan = Path(os.path.abspath(plan_path))
    stem = plan.stem
    if stem.endswith(PLAN_STEM_SUFFIX) and len(stem) > len(PLAN_STEM_SUFFIX):
        stem = stem[: -len(PLAN_STEM_SUFFIX)]
    short = binding_digest.split(":", 1)[-1][:12]
    return plan.with_name(f"{stem}.{short}.authorization.json")


def same_directory(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


#: How many hex characters of the binding digest the approval code is.
APPROVAL_CODE_LENGTH = 8

#: Where a scripted flow is pointed instead: the hand-written artifact.
HAND_WRITTEN_ARTIFACT_DOCS = (
    'docs/operations.md, "Running a real Harness task" - "Writing the artifact by hand"'
)


def approval_code(binding_digest: str) -> str:
    """The code a person types back to approve: the first 8 hex of the plan's binding digest.

    It is shown next to the summary, so it can only be typed by someone looking at what is being
    approved, and it differs for every binding - a reflexive ``y`` or ``no`` never matches.
    """
    return binding_digest.split(":", 1)[-1][:APPROVAL_CODE_LENGTH].lower()


def is_terminal(stream: TextIO | None) -> bool:
    """Whether ``stream`` is an interactive console. A missing or closed stream is not one."""
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def require_terminal(stdin: TextIO | None) -> None:
    """Refuse, without reading anything, unless standard input is an interactive console.

    ``approve`` is the step a person performs. Piped or redirected input is how a script - or the
    agent that wants the task to run - would feed it, so it is refused rather than read.
    """
    if not is_terminal(stdin):
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "`hflow approve` is for a person at a terminal, and standard input is not an "
            "interactive console (piped or redirected), so nothing was read and nothing was "
            "approved or written. Run it in a terminal and type the approval code and your "
            "approval yourself; a scripted flow writes the authorization file by hand instead "
            f"(see {HAND_WRITTEN_ARTIFACT_DOCS})",
        )


def ask_approval(stdin: TextIO, prompt: TextIO, *, code: str) -> str:
    """Ask for the approval code, then for the approval itself; return the approval text.

    Both lines are read from ``stdin`` and both prompts written to ``prompt`` - the stream the
    summary and the code were written to, so redirecting stdout never hides what is being
    approved. The code compares whitespace-trimmed and case-insensitively; anything else refuses
    before the second line is read. The approval is the person's own words, kept as typed (only
    surrounding whitespace is trimmed); an empty line refuses. Never an argument: a command line
    is what the process that wants to run could type for itself.
    """
    prompt.write(f"Type the approval code shown above ({len(code)} characters) to continue: ")
    prompt.flush()
    typed = stdin.readline().strip()
    if typed.lower() != code.lower():
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "the approval code did not match, so nothing was approved and nothing was written. "
            "Re-run `hflow approve --plan` and type the code it shows once you have read the plan",
        )
    prompt.write("Approve this run in your own words (one line; empty cancels): ")
    prompt.flush()
    user_text = stdin.readline().strip()
    if not user_text:
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "no approval text was given (empty input cancels); nothing was written",
        )
    return user_text


def approval_record(
    plan: RunPlan, user_text: str, *, authorization_id: str, authorized_at: str
) -> AuthorizationRecord:
    """The authorization artifact for ``plan``, carrying the user's own ``user_text``.

    Everything else is the plan's: the binding, the root ceilings and the submission count the
    preview said the run needs. ``origin`` is ``user_artifact`` because the text is the user's.
    """
    from .authorization import AuthorizationBinding, AuthorizationRecord

    if not user_text.strip():
        raise RefusedError(
            RefusalCode.RISK_DOWNGRADE,
            "no approval text was given; an empty note is not an approval. Nothing was written",
        )
    pending = plan.prepare_report.authorization
    try:
        record = AuthorizationRecord(
            authorization_id=authorization_id,
            provided_by="user",
            user_text=user_text,
            authorized_at=authorized_at,
            max_top_level_submissions=pending.max_top_level_submissions_required,
            binding=AuthorizationBinding.model_validate(plan.binding),
            root_limits=plan.root_limits,
            origin="user_artifact",
        )
    except ValidationError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            "this plan cannot be approved as it stands: "
            f"{summarize_validation_error(exc)}. Nothing was written",
        ) from exc
    if record.binding_digest() != plan.binding_digest:
        raise RefusedError(
            RefusalCode.INTERNAL_ERROR,
            "the authorization built from this plan digests to "
            f"{record.binding_digest()}, not the plan's {plan.binding_digest}. Nothing was written",
        )
    return record


def write_authorization(path: Path, record: AuthorizationRecord) -> Path:
    """Create ``path`` holding ``record``; an existing file is never overwritten.

    An approval is a record of what the user said. Replacing one in place would erase that
    statement (and a consumed approval's file), so a second approval needs a new file.
    """
    target = Path(os.path.abspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(indent=2) + "\n")
    except FileExistsError as exc:
        raise RefusedError(
            RefusalCode.INVALID_SPEC,
            f"{target} already exists and an authorization file is never overwritten. Nothing "
            "was written; pass --out with a new path, or use the existing approval",
        ) from exc
    return target
