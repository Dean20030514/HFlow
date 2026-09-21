#!/usr/bin/env python
"""Run the prepared M2 task through the real CLI, with a controlled credential injection.

Two things this adds over calling `hflow run` directly, and nothing else:

* the credential reference is resolved from the user's own DSH managed store **in memory** and
  injected into this process's environment, which is the documented highest-precedence source.
  It is never printed, logged or written to disk;
* it runs the real CLI entry point (`hflow.cli.main`), so the run goes through the shipped
  authorization gate, preflight, controller and driver - not a bypass.

    python tools/m2_live/run_authorized_m2.py --authorization-file <auth.json> \
        --data-dir .probe/m2-live/data [--allow-writes]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

CREDENTIAL_REF = "DEEPSEEK_API_KEY"


def resolve_credential(ref: str) -> tuple[str | None, str]:
    """Reuse the probe's resolver: same reference, same boundaries, same non-logging."""
    module_path = REPO_ROOT / "tools" / "m0_probe" / "run_probe.py"
    spec = importlib.util.spec_from_file_location("hflow_m0_run_probe", module_path)
    if spec is None or spec.loader is None:
        return None, "run_probe.py could not be loaded"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.resolve_managed_credential(ref)  # type: ignore[no-any-return]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(REPO_ROOT / ".probe" / "m2-live"))
    parser.add_argument("--task", default="", help="TaskSpec path (defaults to <package>/task.json)")
    parser.add_argument(
        "--project", default="", help="project contract path (defaults to <package>/project.json)"
    )
    parser.add_argument(
        "--repo",
        default="",
        help=(
            "repository of the project under test (defaults to <package>/project). This must be "
            "the same path the authorization was bound to, so it is stated explicitly rather "
            "than guessed from the package layout."
        ),
    )
    parser.add_argument("--authorization-file", required=True)
    parser.add_argument("--authorization-mode", default="m2-live-change")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    package = Path(args.package).resolve()
    data_dir = Path(args.data_dir) if args.data_dir else package / "data"
    task_path = Path(args.task).resolve() if args.task else package / "task.json"
    project_path = Path(args.project).resolve() if args.project else package / "project.json"
    repo = Path(args.repo).resolve() if args.repo else package / "project"
    for label, path in (("task", task_path), ("project", project_path), ("repo", repo)):
        if not path.exists():
            print(f"{label} path does not exist: {path}", file=sys.stderr)
            return 4

    value, status = resolve_credential(CREDENTIAL_REF)
    if not value:
        print(f"credentials unavailable: {status}", file=sys.stderr)
        return 4

    argv = [
        "run",
        "--task",
        str(task_path),
        "--project",
        str(project_path),
        "--project-root",
        str(repo),
        "--driver",
        "acpx-dsh",
        "--authorization-file",
        str(Path(args.authorization_file).resolve()),
        "--authorization-mode",
        args.authorization_mode,
        "--data-dir",
        str(data_dir),
    ]
    if args.json:
        argv.append("--json")

    os.environ[CREDENTIAL_REF] = value
    if args.allow_writes:
        os.environ["HFLOW_ALLOW_WRITES"] = "1"
    del value
    print(f"dispatch      {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"authorization {args.authorization_file}")
    print(f"writes        {'allowed (disposable worktree)' if args.allow_writes else 'denied'}")
    try:
        from hflow.cli import main as hflow_main

        return hflow_main(argv)
    finally:
        os.environ.pop(CREDENTIAL_REF, None)


if __name__ == "__main__":
    raise SystemExit(main())
