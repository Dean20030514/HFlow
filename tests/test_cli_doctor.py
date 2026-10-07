"""`hflow doctor` readiness, and what `main` does before and after any command.

Doctor is read-only (AGENTS rule 6): it never calls a model, never runs ``dsh`` (which may be the
DSH Desktop app) and never opens a credential file. The tests here prove those by planting
programs and files that would leave a trace if they were run or opened.

``main`` sets ``NoDefaultCurrentDirectoryInExePath`` before anything can start a process: on
Windows ``CreateProcess`` otherwise runs a bare program name from the parent's current directory
before ``PATH``.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hflow.cli as cli
from hflow.cli import (
    EXIT_BLOCKED,
    EXIT_IN_PROGRESS,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_RECORD_UNREADABLE,
    EXIT_REFUSED,
    EXIT_USAGE,
    forbid_current_directory_program_search,
    main,
)
from hflow.contracts import MachineProfile, canonical_json
from hflow.drivers.acpx_dsh import NO_CWD_EXE_SEARCH_ENV, find_on_path

from .conftest import write_profile, write_project, write_task

SENTINEL = "doctor-must-never-print-this-7f3a"


def _unset_no_cwd_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variable for this test and restore whatever was there afterwards.

    ``setenv`` first so the original state is recorded (``delenv`` of an absent name records
    nothing, and ``main`` would then leave the variable set for later tests).
    """
    monkeypatch.setenv(NO_CWD_EXE_SEARCH_ENV, "1")
    monkeypatch.delenv(NO_CWD_EXE_SEARCH_ENV)


def _doctor(data_dir: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict]:
    code = main(["doctor", "--json", "--data-dir", str(data_dir), *extra])
    return code, json.loads(capsys.readouterr().out)


def _program(directory: Path, name: str, marker: Path | None = None) -> Path:
    """A launcher stand-in named ``name`` that, if it is ever run, writes ``marker``."""
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = directory / f"{name}.cmd"
        body = f'@echo ran> "{marker}"\r\n' if marker else ""
        path.write_text(f"{body}@exit /b 0\r\n", encoding="utf-8")
    else:
        path = directory / name
        body = f"echo ran > '{marker}'\n" if marker else ""
        path.write_text(f"#!/bin/sh\n{body}exit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return path


# --------------------------------------------------------------------------
# main: no program is ever taken from the current directory
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32", reason="CreateProcess's current-directory search is Windows behaviour"
)
def test_a_git_planted_in_the_current_directory_never_runs_after_cli_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HFlow's own `git` calls are bare names; a `git.exe` in the cwd ran instead of Git.

    The planted program is a copy of ``whoami.exe``, so which one ran is visible in its output.
    """
    if not find_on_path("git", os.environ):
        pytest.skip("git is not on PATH")
    whoami = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    if not whoami.is_file():
        pytest.skip(f"{whoami} is not present")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copyfile(whoami, checkout / "git.exe")
    monkeypatch.chdir(checkout)
    _unset_no_cwd_search(monkeypatch)

    def bare_git_version() -> str:
        completed = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, errors="replace",
            timeout=60, check=False,
        )
        return completed.stdout.strip()

    assert not bare_git_version().startswith("git version"), (
        "precondition: without the setting, the git.exe planted in the cwd is what runs"
    )

    forbid_current_directory_program_search()

    assert os.environ[NO_CWD_EXE_SEARCH_ENV] == "1"
    assert bare_git_version().startswith("git version"), "the real Git must run, not the plant"


def test_main_sets_the_variable_in_exactly_one_spelling_before_any_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _unset_no_cwd_search(monkeypatch)
    if sys.platform != "win32":
        # Names are case-sensitive here, so a second spelling can exist; it is removed.
        monkeypatch.setenv(NO_CWD_EXE_SEARCH_ENV.lower(), "")

    assert main(["schema"]) == EXIT_OK
    capsys.readouterr()

    spellings = [key for key in os.environ if key.upper() == NO_CWD_EXE_SEARCH_ENV.upper()]
    assert len(spellings) == 1, spellings
    assert os.environ[spellings[0]] == "1"
    if sys.platform != "win32":
        assert spellings == [NO_CWD_EXE_SEARCH_ENV]


# --------------------------------------------------------------------------
# main: Ctrl+C and text output
# --------------------------------------------------------------------------


def test_ctrl_c_exits_130_and_points_at_the_recorded_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(args):  # noqa: ANN001, ANN202 - a command function's own shape
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", interrupted)

    code = main(["status", "R-anything", "--data-dir", str(tmp_path / "data")])

    captured = capsys.readouterr()
    assert code == EXIT_INTERRUPTED == 130
    assert captured.err.startswith("interrupted")
    assert "authoritative" in captured.err and "hflow status <run-id>" in captured.err
    assert captured.out == ""
    assert EXIT_INTERRUPTED not in {
        EXIT_OK, EXIT_REFUSED, EXIT_BLOCKED, EXIT_USAGE, EXIT_IN_PROGRESS, EXIT_RECORD_UNREADABLE,
    }


def test_text_lines_render_a_payload_for_a_reader() -> None:
    payload = {
        "run_id": "R-1",
        "block_code": None,
        "turns_reserved": 2,
        "workspace_matches_receipt": True,
        "notes": ["first", "second"],
        "issues": [{"code": "scope_violation", "path": "x"}],
        "warnings": [],
        "receipt": {"task_state": "ACCEPTED"},
    }
    assert cli._text_lines(payload).splitlines() == [
        "run_id: R-1",
        "block_code: null",
        "turns_reserved: 2",
        "workspace_matches_receipt: true",
        "notes:",
        "  - first",
        "  - second",
        "issues:",
        '  - {"code":"scope_violation","path":"x"}',
        "warnings: (none)",
        'receipt: {"task_state":"ACCEPTED"}',
    ]
    assert cli._text_lines("already text") == "already text"


def test_a_refused_run_prints_lines_in_text_mode_and_canonical_json_with_json(
    tmp_path: Path, project, task_spec, project_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task = write_task(tmp_path / "task.json", task_spec)
    project_file = write_project(tmp_path / "hflow" / "project.json", project)
    argv = [
        "run", "--task", str(task), "--project", str(project_file),
        "--project-root", str(project_root), "--driver", "acpx-dsh",
        "--data-dir", str(tmp_path / "data"),
    ]

    assert main(argv) == EXIT_REFUSED
    text = capsys.readouterr().out
    assert "{" not in text and "'refused'" not in text, text
    lines = text.splitlines()
    assert lines[0] == "refused: true"
    assert lines[1] == "reason: live_authorization_missing"
    assert lines[2].startswith("detail: this run resolves to a real Harness driver")

    assert main([*argv, "--json"]) == EXIT_REFUSED
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert out == canonical_json(payload) + "\n", "--json output stays canonical JSON"
    assert payload["reason"] == "live_authorization_missing"


# --------------------------------------------------------------------------
# doctor: programs come from absolute PATH entries, and dsh is never run
# --------------------------------------------------------------------------


def test_doctor_never_runs_dsh_and_reports_its_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dsh on PATH may be the Desktop app's shim; running it could start that app."""
    marker = tmp_path / "dsh-ran.txt"
    shim = _program(tmp_path / "bin", "dsh", marker)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    entry = payload["executables"]["dsh"]
    assert Path(entry["path"]).parent == shim.parent
    assert "version" not in entry
    assert "never starts dsh" in entry["note"]
    assert not marker.exists(), "doctor ran dsh"
    assert payload["readiness"]["dsh"]["on_path"] == entry["path"]
    assert "never runs dsh" in payload["readiness"]["dsh"]["detail"]

    assert main(["doctor", "--data-dir", str(tmp_path / "data")]) == EXIT_OK
    text = capsys.readouterr().out
    assert "(not run: doctor never starts dsh" in text
    assert not marker.exists()


def test_doctor_never_takes_a_program_from_the_current_directory_or_a_relative_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``shutil.which`` searched the cwd first on Windows and honoured relative PATH entries."""
    checkout = tmp_path / "checkout"
    marker = tmp_path / "planted-ran.txt"
    for name in ("acpx", "git", "node", "dsh"):
        _program(checkout, name, marker)
    _program(checkout / "rel", "acpx", marker)
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "rel", ""]))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-desktop-install"))

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    for name in ("acpx", "git", "node", "dsh"):
        assert payload["executables"][name] == {"path": None, "available": False}, name
    assert not marker.exists(), "a planted program ran"
    integrate = payload["readiness"]["integrate"]
    assert integrate["usable"] is False
    assert "no git on PATH" in integrate["detail"]


# --------------------------------------------------------------------------
# doctor readiness: integrate's Git minimum
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "usable", "words"),
    [
        ("git version 2.39.5", False, "hflow integrate: NOT USABLE (needs Git 2.40 or later; this Git is 2.39.5)"),
        ("git version 2.40.0", True, "hflow integrate: usable (Git 2.40.0; needs Git 2.40 or later)"),
        ("git version 2.56.0.windows.2", True, "hflow integrate: usable (Git 2.56.0"),
        ("not git at all", None, "hflow integrate: unknown (needs Git 2.40 or later"),
    ],
    ids=["too-old", "minimum", "vendor-suffix", "unrecognised"],
)
def test_doctor_judges_git_against_the_integration_minimum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reported: str,
    usable: bool | None,
    words: str,
) -> None:
    git_dir = tmp_path / "git-bin"
    git = _program(git_dir, "git", tmp_path / "git-ran.txt")
    monkeypatch.setenv("PATH", f"{git_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    probed: list[str] = []

    def version(program: str) -> str:
        probed.append(program)
        return reported if Path(program).parent == git_dir else "v0.0.0"

    monkeypatch.setattr(cli, "_doctor_version", version)

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK, "readiness never changes doctor's exit code"
    integrate = payload["readiness"]["integrate"]
    assert integrate["usable"] is usable
    assert integrate["minimum_git"] == "2.40"
    assert Path(integrate["git"]).parent == git_dir
    assert integrate["detail"].startswith(words), integrate["detail"]
    assert all(Path(program).is_absolute() for program in probed), probed
    assert Path(probed[0]).parent == git.parent

    assert main(["doctor", "--data-dir", str(tmp_path / "data")]) == EXIT_OK
    assert f"integrate     {words}" in capsys.readouterr().out


# --------------------------------------------------------------------------
# doctor readiness: the acpx entry (never PATH) and the dsh Desktop shim
# --------------------------------------------------------------------------


def _acpx_package(root: Path, version: str = "0.17.1") -> Path:
    package = root / "node_modules" / "acpx"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "acpx", "version": version}), encoding="utf-8"
    )
    entry = package / "dist" / "cli.js"
    entry.write_text("// stand-in, never run\n", encoding="utf-8")
    return entry


def test_doctor_says_plainly_that_acpx_is_not_found_and_where_it_is_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(tmp_path / "missing" / "cli.js"))
    on_path = _program(tmp_path / "acpx-bin", "acpx", tmp_path / "acpx-ran.txt")
    monkeypatch.setenv("PATH", f"{on_path.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    acpx = payload["readiness"]["acpx"]
    assert acpx["client_entry"] is None and acpx["acpx_version"] is None
    assert Path(acpx["on_path"]).parent == on_path.parent
    assert acpx["detail"].startswith("NOT FOUND")
    for words in ("$HFLOW_ACPX_CLI", "m0/acpx/node_modules/acpx/dist/cli.js", "is not used",
                  "A machine profile does not name the acpx entry"):
        assert words in acpx["detail"], words
    assert not (tmp_path / "acpx-ran.txt").exists()

    assert main(["doctor", "--data-dir", str(tmp_path / "data")]) == EXIT_OK
    assert "acpx client   NOT FOUND" in capsys.readouterr().out


def test_doctor_shows_the_acpx_entry_and_version_a_profile_would_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_profile: MachineProfile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read from the package's package.json: nothing is executed, and the profile's launch is
    the same resolution the run performs."""
    entry = _acpx_package(tmp_path / "acpx-install")
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(entry))
    # Never run here; a .js entry only needs an absolute interpreter to resolve.
    monkeypatch.setenv("HFLOW_ACPX_NODE", sys.executable)
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)

    code, payload = _doctor(data_dir, capsys, "--profile", "dsh-local")

    def same_file(reported: str) -> bool:
        # The launch records the entry's final path (links followed once).
        return os.path.normcase(os.path.realpath(reported)) == os.path.normcase(
            os.path.realpath(entry)
        )

    assert code == EXIT_OK, payload["profile"]["detail"]
    acpx = payload["readiness"]["acpx"]
    assert same_file(acpx["client_entry"]), acpx
    assert acpx["acpx_version"] == "0.17.1"
    assert "(acpx 0.17.1, read from its package.json; nothing executed)" in acpx["detail"]
    for role in ("implementer", "reviewer"):
        launch = payload["profile"]["roles"][role]["launch"]
        assert same_file(launch["client_entry"]), (role, launch)
        assert launch["acpx_version"] == "0.17.1", role
        assert launch["dsh_executable"], role
        assert launch["resolvable"] is True, role

    assert main(["doctor", "--profile", "dsh-local", "--data-dir", str(data_dir)]) == EXIT_OK
    text = capsys.readouterr().out
    implementer_entry = payload["profile"]["roles"]["implementer"]["launch"]["client_entry"]
    assert f"launch acpx={implementer_entry} (acpx 0.17.1) dsh=" in text


@pytest.mark.skipif(sys.platform != "win32", reason="the Desktop shim location is Windows-only")
@pytest.mark.parametrize("installed", [True, False], ids=["desktop-installed", "no-desktop"])
def test_doctor_points_at_the_desktop_dsh_shim_when_dsh_is_not_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installed: bool,
) -> None:
    local = tmp_path / "LocalAppData"
    shim = local / cli.DESKTOP_DSH_SHIM_RELATIVE
    marker = tmp_path / "desktop-shim-ran.txt"
    if installed:
        shim.parent.mkdir(parents=True)
        shim.write_text(f'@echo ran> "{marker}"\r\n@exit /b 0\r\n', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    monkeypatch.setenv("PATH", str(system32))  # no dsh, no conftest stand-in

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    dsh = payload["readiness"]["dsh"]
    assert dsh["on_path"] is None
    assert dsh["detail"].startswith("NOT FOUND on PATH")
    if installed:
        assert dsh["desktop_shim"] == str(shim)
        assert f"prepend {shim.parent} to PATH" in dsh["detail"]
        assert "A machine profile cannot name the dsh path" in dsh["detail"]
    else:
        assert dsh["desktop_shim"] is None
        assert "Desktop shim" not in dsh["detail"]
    assert not marker.exists(), "doctor ran the Desktop shim"


# --------------------------------------------------------------------------
# doctor readiness: credential sources, by name and presence only
# --------------------------------------------------------------------------


@pytest.fixture()
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path opened through ``open`` / ``io.open`` / ``os.open`` while the test runs."""
    seen: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def record(path: object) -> None:
        if isinstance(path, (str, os.PathLike)):
            seen.append(os.path.normcase(os.path.abspath(os.fspath(path))))

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        record(file)
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        record(file)
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        record(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    return seen


def test_doctor_reports_credential_sources_by_name_and_stat_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_profile: MachineProfile,
    acpx_client: Path,
    capsys: pytest.CaptureFixture[str],
    opened_paths: list[str],
) -> None:
    home = tmp_path / "bound-home"
    home.mkdir()
    credentials = home / ".credentials.yaml"
    env_file = home / ".env"
    credentials.write_text(f"token: {SENTINEL}\n", encoding="utf-8")
    env_file.write_text(f"DEEPSEEK_API_KEY={SENTINEL}\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(home))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SENTINEL)
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    opened_paths.clear()  # the writes above are the test's own

    code, payload = _doctor(data_dir, capsys, "--profile", "dsh-local")
    assert main(["doctor", "--profile", "dsh-local", "--data-dir", str(data_dir)]) == EXIT_OK
    text = capsys.readouterr().out

    assert opened_paths, "the guard saw no open at all, so it proves nothing"
    assert code == EXIT_OK
    found = payload["readiness"]["credentials"]
    assert found["DEEPSEEK_API_KEY"] == "present"
    assert found["child_dsh_home_kind"] == "bound"
    assert found["dsh_home_files"] == {".credentials.yaml": "present", ".env": "present"}
    assert "stat only, never opened" in found["detail"]
    assert "credentials   DEEPSEEK_API_KEY present" in text
    for rendered in (json.dumps(payload), text):
        assert SENTINEL not in rendered, "a credential value reached the output"
    protected = {os.path.normcase(str(credentials)), os.path.normcase(str(env_file))}
    assert not protected & set(opened_paths), "doctor opened a credential file"


def test_doctor_names_the_launch_environment_as_the_only_source_without_a_bound_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    code, payload = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    found = payload["readiness"]["credentials"]
    assert found["DEEPSEEK_API_KEY"] == "absent"
    assert found["child_dsh_home_kind"] == "per_invocation"
    assert found["dsh_home_files"] == {}
    assert "the launch environment is its only credential source" in found["detail"]
    assert "not observed" in found["detail"]
