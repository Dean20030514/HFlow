"""What DSH reads on its own at launch: fixed paths, names never values, nothing executed.

Every test here builds its own files under ``tmp_path``. Nothing is launched, and a ``.env`` or
the home's stored-credentials file is never opened - asserted through ``_read_regular``, the one
function in ``hflow.drivers.dsh_surfaces`` that opens a file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from hflow.contracts import LaunchConfig, LaunchSurfaces
from hflow.drivers import dsh_surfaces
from hflow.drivers.acpx_dsh import (
    DRIVER_ID,
    INVOCATION_ID_PLACEHOLDER,
    child_environment,
    child_home_for,
    effective_dsh_home,
)
from hflow.drivers.dsh_surfaces import (
    dsh_project_root,
    observe_client_identity,
    observe_launch_surfaces,
)


def _launch(**fields: object) -> LaunchConfig:
    return LaunchConfig(driver_id=DRIVER_ID, harness="dsh", **fields)  # type: ignore[arg-type]


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap the module's only file-opening function and record every path it opens."""
    reads: list[Path] = []
    original = dsh_surfaces._read_regular

    def recording(path: Path, limit: int):
        reads.append(Path(path))
        return original(path, limit)

    monkeypatch.setattr(dsh_surfaces, "_read_regular", recording)
    return reads


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_a_dotenv_file_is_recorded_by_presence_and_size_and_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace_env = b"DSH_X=1\nDEEPSEEK_API_KEY=sk-sentinel-123\n"
    (workspace / ".env").write_bytes(workspace_env)
    home = tmp_path / "home"
    home.mkdir()
    home_env = b"HTTP_PROXY=http://home-sentinel\n"
    (home / ".env").write_bytes(home_env)
    reads = _record_reads(monkeypatch)

    surfaces = observe_launch_surfaces(
        _launch(dsh_home=str(home)),
        dsh_home=home,
        dsh_home_kind="bound",
        child_env={},
        workspace=workspace,
    )

    env_file = surfaces.workspace_env
    assert env_file is not None
    assert env_file.present is True and env_file.kind == "file"
    assert env_file.size == len(workspace_env) and env_file.sha256 == ""
    assert "not opened" in env_file.detail
    home_env_file = next(entry for entry in surfaces.dsh_home_files if entry.name == ".env")
    assert home_env_file.present is True and home_env_file.kind == "file"
    assert home_env_file.size == len(home_env) and home_env_file.sha256 == ""
    assert "not opened" in home_env_file.detail
    assert not [path for path in reads if path.name == ".env"], "a .env is never opened"
    dumped = surfaces.model_dump_json()
    assert "sk-sentinel-123" not in dumped and "home-sentinel" not in dumped
    # No field carries names read out of a .env; ``dsh_env_names`` names ambient variables.
    assert [name for name in LaunchSurfaces.model_fields if "env_names" in name] == [
        "dsh_env_names"
    ]

    assert surfaces.deepseek_api_key_inherited is False
    inherited = observe_launch_surfaces(
        _launch(), dsh_home=home, dsh_home_kind="bound", child_env={"DEEPSEEK_API_KEY": "x"}
    )
    assert inherited.deepseek_api_key_inherited is True
    assert "x" not in inherited.dsh_env_names
    if os.name == "nt":
        lower = observe_launch_surfaces(
            _launch(), dsh_home=home, dsh_home_kind="bound", child_env={"deepseek_api_key": "x"}
        )
        assert lower.deepseek_api_key_inherited is True


def test_a_bound_home_is_examined_from_a_fixed_list_and_its_stored_credentials_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "bound"
    (home / "profiles" / "acp").mkdir(parents=True)
    (home / "skills").mkdir()
    cordis = b"sandbox:\n  mode: workspace-write\n"
    (home / "cordis.patch.yml").write_bytes(cordis)
    (home / "profiles" / "acp" / "cordis.patch.yml").write_bytes(b"profile: acp\n")
    (home / "AGENTS.md").write_text("# home rules\n", encoding="utf-8")
    (home / ".env").write_text("A=1\n", encoding="utf-8")
    (home / ".credentials.yaml").write_text("token: cred-sentinel\n", encoding="utf-8")
    (home / "unrelated.txt").write_text("nothing\n", encoding="utf-8")
    reads = _record_reads(monkeypatch)

    surfaces = observe_launch_surfaces(
        _launch(dsh_home=str(home), profile="acp"),
        dsh_home=home,
        dsh_home_kind="bound",
        child_env={},
    )

    assert surfaces.dsh_home_observed is True
    assert [entry.name for entry in surfaces.dsh_home_files] == [
        "cordis.patch.yml",
        "profiles/acp/cordis.patch.yml",
        ".env",
        "AGENTS.md",
        "skills",
    ]
    files = {entry.name: entry for entry in surfaces.dsh_home_files}
    assert files["cordis.patch.yml"].sha256 == _sha(cordis)
    assert files["skills"].kind == "directory"
    assert files[".env"].kind == "file" and files[".env"].sha256 == ""
    assert {path.name for path in reads} <= {"cordis.patch.yml", "AGENTS.md"}
    dumped = surfaces.model_dump_json()
    assert "cred-sentinel" not in dumped and "unrelated.txt" not in dumped
    assert ".credentials" not in dumped


def test_an_entry_that_cannot_be_examined_is_unknown_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "bound"
    home.mkdir()
    (home / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    (home / "cordis.patch.yml").write_text("x: 1\n", encoding="utf-8")
    blocked = str(home / "AGENTS.md")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == blocked:
            raise PermissionError(13, "denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(dsh_surfaces.os, "stat", stat)
    surfaces = observe_launch_surfaces(
        _launch(dsh_home=str(home)), dsh_home=home, dsh_home_kind="bound", child_env={}
    )
    monkeypatch.undo()

    files = {entry.name: entry for entry in surfaces.dsh_home_files}
    assert files["AGENTS.md"].present is None
    assert files["AGENTS.md"].kind == "unknown"
    assert files["AGENTS.md"].detail == "could not be examined: PermissionError"
    assert files["cordis.patch.yml"].present is True and files["cordis.patch.yml"].sha256
    assert files[".env"].present is False and files[".env"].kind == "absent"


def test_instruction_files_run_from_the_git_marker_down_to_the_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    (repo / ".agents" / "skills").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# root\n", encoding="utf-8")
    (repo / "sub" / "CLAUDE.local.md").write_text("# local\n", encoding="utf-8")

    surfaces = observe_launch_surfaces(
        _launch(),
        dsh_home=tmp_path / "home",
        dsh_home_kind="per_invocation",
        child_env={},
        workspace=repo / "sub",
        look_in_home=False,
    )
    assert surfaces.project_root == str(repo)
    assert [entry.name for entry in surfaces.instruction_files] == [
        "AGENTS.md",
        "sub/CLAUDE.local.md",
    ]
    assert all(entry.sha256 for entry in surfaces.instruction_files)
    skills = {entry.name: entry for entry in surfaces.skill_dirs}
    assert skills[".dsh/skills"].kind == "absent"
    assert skills[".agents/skills"].kind == "directory"

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (worktree / "AGENTS.md").write_text("# wt\n", encoding="utf-8")
    in_worktree = observe_launch_surfaces(
        _launch(),
        dsh_home=tmp_path / "home",
        dsh_home_kind="per_invocation",
        child_env={},
        workspace=worktree,
        look_in_home=False,
    )
    assert in_worktree.project_root == str(worktree)
    assert [entry.name for entry in in_worktree.instruction_files] == ["AGENTS.md"]

    loose = tmp_path / "loose"
    loose.mkdir()
    if dsh_project_root(loose) is not None:
        pytest.skip("the temp directory lies inside a Git checkout on this machine")
    unmarked = observe_launch_surfaces(
        _launch(),
        dsh_home=tmp_path / "home",
        dsh_home_kind="per_invocation",
        child_env={},
        workspace=loose,
        look_in_home=False,
    )
    assert unmarked.project_root == ""
    assert any("no .git marker" in note for note in unmarked.notes)


def test_a_dsh_home_inside_the_workspace_is_noted_not_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".dsh").mkdir(parents=True)
    inside = observe_launch_surfaces(
        _launch(dsh_home=str(workspace / ".dsh")),
        dsh_home=workspace / ".dsh",
        dsh_home_kind="bound",
        child_env={},
        workspace=workspace,
    )
    assert any("lies inside the workspace" in note for note in inside.notes)

    relative = observe_launch_surfaces(
        _launch(dsh_home=".dsh-rel"),
        dsh_home=Path(".dsh-rel"),
        dsh_home_kind="bound",
        child_env={},
        workspace=workspace,
    )
    assert relative.dsh_home == str(workspace / ".dsh-rel")
    assert relative.dsh_home_observed is True
    assert any("relative path" in note for note in relative.notes)

    unknown_base = observe_launch_surfaces(
        _launch(dsh_home=".dsh-rel"),
        dsh_home=Path(".dsh-rel"),
        dsh_home_kind="bound",
        child_env={},
        workspace=None,
    )
    assert unknown_base.dsh_home_observed is False
    assert unknown_base.dsh_home_files == []
    assert unknown_base.dsh_home == ".dsh-rel"


def test_only_names_of_dsh_variables_that_reach_the_child_are_recorded(tmp_path: Path) -> None:
    child_env = child_environment(
        {
            "DSH_TELEMETRY_DISABLED": "tele-sentinel",
            "DSH_AGENTS_HOME": "x",
            "DSH_PERMISSION_MODE": "danger-full-access",
            "DSH_HOME": "h",
            "PATH": "",
        },
        extra_env={},
        dsh_home="",
    )
    surfaces = observe_launch_surfaces(
        _launch(),
        dsh_home=tmp_path / "home",
        dsh_home_kind="per_invocation",
        child_env=child_env,
        look_in_home=False,
    )
    assert surfaces.dsh_env_names == ["DSH_AGENTS_HOME", "DSH_TELEMETRY_DISABLED"]
    assert "tele-sentinel" not in surfaces.model_dump_json()


def _package(directory: Path, name: str, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "package.json"
    manifest.write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    return manifest


def test_client_and_sdk_versions_are_read_from_package_files(tmp_path: Path) -> None:
    modules = tmp_path / "node_modules"
    entry = modules / "acpx" / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// not run\n", encoding="utf-8")
    acpx_manifest = _package(modules / "acpx", "acpx", "0.17.1")
    sdk_manifest = _package(modules / "@agentclientprotocol" / "sdk", "@agentclientprotocol/sdk", "1.4.0")

    identity = observe_client_identity(_launch(client_entry=str(entry)))
    assert identity.acpx_version == "0.17.1"
    assert identity.sdk_version == "1.4.0"
    assert identity.acpx_package_json == str(acpx_manifest)
    assert identity.sdk_package_json == str(sdk_manifest)
    assert not [note for note in identity.notes if "differs from" in note]

    _package(modules / "acpx" / "node_modules" / "@agentclientprotocol" / "sdk", "sdk", "1.7.0")
    nested = observe_client_identity(_launch(client_entry=str(entry)))
    assert nested.sdk_version == "1.7.0"
    assert any("differs from 1.4.0" in note for note in nested.notes)

    _package(modules / "acpx", "acpx", "1.0.0; rm -rf")
    odd = observe_client_identity(_launch(client_entry=str(entry)))
    assert odd.acpx_version == ""
    assert any("not a plain version token" in note for note in odd.notes)

    loose_entry = tmp_path / "loose" / "cli.js"
    loose_entry.parent.mkdir()
    loose_entry.write_text("// not run\n", encoding="utf-8")
    loose = observe_client_identity(_launch(client_entry=str(loose_entry)))
    assert loose.acpx_version == ""
    assert any("not inside a node_modules tree" in note for note in loose.notes)


def test_a_manifest_nested_too_deeply_to_parse_is_a_note_not_an_exception(
    tmp_path: Path,
) -> None:
    """Under the read limit, but deep enough to exhaust the JSON parser's recursion."""
    depth = 100_000
    nested = "[" * depth + "]" * depth
    assert len(nested) < dsh_surfaces.MANIFEST_READ_LIMIT_BYTES, "must reach the parser"
    modules = tmp_path / "node_modules"
    entry = modules / "acpx" / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// not run\n", encoding="utf-8")
    acpx_manifest = modules / "acpx" / "package.json"
    acpx_manifest.write_text(nested, encoding="utf-8")
    sdk_manifest = modules / "@agentclientprotocol" / "sdk" / "package.json"
    sdk_manifest.parent.mkdir(parents=True)
    sdk_manifest.write_text(nested, encoding="utf-8")

    identity = observe_client_identity(_launch(client_entry=str(entry)))

    assert identity.acpx_version == "" and identity.acpx_package_json == ""
    assert identity.sdk_version == "" and identity.sdk_package_json == ""
    assert f"{acpx_manifest} is not valid JSON (or is nested too deeply to parse)" in (
        identity.notes
    )
    assert f"{sdk_manifest} is not valid JSON (or is nested too deeply to parse)" in (
        identity.notes
    )

    # The full observation used at spawn and by prepare degrades the same way.
    surfaces = observe_launch_surfaces(
        _launch(client_entry=str(entry)),
        dsh_home=tmp_path / "home" / ".dsh",
        dsh_home_kind="per_invocation",
        child_env={},
        look_in_home=False,
    )
    assert surfaces.client.acpx_version == ""


def test_dsh_carriers_are_classified_from_shim_text_without_running_them(tmp_path: Path) -> None:
    # The installed Desktop layout: the shim's script lies inside resources/app.asar, a regular
    # file (an Electron archive); the shim text is the installed one plus a trap line.
    desktop = tmp_path / "DeepSeek Harness" / "resources" / "runtime"
    shim = desktop / "cli" / "bin" / "dsh.cmd"
    shim.parent.mkdir(parents=True)
    (desktop.parent / "app.asar").write_bytes(b"asar-archive-stand-in")
    trap = shim.parent / "ran.txt"
    shim.write_text(
        "@echo off\r\nsetlocal DisableDelayedExpansion\r\n"
        'set "ELECTRON_RUN_AS_NODE=1"\r\n'
        f'echo ran > "{trap}"\r\n'
        '"%~dp0..\\..\\..\\..\\DeepSeek Harness.exe" --expose-internals '
        '"%~dp0..\\..\\..\\app.asar\\dsh\\node_modules\\@deepseek-ai\\dsh-desktop-host'
        '\\lib\\cli.js" %*\r\nexit /b %errorlevel%\r\n',
        encoding="utf-8",
    )
    (desktop / "primary-runtime").mkdir()
    (desktop / "primary-runtime" / "runtime.json").write_text(
        json.dumps({"desktopVersion": "0.2.0-rc.2", "payloadDigest": "payload-sentinel"}),
        encoding="utf-8",
    )
    cmd = r"C:\Windows\System32\cmd.exe"
    found = observe_client_identity(
        _launch(dsh_executable=str(shim), agent_argv=[cmd, "/c", str(shim), "--profile", "acp"])
    )
    assert found.dsh_carrier == "desktop"
    assert found.dsh_version == "0.2.0-rc.2"
    assert "documented, not verified" in found.dsh_version_source
    assert "payload-sentinel" not in found.model_dump_json()
    assert not trap.exists(), "the shim was read, never run"
    # The content binding names the archive the entry lies in, not the entry inside it.
    named = dsh_surfaces.carrier_files(
        _launch(dsh_executable=str(shim), agent_argv=[cmd, "/c", str(shim), "--profile", "acp"])
    )
    assert named.carrier == "desktop" and named.problem == ""
    assert named.archive == desktop.parent / "app.asar"
    assert named.entry is None and named.package_json is None
    assert any("bound through the archive's digest" in note for note in named.notes)
    assert not trap.exists()

    npm_dir = tmp_path / "npm"
    npm_shim = npm_dir / "dsh.CMD"
    npm_dir.mkdir()
    npm_shim.write_text(
        '@"%~dp0\\node_modules\\@deepseek-ai\\dsh\\bin\\dsh.js" %*\r\n', encoding="utf-8"
    )
    _package(npm_dir / "node_modules" / "@deepseek-ai" / "dsh", "@deepseek-ai/dsh", "0.2.0-rc.2")
    npm = observe_client_identity(
        _launch(dsh_executable=str(npm_shim), agent_argv=[cmd, "/c", str(npm_shim)])
    )
    assert npm.dsh_carrier == "npm" and npm.dsh_version == "0.2.0-rc.2"

    exe = tmp_path / "dsh.exe"
    exe.write_bytes(b"MZ")
    assert observe_client_identity(
        _launch(dsh_executable=str(exe), agent_argv=[str(exe)])
    ).dsh_carrier == "unknown"
    assert observe_client_identity(_launch()).dsh_carrier == "not_applicable"
    assert observe_client_identity(
        _launch(dsh_executable=str(npm_shim), agent_argv=["python", "stub.py"])
    ).dsh_carrier == "not_applicable"

    (desktop / "primary-runtime" / "runtime.json").unlink()
    no_manifest = observe_client_identity(
        _launch(dsh_executable=str(shim), agent_argv=[cmd, "/c", str(shim)])
    )
    assert no_manifest.dsh_carrier == "desktop" and no_manifest.dsh_version == ""
    assert any("runtime.json" in note for note in no_manifest.notes)
    assert not trap.exists()


def test_a_per_invocation_home_before_dispatch_is_named_but_not_looked_into(
    tmp_path: Path,
) -> None:
    child_home = child_home_for(tmp_path, INVOCATION_ID_PLACEHOLDER)
    kind, home = effective_dsh_home(_launch(), child_home=child_home)
    assert (kind, home) == (
        "per_invocation",
        tmp_path / "invocations" / "<invocation-id>" / "home" / ".dsh",
    )
    surfaces = observe_launch_surfaces(
        _launch(), dsh_home=home, dsh_home_kind=kind, child_env={}, look_in_home=False
    )
    assert surfaces.dsh_home_observed is False and surfaces.dsh_home_files == []
    bound = tmp_path / "bound"
    assert effective_dsh_home(_launch(dsh_home=str(bound)), child_home=child_home) == (
        "bound",
        bound,
    )
    assert list(tmp_path.iterdir()) == [], "naming a home creates nothing"
