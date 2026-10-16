"""Starting files for one operator: ``hflow init project | profile | task`` (batch L).

Each command writes exactly one new file and never replaces one: the file is opened exclusively
(``open(path, "x")``), so an existing contract, profile or task - the operator's own edits - is
refused and left byte for byte as it was. Every document is validated with the contract it is
later read back with (``ProjectConfig``, ``MachineProfile``, ``TaskSpec``) before anything is
written. The one field left invalid on purpose is a task's empty goal when none was given: that
task file does not load until the operator writes one.

What a scaffold never decides on the operator's behalf:

* a check is always ``kind: command``, never ``fake``: a project contract approves programs that
  really run, and with nothing to infer from the repository the operator names them (``--check``);
* a task's reuse decision (the reuse/adapt fit test included) is left unanswered, so admission
  refuses the task until the operator decides it; an empty goal or write scope is left empty, and
  admission refuses that as well;
* no root budget file, no authorization, no plan: approving and spending stay the operator's own
  steps (``prepare`` -> ``approve`` -> ``run``).

Nothing here calls a model, opens the ledger or starts anything but read-only ``git`` queries.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    NATIVE_MODEL_SELECTION,
    AgentBinding,
    CheckDef,
    MachineProfile,
    ProfileLimits,
    ProjectConfig,
    RefusalCode,
    RefusedError,
    TaskSpec,
)
from .hints import command, data_dir_word, shell_word
from .paths import default_data_dir
from .profiles import (
    ENV_PROFILE,
    _is_plain_profile_id,
    available_profile_ids,
    profile_path,
    requested_profile_id,
    summarize_validation_error,
)

#: Deny rules a new project contract starts with: Git's own metadata, HFlow's contract, and CI
#: configuration, which a task should change only when it names it and the contract allows it.
DEFAULT_WRITE_DENY = (".git/**", ".hflow/**", ".github/**")
#: The check timeout a scaffold writes; the operator edits it in the contract.
DEFAULT_CHECK_TIMEOUT = 600
PYTEST_ARGV = ("python", "-m", "pytest", "-q")
#: ``npm`` is a batch file on Windows, and an approved check's argv is started without a shell,
#: which finds ``npm.cmd`` only by its full name.
NPM_PROGRAM = "npm.cmd" if os.name == "nt" else "npm"
#: What ``npm init`` writes as the test script: a placeholder that always fails.
_NPM_PLACEHOLDER_TEST = "no test specified"
#: The two agents of the profile the operations guide describes.
IMPLEMENTER_AGENT = "dsh-worker"
REVIEWER_AGENT = "dsh-reviewer"
PROFILE_DRIVER = "acpx-dsh"
PROFILE_HARNESS = "dsh"
#: What the profile id placeholder looks like in a printed ``prepare``.
PROFILE_PLACEHOLDER = "<profile-id>"
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass
class Scaffold:
    """One written file and what the operator does next."""

    path: Path
    #: What the operator still has to decide or check in the file before it is used.
    todo: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Commands, as printed ``next:`` lines.
    next: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [
            f"wrote {self.path}",
            *(f"todo: {item}" for item in self.todo),
            *(f"note: {item}" for item in self.notes),
            *(f"next: {item}" for item in self.next),
        ]


def _refuse(message: str) -> RefusedError:
    return RefusedError(RefusalCode.INVALID_SPEC, message)


def write_exclusive(path: Path, document: dict[str, Any]) -> Path:
    """Write ``document`` as JSON to a file that must not exist yet; never overwrite one.

    The exclusive open is the check: there is no "exists?" test that a second writer could slip
    past. The parent directory is created when missing (``.hflow`` or ``profiles``).
    """
    path = Path(path)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise _refuse(f"{path} already exists; init never overwrites a file") from exc
    except OSError as exc:
        raise _refuse(f"{path} could not be written: {exc}") from exc
    return path.resolve()


# --------------------------------------------------------------------------
# hflow init project
# --------------------------------------------------------------------------


def _requirement_name(requirement: object) -> str:
    match = _REQUIREMENT_NAME.match(requirement) if isinstance(requirement, str) else None
    return match.group(1).lower() if match else ""


def _pyproject_uses_pytest(path: Path, notes: list[str]) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        notes.append(f"{path.name} was not read ({exc}); nothing was inferred from it")
        return False
    tool = data.get("tool")
    if isinstance(tool, dict) and "pytest" in tool:
        return True
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    groups: list[object] = [project.get("dependencies")]
    for table in (project.get("optional-dependencies"), data.get("dependency-groups")):
        if isinstance(table, dict):
            groups.extend(table.values())
    return any(
        _requirement_name(item) == "pytest"
        for group in groups
        if isinstance(group, list)
        for item in group
    )


def _ini_declares(path: Path, section: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.strip() == section for line in text.splitlines())


def _uses_pytest(root: Path, notes: list[str]) -> bool:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and _pyproject_uses_pytest(pyproject, notes):
        return True
    return (
        (root / "pytest.ini").is_file()
        or _ini_declares(root / "setup.cfg", "[tool:pytest]")
        or _ini_declares(root / "tox.ini", "[pytest]")
    )


def _npm_test_script(root: Path, notes: list[str]) -> bool:
    path = root / "package.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        notes.append(f"package.json was not read ({exc}); nothing was inferred from it")
        return False
    scripts = data.get("scripts") if isinstance(data, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test, str) or not test.strip():
        return False
    if _NPM_PLACEHOLDER_TEST in test:
        notes.append("package.json's test script is npm's placeholder; it was not used as a check")
        return False
    return True


def infer_checks(root: Path, notes: list[str] | None = None) -> list[CheckDef]:
    """Command checks the repository itself declares; empty when it declares none.

    pytest configured or depended on in ``pyproject.toml`` (or ``pytest.ini``, ``setup.cfg``,
    ``tox.ini``) gives ``python -m pytest -q``; a real ``test`` script in ``package.json`` gives
    ``npm test``. Nothing else is guessed, and nothing here is ever ``kind: fake``.
    """
    notes = notes if notes is not None else []
    checks: list[CheckDef] = []
    if _uses_pytest(root, notes):
        checks.append(
            CheckDef(
                id="pytest", kind="command", argv=list(PYTEST_ARGV),
                timeout_seconds=DEFAULT_CHECK_TIMEOUT,
            )
        )
    if _npm_test_script(root, notes):
        checks.append(
            CheckDef(
                id="npm-test", kind="command", argv=[NPM_PROGRAM, "test"],
                timeout_seconds=DEFAULT_CHECK_TIMEOUT,
            )
        )
    return checks


def parse_check(text: str) -> CheckDef:
    """``ID=COMMAND`` -> a ``kind: command`` check.

    ``COMMAND`` is split like a POSIX shell line (``shlex``), or read as a JSON array of strings
    when it starts with ``[`` - the form to use for a Windows path with backslashes. It is never
    run through a shell: the argv is what the check starts.
    """
    ident, sep, line = text.partition("=")
    ident = ident.strip()
    if not sep or not ident or not line.strip():
        raise _refuse(f"--check {text!r}: expected ID=COMMAND, e.g. unit='python -m pytest -q'")
    try:
        if line.lstrip().startswith("["):
            argv = json.loads(line)
            if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                raise ValueError("a JSON command must be an array of strings")
        else:
            argv = shlex.split(line)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise _refuse(f"--check {text!r}: the command could not be read: {exc}") from exc
    if not argv:
        raise _refuse(f"--check {text!r}: the command is empty")
    try:
        return CheckDef(
            id=ident, kind="command", argv=argv, timeout_seconds=DEFAULT_CHECK_TIMEOUT
        )
    except ValidationError as exc:
        raise _refuse(f"--check {text!r}: {summarize_validation_error(exc)}") from exc


def init_project(
    project_root: Path,
    *,
    checks: list[str] | None = None,
    project_id: str | None = None,
    data_dir: Path | None = None,
) -> Scaffold:
    """Write ``<project_root>/.hflow/project.json``: command checks, default deny rules."""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise _refuse(f"project root {root} is not a directory")
    target = root / ".hflow" / "project.json"
    if target.exists():
        raise _refuse(f"{target} already exists; init never overwrites a file")
    notes: list[str] = []
    if checks:
        approved = [parse_check(text) for text in checks]
        source = "from --check"
    else:
        approved = infer_checks(root, notes)
        source = "inferred from the repository"
    if not approved:
        raise _refuse(
            f"no check could be inferred from {root} (no pytest configuration in pyproject.toml, "
            "pytest.ini, setup.cfg or tox.ini, and no test script in package.json). Name each "
            "approved check with --check ID=COMMAND (repeatable); a scaffold never writes a "
            "kind=fake check"
        )
    ids = [check.id for check in approved]
    duplicates = sorted({ident for ident in ids if ids.count(ident) > 1})
    if duplicates:
        raise _refuse(f"check ids must be unique: {', '.join(duplicates)}")
    name = (project_id or root.name).strip()
    if not name:
        raise _refuse("project_id must be non-empty")
    try:
        contract = ProjectConfig(
            project_id=name, checks=approved, write_deny=list(DEFAULT_WRITE_DENY)
        )
    except ValidationError as exc:
        raise _refuse(
            f"the project contract is not valid: {summarize_validation_error(exc)}"
        ) from exc
    path = write_exclusive(target, contract.model_dump(mode="json"))
    data = data_dir_word(data_dir)
    scaffold = Scaffold(path=path, notes=notes)
    for check in approved:
        scaffold.todo.append(
            f"check {check.id!r} ({source}) runs exactly {shlex.join(check.argv)} in the run's "
            "worktree, without a shell; edit it until it is the program you approve"
        )
    scaffold.todo.append(
        "limits, review_required and min_risk_for_review are the contract's defaults; the "
        "contract is versioned with the project, so commit it once it says what you approve"
    )
    scaffold.next.append(command(data, "init", "profile", "<id>"))
    scaffold.next.append(
        command(
            data, "init", "task", "<task-id>", "--goal", '"<what the change must achieve>"',
            "--allow", "<path>", "--project-root", shell_word(str(root)),
        )
    )
    return scaffold


# --------------------------------------------------------------------------
# hflow init profile
# --------------------------------------------------------------------------


def default_profile(profile_id: str) -> MachineProfile:
    """The two-role profile ``docs/operations.md`` describes ("Machine profile").

    Implementer and reviewer are bound to separate agents on the one production driver, with
    ``native_profile``: no ``--model`` flag, so the DSH launcher's own profile decides the model.
    """
    agent = AgentBinding(
        harness=PROFILE_HARNESS, driver=PROFILE_DRIVER, model_selection=NATIVE_MODEL_SELECTION
    )
    return MachineProfile(
        profile_id=profile_id,
        role_bindings={"implementer": IMPLEMENTER_AGENT, "reviewer": REVIEWER_AGENT},
        agents={IMPLEMENTER_AGENT: agent, REVIEWER_AGENT: agent.model_copy()},
        limits=ProfileLimits(max_parallel_workers=1, max_native_children=0),
        security_mode="trusted_local",
    )


def init_profile(profile_id: str, *, data_dir: Path | None) -> Scaffold:
    """Write ``<data_dir>/profiles/<profile_id>.json`` (never inside a checkout)."""
    if not _is_plain_profile_id(profile_id):
        raise _refuse(
            f"profile id {profile_id!r} is not a plain file name (letters, digits, '.', '_' and "
            "'-', not a Windows device name)"
        )
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    profile = default_profile(profile_id)
    # Validated through the loader's own model once more, from the JSON that is written.
    document = profile.model_dump(mode="json")
    MachineProfile.model_validate(document)
    path = write_exclusive(profile_path(root, profile_id), document)
    data = data_dir_word(data_dir)
    return Scaffold(
        path=path,
        todo=[
            f"model_selection is {NATIVE_MODEL_SELECTION} for both roles: no --model flag, so the "
            "DSH launcher's own profile decides the model; set a value per role only if you "
            "choose one (docs/operations.md, \"Model selection\")",
        ],
        notes=[
            f"implementer -> {IMPLEMENTER_AGENT}, reviewer -> {REVIEWER_AGENT}: both "
            f"{PROFILE_HARNESS} over {PROFILE_DRIVER}",
            "a profile binds this machine only; nothing runs and nothing is approved by it",
        ],
        next=[command(data, "doctor", "--profile", profile_id)],
    )


# --------------------------------------------------------------------------
# hflow init task
# --------------------------------------------------------------------------


def current_base(root: Path) -> tuple[str, bool]:
    """``(the checked-out branch's name, False)``, or ``(the commit, True)`` of a detached ``HEAD``.

    The task keeps the name; ``prepare`` resolves it to a commit once and binds that commit, so
    a branch that moves before ``run`` makes an approval for the old commit stop applying. Git
    runs through HFlow's one runner, in its inert environment; git that cannot be run or read is
    a refusal.
    """
    from .gitworkspace import GitError, git_query

    try:
        branch = git_query(("symbolic-ref", "--quiet", "--short", "HEAD"), cwd=root, timeout=30)
        if branch.returncode == 0 and branch.stdout.strip():
            return branch.stdout.strip(), False
        detached = git_query(
            ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"), cwd=root, timeout=30
        )
    except GitError as exc:
        raise _refuse(f"the base of {root} could not be read: {exc}") from exc
    if detached.returncode == 0 and detached.stdout.strip():
        return detached.stdout.strip(), True
    raise _refuse(
        f"{root} is not a Git repository with a commit (git: "
        f"{(branch.stderr or detached.stderr or '').strip()[:200] or 'no HEAD'}); a task runs in "
        "an isolated worktree created from a base commit"
    )


def _profile_for_hint(data_dir: Path | None) -> tuple[str, str]:
    """The ``--profile`` word for the printed ``prepare``, and a note saying where it came from.

    ``HFLOW_PROFILE`` when it is set (``prepare`` would use it anyway), else the one profile in
    ``<data-dir>/profiles`` when there is exactly one; otherwise the placeholder - with several
    profiles, choosing one is the operator's decision. Names only: no profile file is opened.
    """
    from .hints import word

    from_env = requested_profile_id(None)
    if from_env:
        return word(from_env), f"--profile {from_env} is {ENV_PROFILE}'s value"
    available = available_profile_ids(
        Path(data_dir) if data_dir is not None else default_data_dir()
    )
    if len(available) == 1:
        return word(available[0]), f"--profile {available[0]} is the only profile in the data dir"
    return PROFILE_PLACEHOLDER, ""


def _unanswered_reuse() -> dict[str, Any]:
    """A reuse decision admission refuses until the operator makes it.

    ``decided`` with ``choice: none`` and no ``need`` is refused (``reuse.choice``,
    ``reuse.need``), and the fit test is ``pending``: a reuse/adapt choice is refused until a
    recorded result replaces it. Nothing here claims the decision was made.
    """
    return {
        "status": "decided",
        "need": "",
        "local_search": "",
        "choice": "none",
        "reason": "",
        "reference": "",
        "required_fit_test": "",
        "fit_test_status": "pending",
    }


def init_task(
    task_id: str,
    *,
    project_root: Path,
    goal: str = "",
    allow: list[str] | None = None,
    project_file: Path | None = None,
    out: Path | None = None,
    data_dir: Path | None = None,
) -> Scaffold:
    """Write ``<task_id>.task.json``: checks wired from the contract, the rest left to decide."""
    from .prepare import load_project_contract

    if not _is_plain_profile_id(task_id):
        raise _refuse(
            f"task id {task_id!r} is not a plain name (letters, digits, '.', '_' and '-'); it "
            "names the task file"
        )
    root = Path(project_root).resolve()
    contract_path = (
        Path(project_file).resolve() if project_file else root / ".hflow" / "project.json"
    )
    if not contract_path.is_file():
        raise _refuse(
            f"no project contract at {contract_path}: run `hflow init project` first, or name "
            "one with --project"
        )
    project = load_project_contract(contract_path)
    command_checks = [check.id for check in project.checks if check.kind == "command"]
    skipped = [check.id for check in project.checks if check.kind != "command"]
    if not command_checks:
        raise _refuse(
            f"{contract_path} approves no kind=command check; a real delivery is refused when its "
            "acceptance names only kind=fake checks"
        )
    base, detached = current_base(root)
    target = Path(out) if out is not None else Path.cwd() / f"{task_id}.task.json"
    if target.exists():
        raise _refuse(f"{target} already exists; init never overwrites a file")
    write_allow = list(allow or [])
    risk = "strict" if project.min_risk_for_review == "strict" else "standard"
    document: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "revision": 1,
        "goal": goal,
        "acceptance": [
            {
                "id": "AC-1",
                "statement": "the project's approved checks pass: " + ", ".join(command_checks),
                "check_ids": command_checks,
            }
        ],
        "scope": {"write_allow": write_allow, "write_deny": []},
        "risk": risk,
        "dependencies": [],
        "reuse": _unanswered_reuse(),
        "review": {"required": True},
        "delivery": {"mode": "local_candidate"},
        # One implementer turn and one reviewer turn: the loop a reviewed task needs. A repair
        # needs an explicit repair policy and a root budget, which a scaffold never writes.
        "budget": {"max_agent_turns": 2, "max_repair_cycles": 0},
        "workspace": {"mode": "worktree", "base_commit": base, "keep": False},
    }
    # Everything but the goal is checked against the real model; an empty goal stays empty so
    # the file is refused until the operator writes one.
    probe = {**document, "goal": goal or "goal still to be written"}
    try:
        TaskSpec.model_validate(probe)
    except ValidationError as exc:
        raise _refuse(f"the task is not valid: {summarize_validation_error(exc)}") from exc
    path = write_exclusive(target, document)

    scaffold = Scaffold(path=path)
    if not goal.strip():
        scaffold.todo.append(
            "goal: say what the change must achieve (empty, the task file does not load)"
        )
    if not write_allow:
        scaffold.todo.append(
            "scope.write_allow: list each file or directory the worker may change, as literal "
            "paths (empty, admission refuses the task)"
        )
    scaffold.todo.append(
        "reuse: left unanswered on purpose, and admission refuses the task until you decide it: "
        "status existing_decision with a reference; or decided with need and choice "
        "(reuse/adapt/build/defer) - reuse/adapt also needs required_fit_test and its recorded "
        "fit_test_status (passed, or not_required with a reason); or not_required / exempt"
    )
    scaffold.todo.append(
        "acceptance: AC-1 is wired to the approved checks "
        f"({', '.join(command_checks)}); restate what done means in its statement"
    )
    scaffold.notes.append(
        "writes: the prepare below passes --allow-writes because this task changes files "
        "(scope.write_allow); the plan records it, so approve and run --plan use it whatever "
        "the shell sets"
    )
    if skipped:
        scaffold.notes.append(
            "not wired (kind=fake, refused for a real delivery): " + ", ".join(skipped)
        )
    if detached:
        scaffold.notes.append(
            f"base_commit is the commit {base} (HEAD is detached, so there is no branch to "
            "name); prepare binds exactly that commit"
        )
    else:
        scaffold.notes.append(
            f"base_commit is the branch name {base!r}; prepare resolves it to a commit and binds "
            "that"
        )
    data = data_dir_word(data_dir)
    words = ["prepare", "--task", shell_word(str(path))]
    if project_file is not None:
        words += ["--project", shell_word(str(contract_path))]
    from .plan import default_plan_path

    profile, profile_note = _profile_for_hint(data_dir)
    if profile_note:
        scaffold.notes.append(profile_note)
    words += [
        "--project-root", shell_word(str(root)), "--profile", profile, "--allow-writes",
        "--plan-out", shell_word(str(default_plan_path(path))),
    ]
    scaffold.next.append(
        command(data, *words)
        + "  # exit 2 lists what admission still refuses; then approve --plan, run --plan"
    )
    return scaffold
