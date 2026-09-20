"""Exercise the Job Object boundary: does closing it stop a stubborn grandchild?

Run directly: `python tools/m0_probe/check_process_boundary.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hflow.drivers.winjob import (  # noqa: E402
    ProcessBoundary,
    platform_summary,
    popen_in_boundary,
    process_gone,
)

GRANDCHILD = "import time; time.sleep(300)"
HELPER = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', %r])\n"
    "print('READY', p.pid, flush=True)\n"
    "time.sleep(300)\n" % GRANDCHILD
)


def main() -> int:
    out = Path(".probe/jobtest.out")
    err = Path(".probe/jobtest.err")
    print("platform:", platform_summary())

    boundary = ProcessBoundary().open()
    child = popen_in_boundary(
        [sys.executable, "-u", "-c", HELPER],
        cwd=".",
        env=dict(os.environ),
        boundary=boundary,
        stdout_path=str(out),
        stderr_path=str(err),
    )
    time.sleep(2.5)
    print("boundary kind:", boundary.kind)
    print("active processes inside boundary:", boundary.active_processes())
    child._hflow_stdout.close()  # type: ignore[attr-defined]
    child._hflow_stderr.close()  # type: ignore[attr-defined]

    text = out.read_text(encoding="utf-8", errors="replace").strip()
    print("helper stdout:", text[:80])
    grandchild = int(text.split()[1])
    print("grandchild alive before teardown:", not process_gone(grandchild))

    boundary.terminate()
    emptied = boundary.wait_empty(5)
    print("boundary reports empty:", emptied, "| active now:", boundary.active_processes())
    # Give the kernel a moment to signal the process objects, then ask again.
    print("direct child gone:", process_gone(child.pid, 2.0))
    print("grandchild gone:", process_gone(grandchild, 2.0))
    boundary.close()
    return 0 if emptied else 1


if __name__ == "__main__":
    raise SystemExit(main())
