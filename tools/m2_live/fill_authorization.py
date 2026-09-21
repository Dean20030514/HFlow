#!/usr/bin/env python
"""Fill the M2 authorization template from the user's OWN approval text.

The text is taken from the user's message, not written here. Provenance stays `user`, and the
binding is re-derived from the prepared task so a stale template cannot authorize a different
base commit or task.

    python tools/m2_live/fill_authorization.py --text-file <user-approval.txt> \
        --authorization-id AUTH-m2-live-1 --max-submissions 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.authorization import (  # noqa: E402
    AuthorizationBinding,
    AuthorizationRecord,
    current_binding,
    load_authorization,
    verify_authorization,
)
from hflow.contracts import ProjectConfig, RunRequest, TaskSpec  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(REPO_ROOT / ".probe" / "m2-live"))
    parser.add_argument(
        "--task",
        default="",
        help="actual TaskSpec path to bind (defaults to <package>/task.json)",
    )
    parser.add_argument(
        "--project",
        default="",
        help="actual project contract path to bind (defaults to <package>/project.json)",
    )
    parser.add_argument(
        "--repo", default="", help="repository root of the project under test (defaults to <package>/project)"
    )
    parser.add_argument("--text-file", required=True, help="file containing the user's verbatim approval")
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--authorized-at", default="")
    parser.add_argument("--max-submissions", type=int, default=2)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    package = Path(args.package).resolve()
    # Bind the *actual* files the run will use. Defaulting silently to the package root would
    # bind a different revision than the one being dispatched.
    task_path = Path(args.task).resolve() if args.task else package / "task.json"
    project_path = Path(args.project).resolve() if args.project else package / "project.json"
    repo = Path(args.repo).resolve() if args.repo else package / "project"
    for label, path in (("task", task_path), ("project", project_path), ("repo", repo)):
        if not path.exists():
            raise SystemExit(f"{label} path does not exist: {path}")

    user_text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not user_text:
        raise SystemExit("the approval text file is empty; there is nothing to record")

    spec = TaskSpec.model_validate_json(task_path.read_text(encoding="utf-8"))
    project = ProjectConfig.model_validate_json(project_path.read_text(encoding="utf-8"))
    request = RunRequest(task=spec, project=project, project_root=repo, workspace_root=repo)
    binding = current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=project,
        request=request,
        spec_path=task_path,
    )
    record = AuthorizationRecord(
        authorization_id=args.authorization_id,
        user_text=user_text,
        authorized_at=args.authorized_at or "2026-09-20T10:40:00Z",
        max_top_level_submissions=args.max_submissions,
        binding=AuthorizationBinding.model_validate(binding.model_dump(mode="json")),
    )
    out = Path(args.out) if args.out else package / f"{args.authorization_id}.json"
    out.write_text(json.dumps(record.model_dump(mode="json"), indent=2), encoding="utf-8")

    # Read it back through the same loader the CLI uses, and verify it against the binding.
    reloaded = load_authorization(out)
    verify_authorization(reloaded, expected=binding)
    print(f"authorization   {out}")
    print(f"id              {reloaded.authorization_id}")
    print(f"provided_by     {reloaded.provided_by}")
    print(f"max submissions {reloaded.max_top_level_submissions}")
    print(f"binding digest  {reloaded.binding_digest()}")
    print(f"task bound      {reloaded.binding.spec_path}")
    print(f"task digest     {reloaded.binding.spec_digest}")
    print(f"repo bound      {reloaded.binding.repo_path}")
    print(f"base commit     {reloaded.binding.base_commit}")
    print(f"user text       {reloaded.user_text[:120]}...")
    print("verified against the actual task: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
