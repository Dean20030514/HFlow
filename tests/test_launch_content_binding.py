"""H6 (user ruling 2026-10-03): the launch is bound by the content of its entry files.

At prepare the SHA-256 of the client interpreter (node.exe for the real acpx; the Python
interpreter for the stand-in client), the acpx entry and its package.json, the dsh launcher and -
for an npm carrier - the carrier entry and its package.json (for the installed Desktop carrier,
whose entry lies inside ``resources/app.asar``, that archive file) go into the resolved launch
and the authorization binding. The driver hashes them again just before spawn and refuses a
difference before any process exists. Everything here is offline: no model, no real DSH.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from hflow.authorization import (
    AuthorizationRecord,
    current_binding,
    verify_authorization,
)
from hflow.cli import EXIT_OK, main
from hflow.contracts import InvocationRequest, RefusalCode, RefusedError
from hflow.drivers.acpx_dsh import AcpxDshDriver, resolve_launch_config
from hflow.drivers.launch_content import (
    CONTENT_BINDING_LABEL,
    final_path,
    launch_content_changes,
)
from hflow.prepare import build_prepare_report, render_prepare_text, resolve_run

from .conftest import FAKE_ACPX_CLIENT, write_profile, write_project, write_task


@pytest.fixture()
def client_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in client the suite uses for HFLOW_ACPX_CLI, copied so it can be edited."""
    copy = tmp_path / "client" / "fake_acpx_client.py"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(FAKE_ACPX_CLIENT, copy)
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(copy))
    monkeypatch.delenv("HFLOW_ACPX_NODE", raising=False)
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    return copy


def _resolve(tmp_path: Path, project, task, project_root: Path, profile):
    data_dir = tmp_path / "data"
    write_profile(data_dir, profile)
    return resolve_run(
        task_path=write_task(tmp_path / "task.json", task),
        project_root=project_root,
        data_dir=data_dir,
        project_path=write_project(tmp_path / "hflow" / "project.json", project),
        profile_id=profile.profile_id,
    )


def _binding(resolved):
    return current_binding(
        mode="m2-live-change",
        driver="acpx-dsh",
        project=resolved.project,
        request=resolved.request(),
        spec_path=resolved.spec_path,
        effective=resolved.effective,
    )


def _record(binding) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="AUTH-content-1",
        user_text="I approve this task with these launch files.",
        authorized_at="2026-10-03T00:00:00Z",
        max_top_level_submissions=2,
        binding=binding,
    )


def _request(workspace: Path, data_dir: Path, facts: list) -> InvocationRequest:
    return InvocationRequest(
        invocation_id="I-content",
        attempt_id="A-1",
        run_id="R-1",
        role="implementer",
        task_id="T-1",
        task_revision=1,
        goal="do the thing",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        data_dir=str(data_dir),
        on_spawn=facts.append,
    )


def test_prepare_binds_the_launch_entry_files_by_content(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, client_copy,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolved = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    launch = resolved.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None and launch.resolvable, launch
    kinds = {item.kind: item for item in launch.content_digests}
    # The stand-in is a loose .py client: interpreter, entry and the dsh launcher stand-in.
    assert set(kinds) == {"client_interpreter", "client_entry", "dsh_launcher"}
    assert kinds["client_entry"].path == str(client_copy)
    assert kinds["client_interpreter"].path == launch.client_argv_prefix[0]
    assert kinds["dsh_launcher"].path == launch.dsh_executable
    assert all(item.sha256.startswith("sha256:") for item in kinds.values())
    assert any("no package.json" in note for note in launch.content_notes)

    binding = _binding(resolved)
    assert binding.launch_content_digest == resolved.effective.launch_content_digest()
    assert binding.launch_content_digest.startswith("sha256:")

    report = build_prepare_report(resolved)
    assert report.authorization.binding["launch_content_digest"] == binding.launch_content_digest
    text = render_prepare_text(report)
    assert f"content ({CONTENT_BINDING_LABEL})" in text
    assert f"content client_entry {kinds['client_entry'].sha256}" in text
    assert f"launch_content_digest {binding.launch_content_digest}" in text
    assert any(note.startswith(CONTENT_BINDING_LABEL) for note in report.notes)
    assert "binary verified" not in text

    # The JSON form carries the same digests in the launch and in the pending binding.
    data_dir = tmp_path / "data"
    code = main(
        [
            "prepare",
            "--task", str(tmp_path / "task.json"),
            "--project", str(tmp_path / "hflow" / "project.json"),
            "--project-root", str(project_root),
            "--profile", live_profile.profile_id,
            "--json",
            "--data-dir", str(data_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    roles = {entry["role"]: entry for entry in payload["effective_config"]["roles"]}
    digests = roles["implementer"]["launch"]["content_digests"]
    assert {entry["kind"] for entry in digests} == set(kinds)
    assert payload["authorization"]["binding"]["launch_content_digest"] == (
        binding.launch_content_digest
    )


def test_unchanged_files_pass_both_checks(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, client_copy,
) -> None:
    first = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    again = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    verify_authorization(_record(_binding(first)), expected=_binding(again))
    launch = again.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None
    assert launch_content_changes(launch) == []
    assert AcpxDshDriver(data_dir=again.data_dir, launch=launch)._launch_content_refusal() == ""


def test_editing_the_client_after_prepare_refuses_the_approval_and_the_spawn(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, client_copy,
) -> None:
    prepared = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    approved = _record(_binding(prepared))
    launch = prepared.effective.role("implementer").launch  # type: ignore[union-attr]
    assert launch is not None
    driver = AcpxDshDriver(data_dir=prepared.data_dir, launch=launch)

    # Same path, different bytes.
    client_copy.write_bytes(client_copy.read_bytes() + b"\n# replaced after approval\n")

    later = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    # The configuration is the same configuration; only the content differs.
    assert later.effective.digest() == prepared.effective.digest()
    assert later.effective.launch_content_digest() != prepared.effective.launch_content_digest()
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(approved, expected=_binding(later))
    assert excinfo.value.code is RefusalCode.LAUNCH_CONTENT_CHANGED
    assert "hflow prepare" in excinfo.value.message

    # The driver built before the edit hashes again at its spawn gate and starts nothing.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    facts: list = []
    with pytest.raises(RefusedError) as spawn:
        driver.start_handle(_request(workspace, prepared.data_dir, facts))
    assert spawn.value.code is RefusalCode.LAUNCH_CONTENT_CHANGED
    assert "client_entry" in spawn.value.message and str(client_copy) in spawn.value.message
    assert [fact.created for fact in facts] == [False]
    assert driver._processes == {} and driver._handles == {}
    assert not (workspace / "agent-spawns.jsonl").exists(), "the client must not be launched"


def test_a_legacy_artifact_without_launch_content_is_refused_when_digests_resolve(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, client_copy,
) -> None:
    resolved = _resolve(tmp_path, live_project, worktree_task, project_root, live_profile)
    expected = _binding(resolved)
    legacy = expected.model_copy(update={"launch_content_digest": ""})
    with pytest.raises(RefusedError) as excinfo:
        verify_authorization(_record(legacy), expected=expected)
    assert excinfo.value.code is RefusalCode.RISK_DOWNGRADE
    assert "launch content binding" in excinfo.value.message

    # Dropped from the digest while empty: the legacy binding digests as it did before H6
    # (the same recipe without the key), so a consumed legacy ledger row keeps matching.
    from hflow.authorization import POST_BINDING_FIELDS
    from hflow.contracts import digest_of

    payload = legacy.model_dump(mode="json")
    payload.pop("launch_content_digest")
    for field in POST_BINDING_FIELDS:
        if not payload.get(field):
            payload.pop(field, None)
    assert legacy.digest() == digest_of(payload)
    assert legacy.digest() != expected.digest()


def test_a_node_entry_without_its_package_json_is_not_resolvable(tmp_path: Path) -> None:
    entry = tmp_path / "npm" / "node_modules" / "acpx" / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// stand-in", encoding="utf-8")
    node = tmp_path / "nodejs" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(b"MZ-node-stand-in")
    launch = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=entry,
        node_executable=str(node),
        agent_argv_override=[sys.executable, "-c", "pass"],
    )
    assert launch.resolvable is False
    assert "client_package_json" in launch.detail and launch.content_digests == []

    manifest = entry.parent.parent / "package.json"
    manifest.write_text('{"name": "acpx", "version": "0.17.1"}', encoding="utf-8")
    bound = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=entry,
        node_executable=str(node),
        agent_argv_override=[sys.executable, "-c", "pass"],
    )
    assert bound.resolvable, bound.detail
    paths = {item.kind: item.path for item in bound.content_digests}
    assert paths == {
        "client_interpreter": str(node),
        "client_entry": str(entry),
        "client_package_json": str(manifest),
    }
    # Editing the package.json (same path) is a content change at the spawn gate.
    manifest.write_text('{"name": "acpx", "version": "0.99.0"}', encoding="utf-8")
    changes = launch_content_changes(bound)
    assert len(changes) == 1 and changes[0].startswith("client_package_json")


def test_an_npm_carrier_binds_its_entry_and_package_json(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("the carrier shims are Windows batch files")
    cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    shim = tmp_path / "npm" / "dsh.CMD"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        '@"%~dp0\\node_modules\\@deepseek-ai\\dsh\\bin\\dsh.js" %*\r\n', encoding="utf-8"
    )
    package = shim.parent / "node_modules" / "@deepseek-ai" / "dsh"
    script = package / "bin" / "dsh.js"
    script.parent.mkdir(parents=True)
    script.write_text("// carrier entry stand-in", encoding="utf-8")
    (package / "package.json").write_text('{"version": "0.2.0-rc.2"}', encoding="utf-8")

    launch = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=FAKE_ACPX_CLIENT,
        python_executable=sys.executable,
        dsh_executable=str(shim),
        env={"SystemRoot": os.environ["SystemRoot"], "PATH": ""},
    )
    assert launch.resolvable, launch.detail
    assert launch.agent_argv[:3] == [str(cmd), "/c", str(shim)]
    paths = {item.kind: item.path for item in launch.content_digests}
    assert paths["dsh_launcher"] == str(shim)
    assert os.path.normcase(paths["carrier_entry"]) == os.path.normcase(str(script))
    assert os.path.normcase(paths["carrier_package_json"]) == os.path.normcase(
        str(package / "package.json")
    )
    assert "carrier_archive" not in paths
    assert any("bound by path only" in note for note in launch.content_notes)

    script.write_text("// replaced carrier entry", encoding="utf-8")
    changes = launch_content_changes(launch)
    assert len(changes) == 1 and changes[0].startswith("carrier_entry"), changes

    # A classified shim whose script is gone cannot be bound, so it is not resolvable.
    script.unlink()
    gone = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=FAKE_ACPX_CLIENT,
        python_executable=sys.executable,
        dsh_executable=str(shim),
        env={"SystemRoot": os.environ["SystemRoot"], "PATH": ""},
    )
    assert gone.resolvable is False and "carrier entry" in gone.detail


#: The installed DeepSeek Harness Desktop shim, byte for byte as read from
#: ``<install>/resources/runtime/cli/bin/dsh.cmd`` on 2026-10-03 (Desktop 0.2.0-rc.2).
DESKTOP_SHIM_TEXT = (
    "@echo off\r\n"
    "setlocal DisableDelayedExpansion\r\n"
    'set "ELECTRON_RUN_AS_NODE=1"\r\n'
    '"%~dp0..\\..\\..\\..\\DeepSeek Harness.exe" --expose-internals '
    '"%~dp0..\\..\\..\\app.asar\\dsh\\node_modules\\@deepseek-ai\\dsh-desktop-host\\lib\\cli.js" '
    "%*\r\n"
    "exit /b %errorlevel%\r\n"
)


def _desktop_install(root: Path) -> tuple[Path, Path]:
    """The installed Desktop layout: ``(shim, resources/app.asar)``.

    ``app.asar`` is a regular file (an Electron archive) and the shim's script lies inside it;
    ``app.asar.unpacked`` holds only native modules, not the entry, as installed.
    """
    install = root / "DeepSeek Harness"
    resources = install / "resources"
    install.mkdir(parents=True)
    (install / "DeepSeek Harness.exe").write_bytes(b"MZ-electron-stand-in")
    shim = resources / "runtime" / "cli" / "bin" / "dsh.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(DESKTOP_SHIM_TEXT.encode("utf-8"))
    archive = resources / "app.asar"
    archive.write_bytes(b"\x04\x00\x00\x00asar-header-stand-in" + bytes(4096))
    unpacked = (
        resources / "app.asar.unpacked" / "dsh" / "node_modules" / "@deepseek-ai"
        / "dsh-desktop-host" / "node_modules" / "@koromix" / "koffi-win32-x64"
    )
    unpacked.mkdir(parents=True)
    return shim, archive


def test_the_installed_desktop_carrier_binds_its_app_asar_archive(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("the carrier shims are Windows batch files")
    shim, archive = _desktop_install(tmp_path)

    def resolve():
        return resolve_launch_config(
            data_dir=tmp_path / "data",
            acpx_cli=FAKE_ACPX_CLIENT,
            python_executable=sys.executable,
            dsh_executable=str(shim),
            env={"SystemRoot": os.environ["SystemRoot"], "PATH": ""},
        )

    launch = resolve()
    assert launch.resolvable, launch.detail
    paths = {item.kind: item.path for item in launch.content_digests}
    assert paths["dsh_launcher"] == str(shim)
    assert os.path.normcase(paths["carrier_archive"]) == os.path.normcase(str(archive))
    # The entry and its package.json exist only inside the archive: never listed as files.
    assert "carrier_entry" not in paths and "carrier_package_json" not in paths
    assert f"the carrier entry inside {archive} is bound through the archive's digest" in (
        launch.content_notes
    )
    assert any("app.asar.unpacked are bound by path only" in n for n in launch.content_notes)
    assert any("DeepSeek Harness.exe" in n and "path only" in n for n in launch.content_notes)
    assert launch_content_changes(launch) == []
    driver = AcpxDshDriver(data_dir=tmp_path / "data", launch=launch)
    assert driver._launch_content_refusal() == ""

    # Same path, same size, different bytes: the spawn gate refuses the launch it resolved.
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    changes = launch_content_changes(launch)
    assert len(changes) == 1 and changes[0].startswith("carrier_archive"), changes
    assert "carrier_archive" in driver._launch_content_refusal()

    # The entry also present unpacked: which copy Electron runs depends on the archive header,
    # which is not parsed, so the launch is not resolvable.
    twin = (
        archive.parent / "app.asar.unpacked" / "dsh" / "node_modules" / "@deepseek-ai"
        / "dsh-desktop-host" / "lib" / "cli.js"
    )
    twin.parent.mkdir(parents=True)
    twin.write_text("// unpacked copy", encoding="utf-8")
    both = resolve()
    assert both.resolvable is False and "unpacked" in both.detail
    shutil.rmtree(twin.parent)

    # Neither the entry nor an archive holding it: refused as before.
    archive.unlink()
    gone = resolve()
    assert gone.resolvable is False and "carrier entry" in gone.detail
    assert "is not a file" in driver._launch_content_refusal()

    # A directory named app.asar that lacks the entry is not an archive and binds nothing.
    archive.mkdir()
    not_archive = resolve()
    assert not_archive.resolvable is False and "is not a file" in not_archive.detail


def test_a_missing_launch_file_is_refused_at_resolution(tmp_path: Path) -> None:
    missing_dsh = tmp_path / "bin" / "dsh.exe"
    launch = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=FAKE_ACPX_CLIENT,
        python_executable=sys.executable,
        dsh_executable=str(missing_dsh),
        env={"SystemRoot": os.environ.get("SystemRoot", ""), "PATH": ""},
    )
    assert launch.resolvable is False
    assert "dsh_launcher" in launch.detail and str(missing_dsh) in launch.detail


def test_a_linked_entry_is_hashed_and_spawned_at_its_final_path(tmp_path: Path) -> None:
    target = tmp_path / "real" / "client.py"
    target.parent.mkdir()
    shutil.copyfile(FAKE_ACPX_CLIENT, target)
    # A directory junction where symbolic links need a privilege this account may not have.
    linked_dir = tmp_path / "link"
    try:
        os.symlink(target.parent, linked_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("this account cannot create symbolic links")
        import subprocess

        made = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(linked_dir), str(target.parent)],
            capture_output=True,
            check=False,
        )
        if made.returncode != 0:
            pytest.skip("neither a symbolic link nor a junction could be created")
    link = linked_dir / "client.py"
    assert final_path(str(link)) == os.path.realpath(link)
    launch = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=link,
        python_executable=sys.executable,
        agent_argv_override=[sys.executable, "-c", "pass"],
    )
    assert launch.resolvable, launch.detail
    entry = next(item for item in launch.content_digests if item.kind == "client_entry")
    # One path for both: the one the content binding hashed is the one the client argv starts.
    assert launch.client_entry == entry.path == os.path.realpath(link)
    driver = AcpxDshDriver(data_dir=tmp_path / "data", launch=launch)
    assert driver._client_argv(tmp_path, tmp_path, 60)[2] == entry.path


def test_the_probe_and_so_doctor_print_the_digests_with_the_honest_label(
    tmp_path: Path, client_copy: Path,
) -> None:
    from hflow.contracts import AgentBinding

    driver = AcpxDshDriver(
        data_dir=tmp_path / "data",
        acpx_cli=client_copy,
        python_executable=sys.executable,
    )
    notes = driver.probe(
        AgentBinding(harness="dsh", driver="acpx-dsh", model_selection="native_profile")
    ).notes
    entry = next(item for item in driver.launch.content_digests if item.kind == "client_entry")
    assert any(CONTENT_BINDING_LABEL in note for note in notes)
    assert any(entry.sha256 in note and "client_entry" in note for note in notes)
    assert any("exec-by-handle" in note for note in notes)
    assert not any("binary verified" in note for note in notes)
