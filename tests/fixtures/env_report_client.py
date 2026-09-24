"""Test-only client that reports the environment it was actually started with.

A regression for "the launch is bound, not re-derived from the environment" cannot be asserted
on a driver attribute: the question is what the *process* saw. This stand-in is spawned through
the driver's own launch path (``readonly_client_check``), so the answer comes from a real child
of this machine's process boundary rather than from the in-process mapping that produced it.

It reports one variable - ``DSH_HOME`` - and nothing else. Dumping the whole environment into a
test artifact would copy whatever credentials happen to be in it.

Usage (normally spawned by the driver):
    python env_report_client.py --version
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    # The real client takes flags; this stand-in accepts anything and ignores it, because the
    # only thing under test is the environment the driver handed it.
    _ = argv if argv is not None else sys.argv[1:]
    sys.stdout.write(
        json.dumps(
            {
                "dsh_home_present": "DSH_HOME" in os.environ,
                "dsh_home": os.environ.get("DSH_HOME"),
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
