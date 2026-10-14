"""What the operator runs next, derived from stored facts only (batch K).

A run's life after ``hflow run`` is a chain of commands - ``integrate prepare``, ``integrate
apply`` (or a hand merge and ``integrate reconcile``), ``clean`` and, after an interruption,
``resume`` and ``ledger settle`` - each needing the right ids and the right ``--data-dir``.
:func:`next_steps` reads what the ledger recorded about one run and returns the command(s) that
come next, ready to paste; ``status``, ``resume``, ``ledger settle`` and ``integrate`` print them
as ``next: <command>`` lines in their text output (never in ``--json``).

What this module never does: call a model, probe a process, run Git, read a project file or
write anything. A command whose effect depends on something only the command itself can observe
(an owner that may still be alive, a branch some worktree has checked out) is printed with a
trailing ``# ...`` comment saying so - the command decides, the hint does not.

What it never suggests: running a task again, re-using an authorization, or a new revision. Each
of those needs the user's own new approval (AGENTS rules 5 and 10); an ended run's way forward
here is reconciling, settling, integrating and releasing what it left behind.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .contracts import (
    INTEGRATION_ACTIVE_STATES,
    DeliveryState,
    IntegrationRecord,
    IntegrationState,
    RefusalCode,
    TaskState,
)
from .paths import database_path, default_data_dir
from .store import (
    INVOCATION_OPEN_STATES,
    INVOCATION_OPERATOR_SETTLEABLE_STATES,
    Store,
)

#: The words of the attestation placeholder. Pasted as printed - with or without its brackets -
#: an attestation is refused (``controller.attestation_problem``): nobody wrote it.
ATTEST_PLACEHOLDER_TEXT = "what you know and why"
#: The placeholder printed where only the operator can supply the words of an attestation.
ATTEST_PLACEHOLDER = f'"<{ATTEST_PLACEHOLDER_TEXT}>"'
#: The placeholder for a branch name: HFlow integrates into an existing branch the operator names.
BRANCH_PLACEHOLDER = "<branch>"
#: The placeholder for the project contract file ``integrate prepare`` requires. The run records
#: the contract's digest, not its path, so the path cannot be printed from stored facts.
PROJECT_PLACEHOLDER = "<project.json>"

_LIVE_STATES = frozenset({TaskState.DRAFT, TaskState.READY, TaskState.RUNNING, TaskState.CHECKING})
#: Blocks ``resume`` reconciles (the attempt that was in flight is unknown).
_RECONCILED_BLOCKS = frozenset({RefusalCode.OUTCOME_UNKNOWN.value, RefusalCode.OWNER_LOST.value})
#: Terminal integration states after which preparing again against the target can succeed.
_PREPARE_AGAIN = frozenset(
    {
        IntegrationState.STALE,
        IntegrationState.INTERRUPTED,
        IntegrationState.FAILED,
        IntegrationState.SUPERSEDED,
    }
)
#: The run's own worktree is still there: created (``READY``) or kept after a refused removal.
_WORKTREE_LEFT = frozenset({"READY", "PRESENT"})
#: A word that needs no quoting in PowerShell or ``sh``: ids, commit ids, plain branch names.
_PLAIN_WORD = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]*")


# --------------------------------------------------------------------------
# quoting and the data directory (moved from integrate.py)
# --------------------------------------------------------------------------

#: Characters PowerShell reads inside a double-quoted string: ``$`` and the backtick expand, and
#: the typographic double quotes end the string as ``"`` does.
_POWERSHELL_DOUBLE_QUOTED_SPECIAL = frozenset('"`$\u201c\u201d\u201e')
#: The characters PowerShell takes for a single quote; doubling one keeps it literal.
_POWERSHELL_SINGLE_QUOTES = frozenset("'\u2018\u2019\u201a\u201b")


def shell_word(text: str, *, windows: bool = os.name == "nt") -> str:
    """The path ``text`` as one word of a command an operator pastes into their shell.

    A printed command is for PowerShell on Windows (PowerShell 7 or Windows PowerShell 5.1) and
    for ``sh`` elsewhere - not for cmd.exe, which expands ``%VAR%`` inside double quotes and hands
    PowerShell's single quotes to the program as part of the argument. On Windows a path goes in
    double quotes unless it holds a character PowerShell would expand there or end the string at;
    then it goes in PowerShell's literal single quotes, each single quote doubled. A path ending
    in a backslash - a root, ``C:\\`` or ``\\\\server\\share\\``, is the only directory printed
    with one - gets a ``.`` appended first: the same directory, with no backslash before the
    closing quote. Windows PowerShell 5.1 re-quotes an argument that holds a space for the program,
    and a backslash there escapes that quote (observed: ``"C:\\my dir\\" next`` reached the program
    as the one argument ``C:\\my dir" next``). Elsewhere :func:`shlex.quote`.
    """
    if not windows:
        return shlex.quote(text)
    if text.endswith("\\"):
        text += "."
    if not any(char in _POWERSHELL_DOUBLE_QUOTED_SPECIAL for char in text):
        return f'"{text}"'
    doubled = (char * 2 if char in _POWERSHELL_SINGLE_QUOTES else char for char in text)
    return "'" + "".join(doubled) + "'"


def named_data_dir(store: Store) -> str | None:
    """The data directory a printed command must name, or ``None`` when the default reaches it.

    ``--data-dir`` selects the ledger (``<data-dir>/hflow.sqlite``) an ``hflow`` command opens,
    and an integration exists only in the ledger it was prepared in. A command printed without
    it opens the default ledger (``HFLOW_DATA_DIR``, else the platform directory) and exits 4
    (unknown integration) for anyone who passed another one. Absolute, so the command works from
    any working directory.
    """
    path = getattr(store, "path", None)
    if path is None or str(path) == ":memory:":
        return None
    ledger = os.path.normcase(os.path.abspath(path))
    if ledger == os.path.normcase(os.path.abspath(database_path(default_data_dir()))):
        return None
    return os.path.dirname(os.path.abspath(path))


def _word(text: str) -> str:
    """An id, a commit or a branch name as one word: as it is when plain, else quoted."""
    return text if _PLAIN_WORD.fullmatch(text) else shell_word(text)


def command(data_dir: str | None, *words: str) -> str:
    """``hflow <words>``, with ``--data-dir`` appended when the ledger is not the default one.

    ``words`` are printed as given: fixed subcommand names and flags, validated ids, full object
    ids, placeholders, and values the caller already quoted (:func:`shell_word`). The data
    directory is quoted here.
    """
    parts = ["hflow", *words]
    if data_dir is not None:
        parts += ["--data-dir", shell_word(data_dir)]
    return " ".join(parts)


def hflow_command(store: Store, *words: str) -> str:
    """:func:`command` for the ledger ``store`` opened (:func:`named_data_dir`)."""
    return command(named_data_dir(store), *words)


def _with_comment(text: str, why: str) -> str:
    """A command with a trailing comment; ``#`` starts one in PowerShell and in ``sh``."""
    return f"{text}  # {why}"


# --------------------------------------------------------------------------
# the stored facts one decision reads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunFacts:
    """What the ledger recorded about one run that decides what comes next. Read, never probed."""

    run_id: str
    task_state: TaskState
    block_code: str | None = None
    delivery_state: str = DeliveryState.NONE.value
    #: The receipt names a frozen Git candidate commit (the run used a worktree).
    git_candidate: bool = False
    #: ``runs.worktree_state``: ``NONE``, ``READY`` (created), ``REMOVING`` (a cleanup intent is
    #: recorded), ``PRESENT`` (a cleanup failed and released its claim) or ``REMOVED``.
    worktree_state: str = "NONE"
    worktree_path: str = ""
    integrations: tuple[IntegrationRecord, ...] = ()
    #: Ledger entries still ``reserved``/``requested``/``started`` (``INVOCATION_OPEN_STATES``).
    open_entries: tuple[str, ...] = ()
    #: ``(invocation_id, state)`` of the entries ``ledger settle`` may close (``unknown`` /
    #: ``launch_unknown``).
    settleable_entries: tuple[tuple[str, str], ...] = ()
    #: The newest attempt names an invocation and no reconcile was recorded for it.
    unreconciled: bool = False
    #: No owner token, and an attempt recorded a controller pid (no host): a pre-v6 owner that
    #: HFlow can never prove gone, so closing the run's entries needs ``--legacy-owner-gone``.
    legacy_owner: bool = False


def run_facts(store: Store, run_id: str) -> RunFacts:
    """Read :class:`RunFacts` for ``run_id``. Raises ``RunNotFound`` for an unknown run."""
    row = store.get_run(run_id)
    run_id = str(row["run_id"])
    git_candidate = False
    if row["receipt_json"]:
        candidate = (json.loads(row["receipt_json"]).get("candidate") or {})
        git_candidate = bool(candidate.get("git_commit"))
    attempts = store.attempts_for(run_id)
    newest = attempts[-1] if attempts else None
    unreconciled = bool(
        newest is not None
        and (newest["invocation_id"] or newest["review_invocation_id"])
        and not ("reconcile_json" in newest.keys() and newest["reconcile_json"])
    )
    keys = row.keys()
    owner_token = row["owner_token"] if "owner_token" in keys else None
    legacy = owner_token is None and any(a["process_id"] is not None for a in attempts)
    entries = store.invocations_for(run_id)
    return RunFacts(
        run_id=run_id,
        task_state=TaskState(row["task_state"]),
        block_code=row["block_code"],
        delivery_state=str(row["delivery_state"] or DeliveryState.NONE.value),
        git_candidate=git_candidate,
        worktree_state=str(row["worktree_state"] or "NONE"),
        worktree_path=str(row["worktree_path"] or ""),
        integrations=tuple(store.integrations_for(run_id)),
        open_entries=tuple(
            entry.invocation_id for entry in entries if entry.state.value in INVOCATION_OPEN_STATES
        ),
        settleable_entries=tuple(
            (entry.invocation_id, entry.state.value)
            for entry in entries
            if entry.state.value in INVOCATION_OPERATOR_SETTLEABLE_STATES
        ),
        unreconciled=unreconciled,
        legacy_owner=legacy,
    )


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------


def _branch(target_ref: str) -> str:
    return target_ref.removeprefix("refs/heads/")


def _prepare(facts: RunFacts, data_dir: str | None, target: str, project: str | None) -> str:
    words = ["integrate", "prepare", _word(facts.run_id), "--target", target]
    if project is not None:
        words += ["--project", project]
    return command(data_dir, *words)


def _integration_steps(
    facts: RunFacts, data_dir: str | None, project: str | None
) -> tuple[list[str], bool]:
    """The integration half for an accepted Git candidate: ``(steps, finished)``.

    ``finished`` means nothing more is integrated through HFlow - the change reached a branch, or
    the newest integration ended in a state preparing again against the same tip cannot change
    (``conflict``, ``checks_failed``) - so releasing the run's worktree comes next.
    """
    records = facts.integrations
    if any(record.state is IntegrationState.INTEGRATED for record in records):
        return [], True
    active = [record for record in records if record.state in INTEGRATION_ACTIVE_STATES]
    if active:
        return [
            _with_comment(
                command(data_dir, "integrate", "reconcile", _word(record.integration_id)),
                f"{record.state.value}: settles it once the process working on it has exited "
                "(exit 5 while it may still run)",
            )
            for record in active
        ], False
    if not records:
        return [_prepare(facts, data_dir, BRANCH_PLACEHOLDER, project)], False
    latest = records[-1]
    if latest.state is IntegrationState.READY:
        return [
            _with_comment(
                command(
                    data_dir, "integrate", "apply", _word(latest.integration_id),
                    "--expect-target", _word(latest.target_tip),
                ),
                "a checked-out branch is never moved: apply then prints the merge to run "
                "yourself and the reconcile that records it",
            )
        ], False
    if latest.state in _PREPARE_AGAIN:
        return [
            _with_comment(
                _prepare(facts, data_dir, _word(_branch(latest.target_ref)), project),
                f"the last integration ended {latest.state.value}; prepare against the "
                "branch's current tip",
            )
        ], False
    # conflict / checks_failed: nothing HFlow can apply; the decision is the operator's.
    return [], True


def plan_next(
    facts: RunFacts,
    *,
    data_dir: str | None = None,
    project_file: str | None = None,
    project_default: bool = False,
) -> list[str]:
    """The next operator command(s) for a run, from :class:`RunFacts` alone. Pure.

    In order: what keeps a ledger root blocked (``resume`` for an interrupted attempt or for an
    ended run's open entries, then ``ledger settle`` for each unresolved entry), then the
    integration of an accepted Git candidate, then releasing the run's worktree (``clean``
    preview, then ``--apply``) once nothing else needs it. A live run gets ``status`` (and
    ``resume``, which takes it over only from an owner proven gone). An empty list: nothing is
    left to do for this run.

    ``project_file`` is the contract path a caller already holds; without it ``--project`` is
    left out when ``project_default`` says ``integrate prepare``'s own default resolves the run's
    contract, and is otherwise the placeholder :data:`PROJECT_PLACEHOLDER`.
    """
    run = _word(facts.run_id)
    if project_file:
        project: str | None = shell_word(os.path.abspath(project_file))
    else:
        project = None if project_default else PROJECT_PLACEHOLDER
    if facts.task_state in _LIVE_STATES:
        return [
            _with_comment(
                command(data_dir, "status", run),
                f"the run is {facts.task_state.value}; look again later",
            ),
            _with_comment(
                command(data_dir, "resume", run),
                "only once its controller has exited: blocks the run owner_lost and never "
                "re-dispatches (exit 5 while that owner may be alive)",
            ),
        ]

    steps: list[str] = []
    reconcile_path = (
        facts.task_state is TaskState.BLOCKED and facts.block_code in _RECONCILED_BLOCKS
    )
    needs_resume = bool(facts.open_entries) or (reconcile_path and facts.unreconciled)
    if needs_resume:
        if facts.legacy_owner and facts.open_entries and not reconcile_path:
            steps.append(
                _with_comment(
                    command(
                        data_dir, "resume", run, "--legacy-owner-gone", "--attest",
                        ATTEST_PLACEHOLDER,
                    ),
                    "only if you know its pre-v6 controller has exited; recorded as your "
                    "attestation",
                )
            )
        else:
            steps.append(
                _with_comment(
                    command(data_dir, "resume", run),
                    "reconciles, never re-dispatches; exit 5: its owner may still be alive - "
                    "wait, then run it again",
                )
            )
    settle = list(facts.settleable_entries)
    if needs_resume:
        # Resume records each open entry unknown or launch_unknown (unless the run's own confirmed
        # stop closes it); those are what an operator then settles.
        settle += [(entry, "") for entry in facts.open_entries]
    for invocation_id, state in settle:
        words = ["ledger", "settle", _word(invocation_id), "--attest", ATTEST_PLACEHOLDER]
        if facts.legacy_owner:
            words.append("--legacy-owner-gone")
        text = command(data_dir, *words)
        if state == "launch_unknown":
            text = _with_comment(text, "launch_unknown: --as void returns this dispatch's charge")
        elif not state:
            text = _with_comment(text, "after resume, if it is left unknown or launch_unknown")
        steps.append(text)
    if needs_resume:
        return steps  # releasing the workspace waits until the interrupted attempt is reconciled

    if (
        facts.task_state is TaskState.ACCEPTED
        and facts.delivery_state == DeliveryState.LOCAL_CANDIDATE.value
        and facts.git_candidate
    ):
        integration, finished = _integration_steps(facts, data_dir, project)
        steps += integration
        if not finished:
            return steps

    if facts.worktree_state == "REMOVING":
        steps.append(
            _with_comment(
                command(data_dir, "clean", run, "--reconcile"),
                "a cleanup was interrupted; decides the workspace state from recorded facts",
            )
        )
    elif facts.worktree_state in _WORKTREE_LEFT and facts.worktree_path:
        steps.append(_with_comment(command(data_dir, "clean", run), "preview; changes nothing"))
        steps.append(command(data_dir, "clean", run, "--apply"))
    return steps


def next_steps(
    store: Store, run_id: str, *, project_file: str | Path | None = None
) -> list[str]:
    """The next operator command(s) for ``run_id``, read from ``store``. Never raises.

    Anything that keeps a hint from being worked out - a run whose stored records cannot be read,
    a home directory ``Path.home()`` cannot name for the default data directory - gives no hint
    (``[]``): the command printing the hints has already done its own work (``run`` has dispatched),
    and a hint must never change its outcome or exit code. ``project_file``: see :func:`plan_next`.
    """
    try:
        from .integrate import recorded_project_file  # integrate imports this module

        facts = run_facts(store, run_id)
        project_default = project_file is None and recorded_project_file(store, run_id) is not None
        return plan_next(
            facts,
            data_dir=named_data_dir(store),
            project_file=str(project_file) if project_file is not None else None,
            project_default=project_default,
        )
    except Exception:  # noqa: BLE001 - a hint must never change the command's outcome
        return []


def next_lines(steps: list[str]) -> list[str]:
    """``next: <command>`` text lines, one per step."""
    return [f"next: {step}" for step in steps]
