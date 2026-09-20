"""Runtime build identity.

A run is bound to the controller build that created it (plan 9.5). The build id is
resolved once and stored, so a later controller upgrade cannot silently reinterpret
an existing run's facts.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

ENV_BUILD_ID = "HFLOW_BUILD_ID"
PACKAGE_VERSION = "0.0.1"


def _git_sha(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603,S607 - fixed read-only git query
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha or None


@lru_cache(maxsize=1)
def controller_build() -> str:
    """Stable identifier: explicit override, else package version + optional git sha."""
    override = os.environ.get(ENV_BUILD_ID)
    if override:
        return override
    repo_root = Path(__file__).resolve().parents[2]
    sha = _git_sha(repo_root)
    return f"hflow/{PACKAGE_VERSION}+{sha}" if sha else f"hflow/{PACKAGE_VERSION}"
