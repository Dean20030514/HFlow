"""The live forced-stop probe's evidence notes, checked without running the probe.

The probe itself is a live trial that runs only with the user's approval; nothing here starts
it. Only its pure note helper is imported, so the tri-state ``process_gone`` answer cannot be
recorded as an exit nobody observed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[1] / "tools" / "m0_probe" / "live_forced_stop_probe.py"


def _probe_module(monkeypatch: pytest.MonkeyPatch):
    name = "live_forced_stop_probe_notes"
    spec = importlib.util.spec_from_file_location(name, PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while the file executes.
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))  # the probe prepends its src directory
    spec.loader.exec_module(module)
    return module


def test_an_unanswered_liveness_check_is_not_recorded_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _probe_module(monkeypatch)._not_alive_note

    assert note(True) == "helper had already exited before the stop request"
    unanswered = note(None)
    assert "exited" not in unanswered
    assert unanswered == (
        "helper liveness before the stop was unanswered "
        "(the pid could not be opened or the wait failed)"
    )
