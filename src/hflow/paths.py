"""Local runtime paths.

Project state (``.hflow/project.json``) is versioned with the target repository.
Runtime data (SQLite, evidence, managed workspaces) lives outside the repository,
never inside the checkout, so a run cannot dirty the working tree it is measuring.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "HFLOW_DATA_DIR"


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
