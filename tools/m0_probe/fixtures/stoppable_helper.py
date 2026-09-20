"""The helper the real DSH agent must run in the foreground for the live stop trial.

Deliberately boring: no network, no credentials, no home-directory scanning, no writes
outside its own working directory, and a hard self-timeout so it can never wait forever.

It writes two files, both inside the workspace it is started in:

* ``helper_ready.json``   - once, at startup: nonce, PID, start time, heartbeat path.
* ``helper_heartbeat.txt`` - append-only line per second, so a witness can tell whether the
  process is still alive independently of the OS process check.

Usage:
    python stoppable_helper.py --nonce-file helper_ready.json --lifetime 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce-file", default="helper_ready.json")
    parser.add_argument("--heartbeat-file", default="helper_heartbeat.txt")
    parser.add_argument("--lifetime", type=float, default=60.0)
    parser.add_argument("--nonce", default="")
    args = parser.parse_args(argv)

    nonce = args.nonce or os.urandom(8).hex()
    pid = os.getpid()
    started = time.time()
    ready_path = Path(args.nonce_file)
    heartbeat_path = Path(args.heartbeat_file)

    payload = {
        "nonce": nonce,
        "pid": pid,
        "started_epoch": started,
        "heartbeat_file": str(heartbeat_path.resolve()),
        "cwd": os.getcwd(),
        "lifetime_seconds": args.lifetime,
    }
    temporary = ready_path.with_suffix(ready_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(ready_path)  # the name is the marker: never publish a partial file
    print(f"helper READY pid={pid} nonce={nonce}", flush=True)

    deadline = time.monotonic() + max(1.0, args.lifetime)
    try:
        while time.monotonic() < deadline:
            with heartbeat_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.time():.3f} alive\n")
            time.sleep(1.0)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    print("helper self-timeout reached, exiting", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
