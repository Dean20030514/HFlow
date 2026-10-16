"""``plan.is_terminal``: the one test of "a person at an interactive terminal".

``approve`` refuses anything else unread, and ``integrate <run-id>`` stops at ``ready`` without
one. Windows answers ``isatty()`` true for the ``NUL`` device, which is what a scheduled task, a
service or an agent's shell tool often gives a child as standard input - that is no terminal.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from hflow.plan import is_terminal

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
_PROBE = "import sys\nfrom hflow.plan import is_terminal\nprint(is_terminal(sys.stdin))\n"


def _child_answer(stdin: int) -> str:
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    completed = subprocess.run(  # noqa: S603 - the interpreter running this suite
        [sys.executable, "-c", _PROBE],
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    return completed.stdout.strip()


def test_the_null_device_is_not_a_terminal() -> None:
    assert _child_answer(subprocess.DEVNULL) == "False"
    with open(os.devnull, encoding="utf-8") as null:
        assert is_terminal(null) is False


def test_a_pipe_is_not_a_terminal() -> None:
    assert _child_answer(subprocess.PIPE) == "False"


def test_a_stand_in_without_an_os_handle_is_taken_at_its_word() -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert is_terminal(Terminal()) is True
    assert is_terminal(io.StringIO()) is False
    assert is_terminal(None) is False
