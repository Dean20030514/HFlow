#!/usr/bin/env python
"""End-to-end M2 demo through the real CLI, in a throwaway Git project.

    python examples/m2_cli_demo.py            # runs the whole flow and prints what happened

Shows: an offline Fake M2 candidate, the report, a cleanup preview, an explicit apply, and
the candidate still being readable afterwards. Uses only temp directories; nothing in this
repository or the user's environment is modified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hflow.cli import main as hflow_main  # noqa: E402

SOURCE = '''"""Demo utility."""


def slug(text):
    """Return a URL-friendly slug."""
    return "-".join(text.split()).lower()
'''

TEST = '''import pytest

from demo import slug


def test_slug():
    assert slug("Hello World") == "hello-world"


def test_empty_is_rejected():
    with pytest.raises(ValueError):
        slug("")
'''

FIXED = '''"""Demo utility."""


def slug(text):
    """Return a URL-friendly slug."""
    if not text.strip():
        raise ValueError("slug() needs non-empty text")
    return "-".join(text.split()).lower()
'''

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Demo",
    "GIT_AUTHOR_EMAIL": "demo@example.invalid",
    "GIT_COMMITTER_NAME": "Demo",
    "GIT_COMMITTER_EMAIL": "demo@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603,S607
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=GIT_ENV
    )
    return completed.stdout


def run_cli(argv: list[str], *, quiet: bool = False) -> tuple[int, dict]:
    """Call the real CLI, capturing its JSON payload while still showing human output."""
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    if quiet:
        with redirect_stdout(buffer):
            code = hflow_main(argv)
        payload = {}
        for line in reversed(buffer.getvalue().strip().splitlines()):
            if line.startswith("{"):
                payload = json.loads(line)
                break
        return code, payload
    # Not quiet: let the CLI print for the user, and re-read the run from the store afterwards.
    code = hflow_main(argv)
    return code, {}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="hflow-m2-demo-"))
    repo = root / "demo-project"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text(SOURCE, encoding="utf-8")
    (repo / "tests" / "test_demo.py").write_text(TEST, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.env\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "demo project with an empty-input bug")
    base = git(repo, "rev-parse", "HEAD").strip()

    (root / "task.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "T-demo-m2",
                "revision": 1,
                "goal": "Reject empty input in slug() with a clear ValueError",
                "acceptance": [
                    {"id": "AC-1", "statement": "empty input raises ValueError", "check_ids": ["unit"]}
                ],
                "scope": {"write_allow": ["src/demo.py"], "write_deny": [".git/**"]},
                "risk": "standard",
                "reuse": {"status": "existing_decision", "reference": "project:stdlib-only"},
                "review": {"required": False},
                "budget": {"max_agent_turns": 4, "max_repair_cycles": 0},
                "workspace": {"mode": "worktree", "base_commit": base, "keep": True},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "demo",
                "checks": [
                    {
                        "id": "unit",
                        "kind": "command",
                        "argv": [sys.executable, "-m", "pytest", "-q", "tests"],
                        "timeout_seconds": 120,
                    }
                ],
                "write_deny": [".git/**", ".hflow/**"],
                "limits": {"max_agent_turns": 4, "max_repair_cycles": 0},
                "review_required": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "plan.json").write_text(json.dumps({"src/demo.py": FIXED}), encoding="utf-8")
    data = root / "data"

    print(f"demo project: {repo}\nbase commit:  {base}\n")
    code, payload = run_cli(
        [
            "run", "--task", str(root / "task.json"), "--project", str(root / "project.json"),
            "--project-root", str(repo), "--driver", "fake",
            "--fake-write-plan", str(root / "plan.json"), "--data-dir", str(data), "--json",
        ],
        quiet=True,
    )
    if code != 0 or not payload:
        print("run failed; nothing else attempted")
        return code or 1
    run_id = payload["run_id"]
    candidate = payload["receipt"]["candidate"]
    print(f"run           {run_id} -> {payload['task_state']} ({payload['delivery_state']})")
    print(f"base commit   {candidate['base_commit']}")
    print(f"candidate     {candidate['git_commit']}")
    print(f"tree          {candidate['git_tree']}")
    print(f"fingerprint   {candidate['fingerprint']}")
    print(f"paths         {payload['receipt']['candidate_paths']}")
    print(f"verification  {payload['receipt']['verification']['status']}\n")

    print("--- clean preview (no --apply) ---")
    run_cli(["clean", run_id, "--data-dir", str(data)])
    print("\n--- clean --apply ---")
    apply_code, apply_payload = run_cli(["clean", run_id, "--apply", "--data-dir", str(data)])
    ref = f"refs/hflow/candidates/{run_id}/"
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/hflow/")
    print("\n--- after cleanup ---")
    status = apply_payload.get("status", "") if apply_payload else ""
    print(f"apply exit    {apply_code}{f' ({status})' if status else ''}")
    print(f"candidate ref {refs.strip()}")
    print(
        "content       "
        + git(repo, "show", f"{candidate['git_commit']}:src/demo.py").splitlines()[4].strip()
    )
    print(f"source repo   {git(repo, 'status', '--porcelain').strip() or '(clean)'}")
    print("\ndemo root kept for inspection:", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
