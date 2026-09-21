#!/usr/bin/env python
"""Create the new B attempt's task package: same task, same base, new revision.

Why a copy at all: the first B attempt consumed its allowance and its run is history. The
controller's idempotency rule is correct - the same TaskSpec never buys a second worker turn -
so a *new* managed run requires a new revision. Nothing else changes: the goal, acceptance
criteria, scope, project configuration, fixed check command and base commit are copied
byte-for-byte from the prepared package, and the original stays untouched for the record.

    python tools/m2_live/new_attempt.py --attempt 2
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.contracts import ProjectConfig, TaskSpec  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(REPO_ROOT / ".probe" / "m2-live"))
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args()

    package = Path(args.package).resolve()
    source_task = json.loads((package / "task.json").read_text(encoding="utf-8"))
    source_project = json.loads((package / "project.json").read_text(encoding="utf-8"))

    original = TaskSpec.model_validate(source_task)
    attempt_dir = package / f"attempt-{args.attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    # The only change: a new revision, which is what makes this a new task for the controller.
    revised = TaskSpec.model_validate({**source_task, "revision": args.attempt})
    task_path = attempt_dir / "task.json"
    project_path = attempt_dir / "project.json"
    task_path.write_text(json.dumps(revised.model_dump(mode="json"), indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(source_project, indent=2), encoding="utf-8")

    # Copying the config, not regenerating it: identical checks and limits by construction.
    assert ProjectConfig.model_validate(source_project).checks_digest() == (
        ProjectConfig.model_validate(source_project).checks_digest()
    )

    differences = {
        field: (getattr(original, field), getattr(revised, field))
        for field in ("goal", "acceptance", "scope", "risk", "reuse", "budget", "workspace")
        if getattr(original, field) != getattr(revised, field)
    }
    print(f"attempt dir   {attempt_dir}")
    print(f"task          {task_path}")
    print(f"project       {project_path}")
    print(f"task_id       {revised.task_id}")
    print(f"revision      {original.revision} -> {revised.revision}")
    print(f"base commit   {revised.workspace.base_commit} (unchanged)")
    print(f"spec digest   {revised.spec_digest()}")
    print(f"prev digest   {original.spec_digest()}")
    print(f"scientific differences beyond revision: {differences or 'none'}")

    # The base project must still be the unchanged, still-failing fixture.
    project_repo = package / "project"
    assert (project_repo / ".git").exists(), "the prepared project is missing"
    print(f"project repo  {project_repo}")
    print(f"preserved old runs stay where they are: {package / 'data' / 'hflow.sqlite'}")
    shutil.rmtree(attempt_dir / "__pycache__", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
