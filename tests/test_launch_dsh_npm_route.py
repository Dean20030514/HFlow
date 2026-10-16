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


def _node(directory: Path) -> Path:
    """A stand-in ``node.exe`` in ``directory``: hashed by the launch, never run."""
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node.exe"
    node.write_bytes(b"MZ-node-stand-in " + str(directory).encode())
    return node


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
    node = _node(tmp_path / "nodejs")

    launch = _resolve(data_dir, _env(str(node.parent)))

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
    # The shim runs a bare ``node`` (no node.exe beside it): the one on the child's PATH is bound.
    assert _same(paths["carrier_interpreter"], node)
    identity = observe_client_identity(launch)
    assert identity.dsh_carrier == "npm" and identity.dsh_version == RECORDED_DSH_NPM_VERSION

    # The same install found through PATH binds exactly the same files, and says so.
    via_path = _resolve(data_dir, _env(str(shim.parent), str(node.parent)))
    assert via_path.resolvable, via_path.detail
    assert via_path.content_notes[0].startswith(f"dsh={DSH_SOURCE_PATH}: ")
    assert [d.model_dump() for d in via_path.content_digests] == [
        d.model_dump() for d in launch.content_digests
    ]

    # PATH is the operator's choice and wins over the data dir.
    other, _ = _install(tmp_path / "global")
    chosen = _resolve(data_dir, _env(str(other.parent), str(node.parent)))
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


@windows_only
def test_the_node_the_npm_shim_runs_is_bound_and_looked_up_again_at_the_spawn_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The npm shim runs ``node.exe`` beside it, else a bare ``node`` from the child's PATH. That
    file is bound by content, and the spawn gate looks it up again in the environment the child is
    about to get: another node earlier on PATH than at prepare refuses before any process exists.
    """
    from hflow.contracts import InvocationRequest, RefusalCode, RefusedError
    from hflow.drivers.acpx_dsh import AcpxDshDriver
    from hflow.drivers.launch_content import launch_content_changes

    monkeypatch.delenv("DSH_HOME", raising=False)
    data_dir = tmp_path / "data"
    shim, _package = _install(dsh_npm_prefix(data_dir))
    approved = _node(tmp_path / "approved-node")
    launch = _resolve(data_dir, _env(str(approved.parent)))
    assert launch.resolvable, launch.detail
    bound = {item.kind: item for item in launch.content_digests}["carrier_interpreter"]
    assert _same(bound.path, approved)
    assert launch_content_changes(launch, env=_env(str(approved.parent))) == []

    # Another node first on PATH: the binding names the path it now resolves to.
    other = _node(tmp_path / "other-node")
    later = _env(str(other.parent), str(approved.parent))
    changes = launch_content_changes(launch, env=later)
    assert len(changes) == 1 and changes[0].startswith("carrier_interpreter now resolves to")
    folded = os.path.normcase(changes[0])  # PATH lookup spells the extension as PATHEXT does
    assert os.path.normcase(str(other)) in folded and os.path.normcase(str(approved)) in folded

    # The driver judges the environment it builds for the child, inside the spawn gate.
    monkeypatch.setenv("PATH", later["PATH"])
    driver = AcpxDshDriver(data_dir=data_dir, launch=launch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    facts: list = []
    with pytest.raises(RefusedError) as refused:
        driver.start_handle(
            InvocationRequest(
                invocation_id="I-node", attempt_id="A-1", run_id="R-1", role="implementer",
                task_id="T-1", task_revision=1, goal="g", acceptance=[], write_allow=[],
                write_deny=[], workspace=str(workspace), deadline_seconds=60,
                spec_digest="sha256:test", data_dir=str(data_dir), on_spawn=facts.append,
            )
        )
    assert refused.value.code is RefusalCode.LAUNCH_CONTENT_CHANGED
    assert "carrier_interpreter" in refused.value.message
    assert [fact.created for fact in facts] == [False]
    assert driver._processes == {} and driver._handles == {}

    # A node.exe beside the shim is what the shim runs, whatever PATH says.
    beside = _node(shim.parent)
    local = _resolve(data_dir, _env(str(other.parent)))
    assert local.resolvable, local.detail
    paths = {item.kind: item.path for item in local.content_digests}
    assert _same(paths["carrier_interpreter"], beside)
    assert launch_content_changes(local, env=_env(str(approved.parent))) == []


@windows_only
def test_an_npm_shim_with_no_node_to_run_is_not_resolvable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shim, _package = _install(dsh_npm_prefix(data_dir))

    launch = _resolve(data_dir, _env())

    assert launch.resolvable is False
    assert "the npm dsh shim" in launch.detail and "node" in launch.detail
    assert launch.content_digests == []


@windows_only
def test_the_npm_shims_node_inside_the_workspace_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    node = _node(project / "tools")
    data_dir = tmp_path / "data"
    _install(dsh_npm_prefix(data_dir))

    launch = _resolve(data_dir, _env(str(node.parent)), workspaces=[project])

    assert launch.resolvable is False
    assert "node" in launch.detail and "lies inside the workspace" in launch.detail
