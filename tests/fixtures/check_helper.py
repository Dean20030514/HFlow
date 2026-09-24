"""Test-only check process used by the verification lifecycle tests.

It models the three shapes an approved program check can have that matter for process
ownership:

* it can hold **stdin** open (``--stdin``), to prove a check is never left waiting for input
  a runner will not send;
* it can start a **helper** child (``--helper``) that outlives a naive teardown, and wait for
  that helper to prove it is alive (``--wait-helper``) before doing anything else;
* it can **exit while the helper keeps running** (``--exec 0 --linger-after-exit``), which is
  the case where a direct child's exit code would otherwise look like a clean pass.

``--markers`` names a directory, not a file, and every file it writes is named
``<label>-<pid>.<fact>``: ``.ready``/``.helper`` once the helper is up (its content is the
helper's pid), ``.waiting-stdin`` while it waits for input, ``.eof`` once stdin reached
end-of-file. Writing a marker is atomic, so a test waits on a fact instead of a sleep, and
``--stop-on`` lets a test retire a helper that a deliberately weakened boundary failed to kill.

Invoked as::

    python check_helper.py --markers <dir> [--label <name>] [--stdin] [--helper]
                           [--wait-helper] [--sleep <seconds>] [--exec <code>]
                           [--linger-after-exit] [--stop-on <marker dir>]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def write_marker(directory: Path, label: str, pid: int, fact: str, content: str = "") -> None:
    """Write one marker atomically, so a watcher never reads a partial file."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{label}-{pid}.{fact}"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content or fact, encoding="utf-8")
    temporary.replace(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="test-only check process")
    parser.add_argument("--markers", required=True, help="directory this process writes to")
    parser.add_argument("--label", default="check", help="marker prefix, to tell tests apart")
    parser.add_argument("--stdin", action="store_true", help="read stdin until EOF")
    parser.add_argument("--helper", action="store_true", help="start a helper child")
    parser.add_argument("--wait-helper", action="store_true", help="wait for the helper marker")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep afterwards")
    parser.add_argument("--exec", type=int, default=0, dest="exit_code", help="exit code")
    parser.add_argument(
        "--linger-after-exit",
        action="store_true",
        help="leave the helper running and exit: a direct child that looks successful",
    )
    parser.add_argument(
        "--stop-on",
        default="",
        help="directory watched for a 'stop' file while lingering, so tests can retire a helper",
    )
    parser.add_argument(
        "--emit-bytes",
        type=int,
        default=0,
        help="write this many bytes to stdout, with no newline, in 64 KiB chunks",
    )
    parser.add_argument(
        "--emit-stderr-bytes",
        type=int,
        default=0,
        help="write this many bytes to stderr, with no newline",
    )
    parser.add_argument(
        "--emit-utf8",
        type=int,
        default=0,
        help="write this many repetitions of a 3-byte character to stdout (no newline)",
    )
    parser.add_argument(
        "--emit-then-hang",
        action="store_true",
        help="emit the configured output, then sleep past any deadline",
    )
    parser.add_argument(
        "--env-dump",
        default="",
        help="write a JSON object of this process's environment to this path, then continue",
    )
    args = parser.parse_args(argv)

    if args.env_dump:
        import json

        Path(args.env_dump).write_text(
            json.dumps(dict(os.environ), sort_keys=True), encoding="utf-8"
        )

    marker_dir = Path(args.markers)
    marker_dir.mkdir(parents=True, exist_ok=True)
    label, own_pid = args.label, os.getpid()

    if args.emit_bytes or args.emit_stderr_bytes or args.emit_utf8:
        # Deliberately one line and chunked: a runner that reads whole lines, or that reads the
        # entire stream into memory before deciding anything, has to cope with this shape.
        chunk = b"x" * 65536
        remaining = args.emit_bytes
        stream = sys.stdout.buffer
        while remaining > 0:
            piece = chunk[: min(65536, remaining)]
            stream.write(piece)
            remaining -= len(piece)
        stream.flush()
        if args.emit_utf8:
            text = "\u4e2d" * args.emit_utf8  # three bytes per character in UTF-8
            stream.write(text.encode("utf-8"))
        stderr_remaining = args.emit_stderr_bytes
        while stderr_remaining > 0:
            piece = chunk[: min(65536, stderr_remaining)]
            sys.stderr.buffer.write(piece)
            stderr_remaining -= len(piece)
        sys.stderr.buffer.flush()
        if args.emit_then_hang:
            # Past any deadline: this is the case where a runner that stopped draining would
            # deadlock the child on a full pipe instead of reaching its timeout.
            time.sleep(600)
        return args.exit_code

    if args.stdin:
        # Announce readiness first: the test must be able to observe that we are waiting for
        # input, rather than guessing with a sleep.
        write_marker(marker_dir, label, own_pid, "waiting-stdin")
        sys.stdin.read()
        write_marker(marker_dir, label, own_pid, "eof")

    helper: subprocess.Popen | None = None
    if args.helper:
        helper = subprocess.Popen(  # noqa: S603 - fixed interpreter and literal script
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # The marker carries the helper's pid: a test that has to retire a leaked helper
        # (because it deliberately weakened teardown) needs the identity, not just a fact.
        write_marker(marker_dir, label, own_pid, "helper", str(helper.pid))

    if args.wait_helper and helper is not None:
        deadline = time.time() + 30.0
        while not (marker_dir / f"{label}-{own_pid}.helper").exists():
            if time.time() > deadline:
                print("helper never started", file=sys.stderr)
                return 9
            time.sleep(0.02)

    if args.linger_after_exit:
        # The helper is deliberately not reaped, and the parent returns a success code right
        # away: a direct child's exit code therefore looks like a clean pass unless the runner
        # notices that the process tree it owns is still alive. The helper watches this
        # process's liveness and the stop file so that a deliberately weakened boundary in a
        # test cannot leak a sleeper.
        watched = Path(args.stop_on) if args.stop_on else marker_dir
        marker = watched / f"{label}-{own_pid}.parent-gone"
        subprocess.Popen(  # noqa: S603 - fixed interpreter and literal script
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time\n"
                    "from pathlib import Path\n"
                    "pid, marker = int(sys.argv[1]), Path(sys.argv[2])\n"
                    "stop = marker.parent / 'stop'\n"
                    "for _ in range(3000):\n"
                    "    if stop.exists():\n"
                    "        break\n"
                    "    try:\n"
                    "        os.kill(pid, 0)\n"
                    "    except OSError:\n"
                    "        marker.write_text('parent gone', encoding='utf-8')\n"
                    "        break\n"
                    "    time.sleep(0.05)\n"
                ),
                str(os.getpid()),
                str(marker),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return args.exit_code

    if args.sleep:
        time.sleep(args.sleep)
    if helper is not None and helper.poll() is None:
        try:
            helper.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
