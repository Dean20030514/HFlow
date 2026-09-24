"""Local runtime paths and the environment variable names that select them.

Project state (``.hflow/project.json``) is versioned with the target repository.
Runtime data (SQLite, evidence, managed workspaces) lives outside the repository,
never inside the checkout, so a run cannot dirty the working tree it is measuring.

The variable *names* live here because more than one module has to agree on them - the
permission opt-in is read by the admission gate, the resolution path and the controller, and
three spellings of one string is how a gate and a dispatch drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "HFLOW_DATA_DIR"

#: Opt-in to file writes for a real invocation. Off by default; enabled only for a run whose
#: workspace is a disposable worktree created from a fixed base commit.
ENV_ALLOW_WRITES = "HFLOW_ALLOW_WRITES"


def default_data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
        return Path(base or ".") / "HFlow"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "hflow"


def database_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / "hflow.sqlite"
