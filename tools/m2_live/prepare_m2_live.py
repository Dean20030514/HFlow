#!/usr/bin/env python
"""Prepare the real M2 task package: a synthetic project, a failing acceptance check, and the
exact commands to run a real DSH implementer and a real DSH reviewer.

This is preparation only. It dispatches nothing, reads no credential and needs no
authorization. It exists so that a live run never has to be debugged with a model call.

    python tools/m2_live/prepare_m2_live.py --out .probe/m2-live

The package deliberately contains **no fixed patch and no fake-write plan**: the implementer
is given the requirement, the failing test and the source, and has to do the work.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.authorization import AuthorizationBinding, AuthorizationRecord  # noqa: E402
from hflow.contracts import (  # noqa: E402
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    Scope,
    TaskSpec,
    WorkspaceSpec,
    canonical_json,
)

# --- the synthetic project: same business defect as the offline M2 sample ----------------

SOURCE = '''"""Report helpers for the synthetic acceptance project."""


def summarise(lines):
    """Return a one-line summary of ``lines``."""
    cleaned = [line.strip() for line in lines]
    return "; ".join(cleaned)


def average(values):
    """Return the arithmetic mean of ``values``."""
    return sum(values) / len(values)
'''

TEST_FILE = '''import pytest

from reportkit import average, summarise


def test_summarise_trims_and_joins():
    assert summarise(["  a  ", " b "]) == "a; b"


def test_average_of_values():
    assert average([2, 4]) == 3


def test_summarise_rejects_missing_input():
    """The first real defect: None is not rejected with a message a caller can act on."""
    with pytest.raises(TypeError, match="sequence of strings"):
        summarise(None)


def test_average_rejects_empty_sequence():
    """The second real defect: dividing by len([]) raises ZeroDivisionError."""
    with pytest.raises(ValueError):
        average([])
'''

TASK_GOAL = (
    "Two tests in tests/ fail. summarise(None) must raise a TypeError whose message says a "
    "sequence of strings is required; today it fails with the interpreter's iteration error "
    "instead. average([]) must raise a ValueError saying at least one value is required; today "
    "it raises ZeroDivisionError. Behaviour for valid input must not change. Run the tests in "
    "tests/ to see both failures, then fix the source in src/reportkit/__init__.py - not the "
    "tests, and not the project configuration."
)
ACCEPTANCE = [
    ("AC-1", "summarise(None) raises TypeError", ["unit"]),
    ("AC-2", "average([]) raises ValueError", ["unit"]),
    ("AC-3", "existing valid-input behaviour is unchanged", ["unit"]),
]

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "HFlow Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "HFlow Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607 - fixed, read-mostly fixture commands
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, env=GIT_ENV
    )
    if completed.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _is_broken_worktree_registration(repo: Path) -> bool:
    """True when ``repo/.git`` is a worktree pointer whose target no longer exists.

    Such a directory is a remnant of an interrupted preparation, not a real checkout: its
    administrative directory is gone, so nothing in it is worth protecting.
    """
    git_path = repo / ".git"
    if not git_path.is_file():
        return False
    try:
        content = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not content.startswith("gitdir:"):
        return False
    target = Path(content.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = (repo / target).resolve()
    return not target.exists()


def _is_empty_fixture_remnant(repo: Path) -> bool:
    """True when ``repo`` holds only Git administration, with no working files.

    That is what an interrupted preparation leaves behind: the checkout deleted, the object
    store still locked. It is this script's own scratch, and a preparation that could not
    clear its own scratch would refuse forever.
    """
    if not (repo / ".git").exists():
        return False
    try:
        entries = {path.name for path in repo.iterdir()}
    except OSError:
        return False
    return entries <= {".git"}


def build_project(root: Path) -> Path:
    repo = root / "project"
    if repo.exists():
        import shutil
        import time

        if _is_broken_worktree_registration(repo) or _is_empty_fixture_remnant(repo):
            # Either a dangling pointer or the hollow remains of an interrupted preparation
            # (an admin directory with no working files): nothing in it is worth protecting.
            shutil.rmtree(repo, ignore_errors=True)
        else:
            # A real checkout: detach any linked worktrees holding the object store open
            # (Windows keeps those files locked), then remove the directory.
            subprocess.run(  # noqa: S603,S607 - best-effort fixture cleanup
                ["git", "worktree", "prune"], cwd=str(repo), capture_output=True, check=False
            )
            for _ in range(3):
                shutil.rmtree(repo, ignore_errors=True)
                if not repo.exists():
                    break
                time.sleep(0.5)
        if repo.exists():
            raise SystemExit(
                f"could not clear the previous preparation at {repo}: something still holds it "
                "open. Close it and re-run; this script will not force-delete a real checkout."
            )
    (repo / "src" / "reportkit").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "reportkit" / "__init__.py").write_text(SOURCE, encoding="utf-8")
    (repo / "tests" / "test_reportkit.py").write_text(TEST_FILE, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.env\n", encoding="utf-8"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "synthetic report project with two input-handling defects")
    return repo


def base_commit(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def run_check(repo: Path, argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv from the project contract
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"},
    )


def write_package(root: Path, repo: Path, base: str) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.json").write_text(
        canonical_json(
            TaskSpec(
                task_id="T-m2-live-reportkit",
                revision=1,
                goal=TASK_GOAL,
                acceptance=[
                    AcceptanceCriterion(id=ac_id, statement=statement, check_ids=checks)
                    for ac_id, statement, checks in ACCEPTANCE
                ],
                scope=Scope(write_allow=["src/reportkit/__init__.py"], write_deny=[".git/**"]),
                risk="standard",
                reuse=ReuseDecision(
                    status=ReuseStatus.EXISTING_DECISION,
                    reference="project:python-stdlib-only",
                    reason="standard library only; no new dependency or component",
                ),
                budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
                workspace=WorkspaceSpec(mode="worktree", base_commit=base, keep=True),
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    (root / "project.json").write_text(
        canonical_json(
            ProjectConfig(
                project_id="m2-live-reportkit",
                checks=[
                    CheckDef(
                        id="unit",
                        kind="command",
                        argv=[sys.executable, "-m", "pytest", "-q", "tests"],
                        timeout_seconds=600,
                    )
                ],
                write_deny=[".git/**", ".hflow/**"],
                limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
                review_required=True,
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    (root / "base.txt").write_text(base, encoding="utf-8")
    return {
        "task": root / "task.json",
        "project": root / "project.json",
        "repo": repo,
    }


def write_authorization_template(root: Path, repo: Path, task_path: Path, project_path: Path) -> Path:
    """Emit the artifact skeleton with an EMPTY user_text: only the user fills that in."""
    from hflow.contracts import ProjectConfig as PC
    from hflow.contracts import RunRequest, TaskSpec as TS

    spec = TS.model_validate_json(task_path.read_text(encoding="utf-8"))
    project = PC.model_validate_json(project_path.read_text(encoding="utf-8"))
    request = RunRequest(task=spec, project=project, project_root=repo, workspace_root=repo)
    from hflow.authorization import current_binding

    binding = current_binding(
        mode="m2-live-change", driver="acpx-dsh", project=project, request=request, spec_path=task_path
    )
    template = AuthorizationRecord(
        authorization_id="AUTH-m2-live-1",
        user_text="",
        authorized_at="<set by the user>",
        max_top_level_submissions=2,
        binding=AuthorizationBinding.model_validate(binding.model_dump(mode="json")),
    )
    path = root / "authorization.template.json"
    path.write_text(json.dumps(template.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPO_ROOT / ".probe" / "m2-live"))
    args = ap.parse_args()
    root = Path(args.out).resolve()

    repo = build_project(root)
    base = base_commit(repo)
    files = write_package(root, repo, base)
    template = write_authorization_template(root, repo, files["task"], files["project"])

    argv = [sys.executable, "-m", "pytest", "-q", "tests"]
    result = run_check(repo, argv)
    print(f"project        {repo}")
    print(f"base commit    {base}")
    print(f"check command  {' '.join(argv)}")
    print(f"check at base  exit={result.returncode}")
    print(f"  stdout tail  {result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''}")
    assert result.returncode != 0, "the prepared base must FAIL the acceptance check"
    assert "2 failed" in result.stdout, (
        f"both acceptance defects must fail at the base commit; saw: {result.stdout.strip()}"
    )
    for expected in ("test_summarise_rejects_missing_input", "test_average_rejects_empty_sequence"):
        assert expected in result.stdout, f"{expected} did not fail at the base commit"
    for forbidden in ("error during collection", "ModuleNotFoundError", "ImportError"):
        assert forbidden not in result.stdout, f"the base check failed for the wrong reason: {forbidden}"
    print("  failing tests: the two defect tests only (no import or collection errors)")

    print(f"\ntask           {files['task']}")
    print(f"project        {files['project']}")
    print(f"authorization  {template}  (fill in user_text from the user's own words)")
    print("\nRun a real implementer (requires a bound authorization file):")
    print(
        "  hflow run --task {task} --project {project} --project-root {repo} \\\n"
        "      --driver acpx-dsh --authorization-file <filled-in.json> \\\n"
        "      --authorization-mode m2-live-change --data-dir {data} --json".format(
            task=files["task"], project=files["project"], repo=repo, data=root / "data"
        )
    )
    print(
        "\nThen read the result (no model, no re-run):\n"
        "  hflow status <run-id> --data-dir {data} --project-root {repo}\n"
        "  hflow report <run-id> --data-dir {data} --project-root {repo} --json".format(
            data=root / "data", repo=repo
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
