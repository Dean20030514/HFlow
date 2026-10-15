"""Where a launch finds ``dsh``: PATH, then exactly the npm CLI under the data directory.

User ruling 2026-10-14: DSH #9626 makes the Desktop shim's workspace-write sandbox fail every shell
call, so the first live task runs ``@deepseek-ai/dsh@0.2.0-rc.2`` installed with
``--ignore-scripts`` at ``<data-dir>/dsh/0.2.0-rc.2``. Everything here is offline: the install is a
stand-in layout (npm 11's local shim text, a package.json, an entry file), read and hashed, never
run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hflow.drivers.acpx_dsh import (
    DSH_SOURCE_DATA_DIR,
    DSH_SOURCE_PATH,
    resolve_dsh,
    resolve_launch_config,
)
from hflow.drivers.dsh_surfaces import (
    RECORDED_DSH_NPM_VERSION,
    dsh_npm_bin_dir,
    dsh_npm_install_command,
    dsh_npm_prefix,
    observe_client_identity,
)

from .conftest import FAKE_ACPX_CLIENT
from .test_dsh_surfaces import NPM_LOCAL_SHIM

windows_only = pytest.mark.skipif(os.name != "nt", reason="the npm shim is a Windows batch file")


def _env(*path_entries: str) -> dict[str, str]:
    env = {"PATH": os.pathsep.join(path_entries)}
    if os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return env


def _install(prefix: Path) -> tuple[Path, Path]:
    """What ``npm install --prefix <prefix> --ignore-scripts @deepseek-ai/dsh@...`` leaves for
    a launch: ``(shim, package directory)``."""
    modules = prefix / "node_modules"
    shim = modules / ".bin" / "dsh.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text(NPM_LOCAL_SHIM, encoding="utf-8", newline="")
    package = modules / "@deepseek-ai" / "dsh"
    (package / "lib").mkdir(parents=True)
    (package / "lib" / "bin.js").write_text("// dsh entry stand-in\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh", "version": RECORDED_DSH_NPM_VERSION}),
        encoding="utf-8",
    )
    return shim, package


def _resolve(data_dir: Path, env: dict[str, str], **extra: object):
    return resolve_launch_config(
        data_dir=data_dir,
        acpx_cli=FAKE_ACPX_CLIENT,
        python_executable=sys.executable,
        env=env,
        **extra,  # type: ignore[arg-type]
    )


def _same(left: str, right: Path | str) -> bool:
    return os.path.normcase(left) == os.path.normcase(str(right))


@windows_only
def test_the_data_dir_install_is_the_launcher_when_path_has_none_and_is_bound_alike(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shim, package = _install(dsh_npm_prefix(data_dir))
    assert _same(str(dsh_npm_bin_dir(data_dir)), shim.parent)
    cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"

    launch = _resolve(data_dir, _env())

    assert launch.resolvable, launch.detail
    assert _same(launch.dsh_executable, shim)
    assert launch.agent_argv[:3] == [str(cmd), "/d", "/c"]
    assert launch.agent_argv[3:] == [launch.dsh_executable, "--profile", "acp"]
    assert launch.content_notes[0].startswith(f"dsh={DSH_SOURCE_DATA_DIR}: PATH has no dsh")
    assert str(dsh_npm_prefix(data_dir)) in launch.content_notes[0]
    paths = {item.kind: item.path for item in launch.content_digests}
    assert _same(paths["dsh_launcher"], shim)
    assert _same(paths["carrier_entry"], package / "lib" / "bin.js")
    assert _same(paths["carrier_package_json"], package / "package.json")
    identity = observe_client_identity(launch)
    assert identity.dsh_carrier == "npm" and identity.dsh_version == RECORDED_DSH_NPM_VERSION

    # The same install found through PATH binds exactly the same files, and says so.
    via_path = _resolve(data_dir, _env(str(shim.parent)))
    assert via_path.resolvable, via_path.detail
    assert via_path.content_notes[0].startswith(f"dsh={DSH_SOURCE_PATH}: ")
    assert [d.model_dump() for d in via_path.content_digests] == [
        d.model_dump() for d in launch.content_digests
    ]

    # PATH is the operator's choice and wins over the data dir.
    other, _ = _install(tmp_path / "global")
    chosen = _resolve(data_dir, _env(str(other.parent)))
    assert _same(chosen.dsh_executable, other)


def test_only_the_recorded_version_under_the_data_dir_is_looked_for(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    stray = data_dir / "dsh" / "0.2.1-alpha.2" / "node_modules" / ".bin"
    stray.mkdir(parents=True)
    (stray / ("dsh.cmd" if os.name == "nt" else "dsh")).write_text("", encoding="utf-8")

    assert resolve_dsh(data_dir, _env()) == ("", "")
    launch = _resolve(data_dir, _env())

    assert launch.resolvable is False
    assert launch.agent_argv == [] and launch.dsh_executable == ""
    # Both places and the install that fixes it, with the prefix quoted for the operator's shell.
    assert "PATH" in launch.detail and str(dsh_npm_bin_dir(data_dir)) in launch.detail
    assert dsh_npm_install_command(data_dir) in launch.detail
    assert dsh_npm_install_command(data_dir).endswith(
        f"--ignore-scripts @deepseek-ai/dsh@{RECORDED_DSH_NPM_VERSION}"
    )


@windows_only
def test_a_data_dir_inside_the_workspace_is_not_a_launch_source(tmp_path: Path) -> None:
    """The containment rule applies to the data-dir shim like to a PATH-found one: the agent can
    write inside its workspace, so a launcher there never runs."""
    project = tmp_path / "project"
    data_dir = project / ".hflow-data"
    _install(dsh_npm_prefix(data_dir))

    launch = _resolve(data_dir, _env(), workspaces=[project])

    assert launch.resolvable is False
    assert "lies inside the workspace" in launch.detail and "dsh" in launch.detail
