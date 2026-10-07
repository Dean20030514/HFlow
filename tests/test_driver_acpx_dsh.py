"""Driver contract tests: launch, observe, stop, reconcile - all offline.

The production driver is exercised through a test-only client that reproduces the acpx
contract (structured-argv config, ``--format json``, ``exec -f -`` with the task on stdin)
and through stub agents that are *separate processes*. That matters for the stop tests: a
process tree that ignores cancellation can only be stopped by the managed boundary, so the
test would fail if the boundary were decorative.

No test here sends a model request, and none needs a credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from hflow.contracts import (
    AgentBinding,
    AttemptState,
    CapabilityState,
    DeliveryState,
    EventKind,
    EvidenceStatus,
    InvocationOutcome,
    InvocationRequest,
    ModelApplied,
    RefusalCode,
    RefusedError,
    RunRequest,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import (
    DRIVER_ID,
    AcpxDshDriver,
    DriverSetupError,
    _workspace_client_config,
    resolve_launch_config,
)
from hflow.drivers import winjob
from hflow.drivers.acp_events import V1_STOP_REASONS, project_line
from hflow.drivers.winjob import process_gone
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner
from tests.conftest import STAND_IN_API_KEY
from tests.test_winjob import ERROR_ACCESS_DENIED, kernel32_stand_in

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLIENT = FIXTURES / "fake_acpx_client.py"
#: A client whose only job is to report the environment it was started with.
ENV_REPORT_CLIENT = FIXTURES / "env_report_client.py"
STUB_AGENT = FIXTURES / "stub_acp_agent.py"

#: A real Job Object is required before anything about a process *tree* may be claimed.
requires_job_object = pytest.mark.skipif(
    not winjob.IS_WINDOWS,
    reason="needs a Windows Job Object: elsewhere the boundary covers the direct child only",
)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class DriverHarness:
    """Owns one driver plus the temp roots its invocation state must stay inside."""

    def __init__(
        self,
        tmp_path: Path,
        mode: str,
        *,
        delay_before_prompt: float = 0.0,
        max_raw_log_bytes: int | None = None,
        binding: AgentBinding | None = None,
    ) -> None:
        self.mode = mode
        self.data_dir = (tmp_path / "data").resolve()
        self.workspace = (tmp_path / "ws").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Stub markers live in their own scoped subdirectory. The stub is harness scaffolding,
        # not a worker changing project files: leaving its files loose in the workspace would
        # make every controller run look like an out-of-scope write.
        self.scratch = self.workspace / "stub-scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.delay_before_prompt = delay_before_prompt
        stub_argv = [sys.executable, "-u", str(STUB_AGENT), mode]
        if delay_before_prompt:
            # The fake client passes this through so a cancel can arrive pre-dispatch.
            stub_argv += ["--delay", str(delay_before_prompt)]
        extra: dict[str, object] = {}
        if max_raw_log_bytes is not None:
            # Retention is configurable for exactly this reason: driving the overflow path should
            # not require generating tens of megabytes of output.
            extra["max_raw_log_bytes"] = max_raw_log_bytes
        self.driver = AcpxDshDriver(
            data_dir=self.data_dir,
            acpx_cli=FAKE_CLIENT,
            python_executable=sys.executable,
            completion_timeout_seconds=90,
            agent_argv_override=stub_argv,
            binding=binding,
            **extra,  # type: ignore[arg-type]
        )
        # The stub keeps its marker files in STUB_SCRATCH_DIR; the fake client inherits the
        # driver's child environment, so pointing it here keeps scaffolding out of the
        # project paths the controller snapshots.
        self.driver.extra_env["STUB_SCRATCH_DIR"] = str(self.scratch)

    def start(self, invocation_id: str = "I-1", *, attempt_id: str = "A-1", goal: str = "do the thing"):
        request = InvocationRequest(
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            run_id="R-1",
            role="implementer",
            task_id="T-1",
            task_revision=1,
            goal=goal,
            acceptance=[],
            write_allow=["src/x.py"],
            write_deny=[],
            workspace=str(self.workspace),
            deadline_seconds=60,
            spec_digest="sha256:test",
            data_dir=str(self.data_dir),
        )
        handle = self.driver.start_handle(request)
        return handle, request

    def stub_files(self, suffix: str) -> list[Path]:
        return sorted(self.scratch.glob(f"stub-*-*.{suffix}"))

    def spawn_log(self) -> Path:
        """Written by the fake client when it launches an agent: the client-path marker."""
        return self.workspace / "agent-spawns.jsonl"

    def wait_for_stub_marker(self, suffix: str, timeout: float = 20.0, *, non_empty: bool = False) -> Path:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for candidate in self.stub_files(suffix):
                if not non_empty or candidate.stat().st_size > 0:
                    return candidate
            time.sleep(0.05)
        raise AssertionError(f"stub never wrote a .{suffix} marker in {self.scratch}")

    def wait_for_dispatch(self, handle, timeout: float = 20.0) -> bool:
        """Wait until the driver observed the session/prompt marker."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if handle.dispatched:
                return True
            time.sleep(0.05)
        return False


@pytest.fixture()
def harness_factory(tmp_path: Path):
    created: list[DriverHarness] = []

    def make(
        mode: str,
        *,
        delay_before_prompt: float = 0.0,
        max_raw_log_bytes: int | None = None,
        binding: AgentBinding | None = None,
    ) -> DriverHarness:
        harness = DriverHarness(
            tmp_path / f"{mode}-{len(created)}",
            mode,
            delay_before_prompt=delay_before_prompt,
            max_raw_log_bytes=max_raw_log_bytes,
            binding=binding,
        )
        created.append(harness)
        return harness

    yield make
    for harness in created:
        # Never leave a boundary or a child behind, whatever the test asserted.
        for invocation_id in list(harness.driver._handles):
            try:
                harness.driver.cancel_handle(harness.driver._handles[invocation_id])
            except Exception:  # noqa: BLE001
                pass
            harness.driver.release(invocation_id)


def _collect_events(driver: AcpxDshDriver, handle, limit: float = 60.0):
    events = []
    started = time.time()
    for event in driver.observe(handle):
        events.append(event)
        if time.time() - started > limit:
            break
    return events


def test_client_argv_uses_the_right_interpreter_for_the_entry_point(tmp_path: Path) -> None:
    """A Node CLI entry point must not be handed to the Python interpreter.

    This is a regression test for a real failure: the live trial launched acpx's
    ``dist/cli.js`` as ``python -u cli.js``, which cannot parse JavaScript, so the client
    died before the harness ever saw the task.

    The entry points are created for real: a launch is resolved from programs that exist, and
    a path that is not there is refused rather than assumed to be launchable.
    """
    node_cli = tmp_path / "node_modules" / "acpx" / "dist" / "cli.js"
    node_cli.parent.mkdir(parents=True)
    node_cli.write_text("// the client entry point, not executed here\n", encoding="utf-8")
    # A Node entry's package.json is a launch entry file bound by content (H6).
    (node_cli.parent.parent / "package.json").write_text('{"name": "acpx"}', encoding="utf-8")
    node_entry = AcpxDshDriver(data_dir=tmp_path, acpx_cli=node_cli)
    argv = node_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0] == node_entry.node_executable
    assert argv[1].endswith("cli.js")
    assert "exec" in argv and argv[-2:] == ["-f", "-"]

    python_entry = AcpxDshDriver(data_dir=tmp_path, acpx_cli=FAKE_CLIENT)
    argv = python_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0] == python_entry.python_executable
    assert argv[1] == "-u"
    assert argv[2].endswith("fake_acpx_client.py")

    shim = tmp_path / "tools" / "acpx.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    shim_entry = AcpxDshDriver(data_dir=tmp_path, acpx_cli=shim)
    argv = shim_entry._client_argv(tmp_path, tmp_path, 60)
    assert argv[0].endswith("acpx.cmd"), "a real executable is launched directly"


def test_a_launch_whose_entry_point_is_missing_is_refused(tmp_path: Path) -> None:
    """An explicit client path is not assumed to be launchable: it has to exist."""
    with pytest.raises(DriverSetupError) as excinfo:
        AcpxDshDriver(data_dir=tmp_path, acpx_cli=tmp_path / "gone" / "cli.js")
    assert "acpx CLI not found" in str(excinfo.value)


# --------------------------------------------------------------------------
# the bound launch is what the child gets, not what the environment says now
# --------------------------------------------------------------------------


def _observe_child_env(driver: AcpxDshDriver, tmp_path: Path) -> dict[str, object]:
    """Start the reporting client for real and return the environment *it* observed.

    Through the driver's own launch path (same argv construction, same boundary, same child
    environment), so the answer is a fact about a process rather than about the mapping that
    was supposed to produce it.
    """
    result = driver.readonly_client_check(["--version"], timeout_seconds=90)
    assert result["returncode"] == 0, result
    return json.loads(str(result["stdout"]).strip().splitlines()[-1])


def test_a_launch_that_bound_no_dsh_home_keeps_it_out_of_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved without DSH_HOME, and one appears afterwards: the child must still not see it.

    The resolution pins *absence*, not just presence. Copying the ambient environment and only
    overriding a non-empty bound home would let a variable added after the launch was resolved
    reach the process - the approval would name one launch and the process would run another.
    """
    monkeypatch.delenv("DSH_HOME", raising=False)
    launch = resolve_launch_config(data_dir=tmp_path, acpx_cli=ENV_REPORT_CLIENT)
    assert launch.dsh_home == "", "an absent DSH_HOME is resolved as absent, not as a default"

    # `extra_env` is not a way around the binding either.
    driver = AcpxDshDriver(
        data_dir=tmp_path, launch=launch, extra_env={"DSH_HOME": "smuggled-by-extra-env"}
    )
    monkeypatch.setenv("DSH_HOME", "appeared-after-resolution")

    assert "DSH_HOME" not in driver._child_env(tmp_path)

    observed = _observe_child_env(driver, tmp_path)
    assert observed["dsh_home_present"] is False, observed
    assert observed["dsh_home"] is None, observed


def test_a_launch_that_bound_a_dsh_home_keeps_it_after_the_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved with A, changed to B afterwards: the child must still get A."""
    bound_home = tmp_path / "bound-home"
    bound_home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(bound_home))
    launch = resolve_launch_config(data_dir=tmp_path, acpx_cli=ENV_REPORT_CLIENT)
    assert launch.dsh_home == str(bound_home)

    driver = AcpxDshDriver(
        data_dir=tmp_path, launch=launch, extra_env={"DSH_HOME": "changed-by-extra-env"}
    )
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "something-else"))

    assert driver._child_env(tmp_path)["DSH_HOME"] == str(bound_home)

    observed = _observe_child_env(driver, tmp_path)
    assert observed["dsh_home_present"] is True, observed
    assert observed["dsh_home"] == str(bound_home), observed


def test_ambient_dsh_mode_variables_never_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DSH reads its sandbox and tool modes from the environment at launch.

    An ambient ``DSH_PERMISSION_MODE=danger-full-access`` would silently drop DSH's file sandbox
    and set its approval policy to "never" for every role, the reviewer included - nothing in
    the resolved launch would say so. The driver removes both variables (it never sets them),
    and ``extra_env`` cannot put them back. The probe says which ones it found and removed.
    """
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    monkeypatch.setenv("DSH_TOOLS_MODE", "full")
    launch = resolve_launch_config(data_dir=tmp_path, acpx_cli=ENV_REPORT_CLIENT)
    driver = AcpxDshDriver(
        data_dir=tmp_path, launch=launch, extra_env={"DSH_PERMISSION_MODE": "danger-full-access"}
    )

    env = driver._child_env(tmp_path)
    assert "DSH_PERMISSION_MODE" not in env
    assert "DSH_TOOLS_MODE" not in env

    observed = _observe_child_env(driver, tmp_path)
    assert observed["dsh_permission_mode_present"] is False, observed
    assert observed["dsh_tools_mode_present"] is False, observed

    notes = driver.probe(AgentBinding(harness="dsh", driver=DRIVER_ID)).notes
    removed = [note for note in notes if "DSH_PERMISSION_MODE" in note]
    assert removed and "removed" in removed[0] and "DSH_TOOLS_MODE" in removed[0], notes


@pytest.mark.skipif(os.name != "nt", reason="the batch-shim wrapper exists only on Windows")
def test_the_batch_shim_wrapper_is_the_absolute_system_cmd_exe(tmp_path: Path) -> None:
    """A bare ``cmd.exe`` is searched for in the agent's cwd - the workspace - before PATH.

    So a ``cmd.exe`` planted at the worktree root could run as the launcher. The wrapper is the
    absolute ``<SystemRoot>\\System32\\cmd.exe`` taken from the environment the launch is
    resolved from, and a launch whose command processor cannot be found is not resolvable.
    """
    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    cmd = system_root / "System32" / "cmd.exe"
    cmd.write_bytes(b"")
    shim = tmp_path / "bin" / "dsh.CMD"
    # The launcher file exists: a launch binds its content (H6), so a missing one is refused.
    _launcher_stand_in(shim.parent, shim.name)

    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        dsh_executable=str(shim),
        python_executable=sys.executable,
        env={"SystemRoot": str(system_root)},
    )
    assert launch.resolvable, launch.detail
    assert launch.agent_argv == [str(cmd), "/c", str(shim), "--profile", "acp"]
    assert Path(launch.agent_argv[0]).is_absolute()

    missing = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        dsh_executable=str(shim),
        python_executable=sys.executable,
        env={"SystemRoot": str(tmp_path / "no-such-windows")},
    )
    assert missing.resolvable is False
    assert "cmd.exe" in missing.detail
    assert "cmd.exe" not in missing.agent_argv, "a bare cmd.exe is never a fallback"


#: The launcher's file name as the DSH install has it: an npm batch shim on Windows.
DSH_LAUNCHER_NAME = "dsh.CMD" if os.name == "nt" else "dsh"


def _launcher_stand_in(directory: Path, name: str = DSH_LAUNCHER_NAME) -> Path:
    """A file that PATH lookup finds as ``name``. Resolution only; nothing here runs it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = "@exit /b 1\r\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n"
    path.write_text(body, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path


def _resolution_env(*path_entries: str) -> dict[str, str]:
    """The environment a launch is resolved from: only the given PATH, plus what Windows needs."""
    env = {"PATH": os.pathsep.join(path_entries)}
    if os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return env


def test_a_dsh_that_is_not_on_path_is_not_resolvable_and_never_a_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """acpx spawns the agent with the workspace as its cwd, so a bare ``dsh`` can run a file there.

    Neither the HFlow process's own cwd (``shutil.which`` searches it first on Windows and returns
    a *relative* path, later resolved against the worktree) nor a relative PATH entry may supply
    the launcher, and a launcher that is not found is not resolvable - never the bare name.
    """
    planted_cwd = tmp_path / "cwd"
    _launcher_stand_in(planted_cwd)
    monkeypatch.chdir(planted_cwd)
    (tmp_path / "empty").mkdir()

    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        env=_resolution_env(str(tmp_path / "empty"), ".", ""),
    )

    assert launch.resolvable is False, launch
    assert "dsh" in launch.detail and "PATH" in launch.detail, launch.detail
    assert launch.agent_argv == [], "no argv may be built around a launcher that was not found"
    assert launch.dsh_executable == ""


def test_dsh_is_resolved_to_an_absolute_path_from_path_only(tmp_path: Path) -> None:
    """The launch names the absolute file PATH lookup found - the file an approval covers."""
    installed = _launcher_stand_in(tmp_path / "npm")

    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        python_executable=sys.executable,
        env=_resolution_env(str(tmp_path / "npm")),
    )

    assert launch.resolvable, launch.detail
    assert Path(launch.dsh_executable).is_absolute()
    assert os.path.normcase(launch.dsh_executable) == os.path.normcase(str(installed))
    assert Path(launch.agent_argv[0]).is_absolute(), launch.agent_argv
    assert os.path.normcase(str(installed)) in [os.path.normcase(a) for a in launch.agent_argv]


@pytest.mark.parametrize(
    "explicit",
    [
        {"dsh_executable": "dsh"},
        {"dsh_executable": f".{os.sep}{DSH_LAUNCHER_NAME}"},
        {"python_executable": "python"},
        {"agent_argv_override": ["agent", "--profile", "acp"]},
    ],
    ids=["bare-dsh", "relative-dsh", "bare-python", "bare-override"],
)
def test_an_explicit_relative_program_is_not_resolvable(
    tmp_path: Path, explicit: dict[str, object]
) -> None:
    """An explicit program is held to the same rule: a relative name is resolved somewhere else."""
    _launcher_stand_in(tmp_path / "npm")

    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        env=_resolution_env(str(tmp_path / "npm")),
        **explicit,  # type: ignore[arg-type]
    )

    assert launch.resolvable is False, launch
    assert "absolute" in launch.detail, launch.detail


@pytest.mark.parametrize("program", ["dsh", "python"])
def test_a_launch_program_inside_the_workspace_is_not_resolvable(
    tmp_path: Path, program: str
) -> None:
    """A file the agent can write must never run as the launcher or the client interpreter."""
    workspace = tmp_path / "project"
    outside = tmp_path / "npm"
    _launcher_stand_in(outside)
    explicit: dict[str, object] = {}
    path_entries = [str(outside)]
    if program == "dsh":
        _launcher_stand_in(workspace / "node_modules" / ".bin")
        path_entries.insert(0, str(workspace / "node_modules" / ".bin"))
    else:
        planted = workspace / ".venv" / "python.exe"
        planted.parent.mkdir(parents=True)
        planted.write_bytes(b"")
        explicit["python_executable"] = str(planted)

    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=FAKE_CLIENT,
        env=_resolution_env(*path_entries),
        workspaces=[workspace],
        **explicit,  # type: ignore[arg-type]
    )

    assert launch.resolvable is False, launch
    assert "inside the workspace" in launch.detail, launch.detail
    assert str(workspace) in launch.detail


@pytest.mark.parametrize(
    "layout", ["node_modules", "loose", "workspace_in_module_tree", "elsewhere"]
)
def test_an_acpx_client_entry_the_agent_can_write_is_not_resolvable(
    tmp_path: Path, layout: str
) -> None:
    """The interpreter is not the only file that runs: the acpx entry it executes is one too.

    An entry inside a workspace - in a project-local ``node_modules`` or loose - is refused like
    a planted interpreter, and so is a workspace inside the ``node_modules`` tree the entry
    loads its modules from (the agent could rewrite a dependency). An entry whose package lies
    elsewhere resolves, with an absolute node and dsh outside the workspace.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "npm"
    _launcher_stand_in(outside)
    node = outside / "node.exe"
    node.write_bytes(b"")
    workspaces = [workspace]
    if layout == "node_modules":
        entry = workspace / "node_modules" / "acpx" / "dist" / "cli.js"
    elif layout == "loose":
        entry = workspace / "tools" / "cli.js"
    else:
        entry = tmp_path / "client" / "node_modules" / "acpx" / "dist" / "cli.js"
        if layout == "workspace_in_module_tree":
            dependency = tmp_path / "client" / "node_modules" / "some-dependency"
            dependency.mkdir(parents=True)
            workspaces = [workspace, dependency]
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// stand-in; resolution only", encoding="utf-8")
    if layout != "loose":
        # The package.json of the entry's package is bound by content too (H6).
        (entry.parent.parent / "package.json").write_text('{"name": "acpx"}', encoding="utf-8")

    launch = resolve_launch_config(
        data_dir=tmp_path / "data",
        acpx_cli=entry,
        node_executable=str(node),
        env=_resolution_env(str(outside)),
        workspaces=workspaces,
    )

    if layout == "elsewhere":
        assert launch.resolvable is True, launch.detail
        assert launch.detail == ""
        return
    assert launch.resolvable is False, launch
    assert "acpx client entry" in launch.detail, launch.detail
    assert str(workspaces[-1]) in launch.detail, launch.detail


def test_the_child_never_searches_its_working_directory_for_a_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The npm shim runs a bare ``node``; cmd.exe and libuv would look in the workspace first.

    ``NoDefaultCurrentDirectoryInExePath`` turns that off for both, so it is always set in the
    child environment - whatever the ambient environment or ``extra_env`` says, in any spelling -
    and a relative PATH entry (resolved against the workspace) is not passed on.
    """
    _launcher_stand_in(tmp_path / "npm")
    launch = resolve_launch_config(
        data_dir=tmp_path,
        acpx_cli=ENV_REPORT_CLIENT,
        python_executable=sys.executable,
        env=_resolution_env(str(tmp_path / "npm")),
    )
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "bin", os.environ.get("PATH", "")]))
    driver = AcpxDshDriver(
        data_dir=tmp_path,
        launch=launch,
        extra_env={"nodefaultcurrentdirectoryinexepath": ""},
    )

    env = driver._child_env(tmp_path)

    spellings = [key for key in env if key.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH"]
    assert spellings == ["NoDefaultCurrentDirectoryInExePath"], spellings
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    path_keys = [key for key in env if key.upper() == "PATH"]
    assert len(path_keys) == 1, path_keys
    entries = env[path_keys[0]].split(os.pathsep)
    assert entries and all(Path(entry).is_absolute() for entry in entries), entries


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_a_workspace_client_config_refuses_the_launch_before_any_process(
    harness_factory, role: str, kind: str
) -> None:
    """acpx always loads ``<--cwd>/.acpxrc.json`` and lets it replace HFlow's agent argv.

    No CLI flag can override a project agent entry on Windows, so the only safe answer is to
    refuse: inside the spawn gate, for either role, whatever kind of entry has that name. No
    process is created, the spawn is reported as not created, and the refusal names the file.
    """
    harness = harness_factory("cooperative")
    planted = harness.workspace / ".acpxrc.json"
    if kind == "file":
        planted.write_text('{"agents": {"acpx-dsh-acp": {"command": "evil"}}}', encoding="utf-8")
    else:
        planted.mkdir()
    facts = []
    request = InvocationRequest(
        invocation_id="I-rc",
        attempt_id="A-1",
        run_id="R-1",
        role=role,
        task_id="T-1",
        task_revision=1,
        goal="do the thing",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(harness.workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        data_dir=str(harness.data_dir),
        on_spawn=facts.append,
    )

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(request)

    assert excinfo.value.code is RefusalCode.WORKSPACE_CLIENT_CONFIG
    assert ".acpxrc.json" in excinfo.value.message
    assert "remove the file" in excinfo.value.message.lower()
    # The step that works once a dispatch was reserved: an identical TaskSpec is a history
    # lookup that returns the blocked run, so only a new revision can run again.
    assert "new revision" in excinfo.value.message, excinfo.value.message
    assert "identical TaskSpec returns this blocked run" in excinfo.value.message
    assert [fact.created for fact in facts] == [False]
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == [], "no agent may be launched"
    assert not harness.spawn_log().exists(), "the client itself must not be launched"


@pytest.mark.parametrize("planted", [".ACPXRC.JSON", ".AcpxRc.json"])
def test_any_spelling_of_the_workspace_client_config_refuses_the_launch(
    harness_factory, planted: str
) -> None:
    """A case-insensitive filesystem opens ``.ACPXRC.JSON`` when acpx reads ``.acpxrc.json``.

    The spawn gate matches the workspace root's entries without regard to case, on any
    filesystem (refusing more is the safe side), and names the entry as it is spelled on disk.
    """
    harness = harness_factory("cooperative")
    (harness.workspace / planted).write_text("{}", encoding="utf-8")
    (harness.workspace / "src").mkdir(exist_ok=True)
    found = _workspace_client_config(harness.workspace)
    assert found is not None and found.name == planted, found
    facts = []
    request = InvocationRequest(
        invocation_id="I-rc-case",
        attempt_id="A-1",
        run_id="R-1",
        role="implementer",
        task_id="T-1",
        task_revision=1,
        goal="do the thing",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(harness.workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        data_dir=str(harness.data_dir),
        on_spawn=facts.append,
    )

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(request)

    assert excinfo.value.code is RefusalCode.WORKSPACE_CLIENT_CONFIG
    assert planted in excinfo.value.message, excinfo.value.message
    assert [fact.created for fact in facts] == [False]
    assert harness.stub_files("spawn") == [], "no agent may be launched"
    assert not harness.spawn_log().exists(), "the client itself must not be launched"


def test_a_nested_or_lookalike_client_config_does_not_count(tmp_path: Path) -> None:
    """acpx reads only the workspace root's entry; another directory or name is not it."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".ACPXRC.JSON").write_text("{}", encoding="utf-8")
    (tmp_path / ".acpxrc.json.bak").write_text("{}", encoding="utf-8")
    assert _workspace_client_config(tmp_path) is None
    assert _workspace_client_config(tmp_path / "missing") is None


def _plant_client_config_at_launch(driver: AcpxDshDriver, project_root: Path) -> None:
    """Make ``.acpxrc.json`` appear after admission, just before the driver's spawn gate.

    A file already in the starting workspace is refused by admission before anything is spent;
    this is the other case - the workspace changed after the run was admitted - which only the
    spawn gate can see.
    """
    original = driver.start_handle

    def start_handle(request):  # noqa: ANN001, ANN202 - the driver's own shape
        (project_root / ".acpxrc.json").write_text("{}", encoding="utf-8")
        return original(request)

    driver.start_handle = start_handle  # type: ignore[method-assign]


def test_a_workspace_client_config_at_submission_is_refused_before_anything_is_spent(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """Already in the workspace the run starts in: refused by the run gate, not at spawn.

    No run row, no reservation and no process, so "remove the file and submit again" is true:
    the identical TaskSpec dispatches once the file is gone.
    """
    harness = harness_factory("cooperative")
    (project_root / ".acpxrc.json").write_text("{}", encoding="utf-8")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=harness.data_dir,
        production=False,
    )
    request = RunRequest(
        task=task_spec, project=project, project_root=project_root, workspace_root=project_root
    )
    try:
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(request)
        assert excinfo.value.code is RefusalCode.WORKSPACE_CLIENT_CONFIG
        assert ".acpxrc.json" in excinfo.value.message
        assert "before anything was dispatched or charged" in excinfo.value.message
        assert store.find_run_by_spec_digest(project.project_id, task_spec.spec_digest()) is None
        assert harness.driver._processes == {}
        assert harness.stub_files("spawn") == []

        (project_root / ".acpxrc.json").unlink()
        outcome = controller.run_task(request)
    finally:
        store.close()

    assert outcome.block_code is not RefusalCode.WORKSPACE_CLIENT_CONFIG, outcome.block_reason
    assert not any("identical TaskSpec" in note for note in outcome.notes), outcome.notes
    assert harness.stub_files("spawn"), "the same TaskSpec dispatched once the file was gone"


def test_a_workspace_client_config_blocks_the_implementer_run(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """Through the controller: the run blocks with the new code and nothing was launched."""
    harness = harness_factory("cooperative")
    _plant_client_config_at_launch(harness.driver, project_root)
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG
    assert ".acpxrc.json" in (outcome.block_reason or "")
    assert "new revision" in (outcome.block_reason or ""), outcome.block_reason
    assert outcome.receipt is None
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


def test_a_workspace_client_config_blocks_the_reviewer_handoff(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory, fake_script
) -> None:
    """The reviewer runs on the worktree the implementer wrote, so it is checked there too.

    The implementer here is the offline fake (it does not read client config); the reviewer
    is the real driver. The run blocks with the workspace-config code - not as a review
    protocol error, because no review was attempted - and no reviewer process exists.
    """
    from hflow.drivers.fake import FakeDriver

    harness = harness_factory("cooperative")
    _plant_client_config_at_launch(harness.driver, project_root)
    store = Store(tmp_path / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        FakeDriver(project_root, fake_script),
        reviewer_driver=harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG, outcome.block_reason
    assert outcome.receipt is None
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


# --------------------------------------------------------------------------
# group 1: normal single execution
# --------------------------------------------------------------------------


def test_normal_execution_is_observed_and_settles(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()

    spawn_marker = harness.wait_for_stub_marker("spawn", non_empty=True)
    events = _collect_events(harness.driver, handle)
    kinds = [event.kind for event in events]

    assert EventKind.STARTED in kinds
    assert EventKind.DISPATCHED in kinds, "the session/prompt marker must be observed"
    assert EventKind.COMPLETED in kinds
    assert handle.dispatched is True
    assert handle.session_id and handle.session_id.startswith("sess-")

    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.agent_turns == 1
    assert result.provider_billed_tokens is None, "billing is unknown, never fabricated"

    # The task body travelled through stdin, not through a command line.
    spawn = json.loads(spawn_marker.read_text(encoding="utf-8"))
    assert spawn["mode"] == "cooperative"
    assert "do the thing" in json.loads(harness.spawn_log().read_text(encoding="utf-8").splitlines()[0])["task"]
    assert Path(handle.event_log).exists()
    assert harness.driver.unparsed_line_count(handle.invocation_id) == 0

    harness.driver.release(handle.invocation_id)
    assert process_gone(handle.pid, 3.0), "a finished invocation must leave no process"


def test_invocation_state_stays_out_of_the_workspace(tmp_path: Path, harness_factory) -> None:
    """Config, logs and session state belong to the driver's data dir, not the checkout."""
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    harness.driver.collect(handle)

    assert Path(handle.event_log).is_relative_to(harness.data_dir)
    workspace_entries = {path.name for path in harness.workspace.iterdir()}
    assert not any(name.endswith(".json") and "acpx" in name for name in workspace_entries)
    harness.driver.release(handle.invocation_id)


def test_probe_reports_cancel_as_unsupported_without_calling_a_model(harness_factory) -> None:
    harness = harness_factory("cooperative")
    report = harness.driver.probe(AgentBinding(harness="dsh", driver=DRIVER_ID))

    assert report.probe_only is True and report.live_tested is False
    assert report.capabilities["cancel"] is CapabilityState.UNSUPPORTED
    assert report.capabilities["process_boundary_teardown"] is CapabilityState.PROBED
    assert any("no prompt" in note for note in report.notes)
    assert harness.stub_files("spawn") == [], "probe must not launch the agent"


def test_controller_runs_the_driver_through_its_normal_contract(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """The same TaskSpec goes through admission, budget and the receipt path."""
    harness = harness_factory("cooperative")
    store = Store(tmp_path / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    # The stub client is harness scaffolding and writes its own log into the session cwd,
    # which the controller correctly flags as a change outside the TaskSpec's write scope.
    # When the run does get past that, the stub's reviewer answers in prose with no
    # structured verdict, which the controller correctly refuses as a review protocol
    # problem. Each refusal is fail-closed; what matters is that the driver was reached and
    # that no receipt was produced.
    assert outcome.block_code in {
        RefusalCode.SCOPE_VIOLATION,
        RefusalCode.VERIFICATION_FAILED,
        RefusalCode.REVIEW_PROTOCOL_ERROR,
    }
    assert outcome.receipt is None
    # The implementer really ran; the driver was reached, not replaced by a fake.
    assert len(harness.driver._handles) == 1


# --------------------------------------------------------------------------
# group 2: budget and duplicate dispatch
# --------------------------------------------------------------------------


def test_duplicate_start_of_the_same_invocation_is_refused(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, request = harness.start()
    harness.driver.collect(handle)

    with pytest.raises(Exception) as excinfo:
        harness.driver.start_handle(request)
    assert "already started" in str(excinfo.value)
    assert len(harness.stub_files("spawn")) == 1, "no second agent process may exist"
    harness.driver.release(handle.invocation_id)


def test_exhausted_budget_never_reaches_the_cli(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    harness = harness_factory("cooperative")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store, harness.driver, controller_build="test-build", data_dir=harness.data_dir, production=False
    )
    try:
        run = store.create_run(
            run_id="R-seeded",
            project_id=project.project_id,
            spec=task_spec,
            spec_digest=task_spec.spec_digest(),
            controller_build="test-build",
            checks_digest=project.checks_digest(),
            turn_limit=1,
            repair_limit=0,
            admission_binding=controller._admission_binding("R-seeded", RunRequest(
                task=task_spec, project=project, project_root=project_root, workspace_root=project_root,
            )),
        )
        # Claimed for this controller's owner identity (owner lease): a label-only claim is never
        # adopted by a controller.
        store.claim_run_owned(
            run["run_id"],
            "local-controller",
            token=controller.owner_token,
            identity=controller.owner_identity,
        )
        store.reserve_turn(run["run_id"], "local-controller", turns=1)
        store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)

        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
    finally:
        store.close()

    assert outcome.block_code is RefusalCode.BUDGET_EXHAUSTED
    assert harness.driver._handles == {}, "no invocation may be started without reserved budget"
    assert harness.stub_files("spawn") == [], "the real client path must not be reached"


def test_budget_ledger_survives_a_restart(tmp_path: Path, project, task_spec, project_root: Path) -> None:
    """A fresh process must read the existing ledger, not start from zero."""
    data_dir = tmp_path / "data"
    first = Store(tmp_path / "hflow.sqlite")
    run = first.create_run(
        run_id="R-persist",
        project_id=project.project_id,
        spec=task_spec,
        spec_digest=task_spec.spec_digest(),
        controller_build="test-build",
        checks_digest=project.checks_digest(),
        turn_limit=2,
        repair_limit=0,
    )
    first.claim_run(run["run_id"], "local-controller")
    first.reserve_turn(run["run_id"], "local-controller", turns=2)
    first.close()

    reopened = Store(tmp_path / "hflow.sqlite")
    try:
        assert reopened.turns_remaining("R-persist") == 0
        with pytest.raises(Exception):
            reopened.reserve_turn("R-persist", "local-controller", turns=1)
    finally:
        reopened.close()
    assert data_dir  # keep the fixture honest about where state lives


# --------------------------------------------------------------------------
# group 3: protocol and output failures
# --------------------------------------------------------------------------


def test_missing_stop_reason_is_unknown_not_success(harness_factory) -> None:
    harness = harness_factory("no-answer")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("spawn")
    # Let the turn start, then stop the process so collection has to judge an unsettled run.
    harness.driver._processes[handle.invocation_id].terminate()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize("payload", ['"boom"', "null", "[1]", "1", "true"])
def test_a_malformed_error_response_is_projected_not_raised(payload: str) -> None:
    """An ``error`` that is not an object is still an error line.

    The string, list, ``1`` and ``true`` payloads used to raise ``AttributeError`` out of the
    projection - and out of the driver's reader thread, which then never counted what followed.
    """
    observed = project_line('{"jsonrpc":"2.0","id":2,"error":%s}' % payload, 0, "t")

    assert observed.parsed is True
    assert observed.event is not None and observed.event.kind is EventKind.FAILED


def test_the_event_projection_and_the_result_share_acp_v1_stop_reasons() -> None:
    """One closed set decides which stop reasons are known, for the event and for ``collect``."""
    assert V1_STOP_REASONS == {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}

    def kind_of(reason: str) -> EventKind:
        observed = project_line(json.dumps({"id": 2, "result": {"stopReason": reason}}), 0, "t")
        assert observed.event is not None
        return observed.event.kind

    for reason in V1_STOP_REASONS:
        assert kind_of(reason) is not EventKind.OUTCOME_UNKNOWN, reason
    for reason in ("paused", "error"):
        assert kind_of(reason) is EventKind.OUTCOME_UNKNOWN, reason


# --------------------------------------------------------------------------
# group 3b: model selection - observed always, passed only from the bound profile
# --------------------------------------------------------------------------

#: The stand-in's grouped catalog (``STUB_MODEL_CATALOG=grouped``): DSH's shape, placeholder ids.
STAND_IN_INITIAL_MODEL = '["stub-provider","stub-flash"]'
STAND_IN_OTHER_MODEL = '["stub-provider","stub-pro"]'


def _model_binding(model_selection: str) -> AgentBinding:
    return AgentBinding(harness="dsh", driver=DRIVER_ID, model_selection=model_selection)


def test_client_argv_carries_a_bound_model_as_one_fixed_global_flag(tmp_path: Path) -> None:
    """``native_profile`` adds nothing; a chosen model is one ``--model`` before ``exec``.

    The value is the profile's validated value, verbatim - a JSON pair stays one argument, and
    the agent launch argv (rule 9's launcher plus fixed flags) does not change at all.
    """
    native = AcpxDshDriver(
        data_dir=tmp_path, acpx_cli=FAKE_CLIENT, binding=_model_binding("native_profile")
    )
    assert native.launch.model == ""
    assert "--model" not in native._client_argv(tmp_path, tmp_path, 60)

    chosen = AcpxDshDriver(
        data_dir=tmp_path, acpx_cli=FAKE_CLIENT, binding=_model_binding(STAND_IN_OTHER_MODEL)
    )
    argv = chosen._client_argv(tmp_path, tmp_path, 60)
    assert chosen.launch.model == STAND_IN_OTHER_MODEL
    assert argv.count("--model") == 1
    flag = argv.index("--model")
    assert argv[flag + 1] == STAND_IN_OTHER_MODEL
    assert flag < argv.index("exec"), "--model is a global flag: it goes before the subcommand"
    assert argv[-2:] == ["-f", "-"], "the task still travels on stdin"
    assert chosen._agent_argv() == native._agent_argv()


def test_the_stand_in_stream_with_a_grouped_catalog_yields_the_effective_model(
    harness_factory,
) -> None:
    """Observed without passing anything: what the session started with is recorded."""
    harness = harness_factory("cooperative")
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    observation = result.model_observation
    assert observation is not None
    assert observation.advertised is True
    assert observation.config_id == "model"
    assert observation.initial_value == STAND_IN_INITIAL_MODEL
    assert observation.effective_value == STAND_IN_INITIAL_MODEL
    assert observation.requested is None
    assert observation.source == "session/new"
    assert observation.thought_level == "high"
    assert observation.changes == []
    assert result.model_applied is ModelApplied.NOT_PASSED
    harness.driver.release(handle.invocation_id)


def test_a_stream_without_a_catalog_records_that_no_model_was_advertised(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.model_observation is not None
    assert result.model_observation.advertised is False
    assert result.model_observation.effective_value is None
    assert result.model_applied is ModelApplied.NOT_PASSED
    harness.driver.release(handle.invocation_id)


def test_an_advertised_model_is_applied_and_accepted_before_the_prompt(harness_factory) -> None:
    harness = harness_factory("cooperative", binding=_model_binding(STAND_IN_OTHER_MODEL))
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    observation = result.model_observation
    assert observation is not None
    assert observation.requested == STAND_IN_OTHER_MODEL
    assert observation.initial_value == STAND_IN_INITIAL_MODEL
    assert observation.effective_value == STAND_IN_OTHER_MODEL
    assert observation.source == "session/set_config_option"
    assert [change.value for change in observation.changes] == [STAND_IN_OTHER_MODEL]
    assert result.model_applied is ModelApplied.ACCEPTED
    harness.driver.release(handle.invocation_id)


def test_an_unadvertised_model_fails_before_any_prompt_is_sent(harness_factory) -> None:
    """The client refuses the model and exits: a definite failure, not an unknown outcome.

    No ``session/prompt`` left the client, the stream is complete and the process is gone, so
    nothing reached a model - which is what makes FAILED (and not OUTCOME_UNKNOWN) honest.
    """
    harness = harness_factory("cooperative", binding=_model_binding('["stub-provider","nope"]'))
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.FAILED
    assert result.error_code == "model_rejected_before_prompt"
    assert handle.dispatched is False
    assert result.agent_turns == 0
    assert result.model_applied is ModelApplied.REJECTED
    assert result.model_observation is not None
    assert result.model_observation.effective_value == STAND_IN_INITIAL_MODEL
    harness.driver.release(handle.invocation_id)


def test_a_model_with_no_catalog_to_choose_from_fails_before_the_prompt(harness_factory) -> None:
    harness = harness_factory("cooperative", binding=_model_binding("stub-pro"))
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.FAILED
    assert result.error_code == "model_rejected_before_prompt"
    assert result.model_observation is not None
    assert result.model_observation.advertised is False
    assert result.model_applied is ModelApplied.REJECTED
    harness.driver.release(handle.invocation_id)


@pytest.mark.parametrize(
    ("model", "applied"),
    [
        (None, ModelApplied.NOT_PASSED),
        (STAND_IN_OTHER_MODEL, ModelApplied.ACCEPTED),
        # Already the session's value: the client sends no change, and that is not a refusal.
        (STAND_IN_INITIAL_MODEL, ModelApplied.PASSED),
    ],
)
def test_a_client_error_before_the_prompt_that_is_not_a_model_rejection_stays_unknown(
    harness_factory, model: str | None, applied: ModelApplied
) -> None:
    """The narrow conjunction is narrow: any other missing stop reason keeps today's reading.

    Without a model on the command line, with one the agent accepted, or with one that was
    already current, a client that errors out before the prompt is not a model rejection - and
    is not reclassified (rule 5).
    """
    binding = _model_binding(model) if model else None
    harness = harness_factory("cooperative", binding=binding)
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    harness.driver.extra_env["STUB_CLIENT_FAIL_BEFORE_PROMPT"] = "1"
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    assert handle.dispatched is False
    assert result.model_applied is applied
    harness.driver.release(handle.invocation_id)


def _watched_session(requested: str, *, catalog: list[str] | None, current: str = "") -> object:
    """A ``_ModelWatch`` that has seen ``session/new`` answered, with or without a model option."""
    from hflow.drivers.acpx_dsh import _ModelWatch

    watch = _ModelWatch(requested)
    watch.observe({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
    result: dict[str, object] = {"sessionId": "s"}
    if catalog is not None:
        result["configOptions"] = [_model_option(current, catalog)]
    watch.observe({"jsonrpc": "2.0", "id": 1, "result": result})
    return watch


def _model_option(current: str, catalog: list[str]) -> dict[str, object]:
    return {
        "id": "model",
        "category": "model",
        "type": "select",
        "currentValue": current,
        "options": [{"value": value} for value in catalog],
    }


def test_an_already_current_model_with_no_later_change_is_passed() -> None:
    watch = _watched_session("a", catalog=["a", "b"], current="a")

    assert watch.refused() is False
    assert watch.applied(rejected=False) is ModelApplied.PASSED


def test_a_model_moved_away_after_the_session_started_is_not_passed() -> None:
    """Already current, so no change request went out - then the stream shows another value.

    "passed" means nothing in the stream contradicts the request. A ``config_option_update``
    moving the model away does, so the label must not claim the requested model ran.
    """
    watch = _watched_session("a", catalog=["a", "b"], current="a")
    watch.observe(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [_model_option("b", ["a", "b"])],
                },
            },
        }
    )

    assert watch.effective_value == "b"
    assert len(watch.changes) == 1
    assert watch.applied(rejected=False) is ModelApplied.UNKNOWN


@pytest.mark.parametrize(
    "catalog",
    [None, ["a", "b"]],
    ids=["no-model-option-advertised", "value-not-in-the-catalog"],
)
def test_a_refusal_shadowed_by_an_earlier_outcome_is_not_passed(catalog) -> None:
    """The stream shows the client could not offer the model, but ``collect`` settled the turn on
    an earlier branch (boundary, overflow, unparseable, unbound), so ``rejected`` is False.

    The refusal is still in the stream; "passed" would say the opposite. Not upgraded to
    ``rejected`` either: the branch that confirms a rejection did not run (rule 7).
    """
    watch = _watched_session("zzz", catalog=catalog, current="a")
    watch.observe(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "model unavailable"}}
    )

    assert watch.refused() is True
    assert watch.applied(rejected=False) is ModelApplied.UNKNOWN


def test_status_shows_each_invocations_model_and_old_runs_say_not_recorded(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """status/report read the model facts from the stored result; nothing is re-derived."""
    from hflow.controller import inspect_run
    from hflow.report import report_json, status_text

    harness = harness_factory("cooperative", binding=_model_binding(STAND_IN_OTHER_MODEL))
    harness.driver.extra_env["STUB_MODEL_CATALOG"] = "grouped"
    # A foreign session reports the old model after the genuine session's successful set.
    # The controller must store the genuine value and both report forms must preserve it.
    harness.driver.extra_env["STUB_OTHER_SESSION_MODEL"] = STAND_IN_INITIAL_MODEL
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        inspection = inspect_run(store, outcome.run_id)
        invocation_result = harness.driver._results[inspection.attempts[0].invocation_id]
        # An attempt row written before model observation existed: its stored result simply
        # has no model fields.
        with store.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET result_json = ? WHERE run_id = ?",
                (json.dumps({"invocation_id": "old", "outcome": "completed"}), outcome.run_id),
            )
        legacy = inspect_run(store, outcome.run_id)
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    implementer = inspection.attempts[0]
    assert implementer.model_applied is ModelApplied.ACCEPTED
    assert implementer.model_observation is not None
    assert implementer.model_observation.effective_value == STAND_IN_OTHER_MODEL
    assert any(note.startswith("model_other_session:") for note in invocation_result.limitations)
    rendered = status_text(inspection)
    assert "model_applied=accepted" in rendered
    assert STAND_IN_OTHER_MODEL in rendered
    assert report_json(inspection)["attempts"][0]["model_applied"] == "accepted"
    assert (
        report_json(inspection)["attempts"][0]["model_observation"]["effective_value"]
        == STAND_IN_OTHER_MODEL
    )

    assert legacy.attempts[0].model_observation is None
    assert legacy.attempts[0].model_applied is None
    assert "model         implementer not recorded" in status_text(legacy)


# --------------------------------------------------------------------------
# launch surfaces: what DSH reads on its own, recorded and never enforced
# --------------------------------------------------------------------------


def test_an_invocation_records_the_empty_per_invocation_dsh_home_it_launched_with(
    harness_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The empty per-invocation home holds no credential, so a launch needs DEEPSEEK_API_KEY in
    # its environment (no_credential_source, tests/test_credential_source.py): the conftest
    # stand-in is that variable here, and the record holds its name only.
    monkeypatch.delenv("DSH_HOME", raising=False)
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    surfaces = result.launch_surfaces
    assert surfaces is not None
    assert surfaces.dsh_home_kind == "per_invocation"
    home = Path(surfaces.dsh_home)
    assert home == (harness.data_dir / "invocations" / "I-1" / "home").resolve() / ".dsh"
    # The same home the client got: its acpx config sits beside the DSH home it names.
    assert (home.parent / ".acpx" / "config.json").is_file()
    assert surfaces.dsh_home_observed is True
    assert surfaces.dsh_home_files
    assert all(entry.present is False for entry in surfaces.dsh_home_files)
    assert surfaces.workspace == str(harness.workspace)
    assert surfaces.deepseek_api_key_inherited is True
    assert STAND_IN_API_KEY not in result.model_dump_json(), "a name, never a value"
    # The stand-in launch is an override argv, so no resolved dsh is started.
    assert surfaces.client.dsh_carrier == "not_applicable"
    harness.driver.release(handle.invocation_id)


def test_a_bound_home_and_its_env_are_recorded_and_do_not_change_the_launch(
    tmp_path: Path, harness_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A workspace .env now refuses a real launch (tests/test_launch_refusals.py); the home's
    # .env, outside the workspace, is still only recorded - by presence and size, never opened.
    bound = tmp_path / "bound-home"
    bound.mkdir()
    cordis = b"sandbox:\n  mode: read-only\n"
    (bound / "cordis.patch.yml").write_bytes(cordis)
    (bound / ".env").write_text(
        "DSH_SANDBOX_HINT=x\nDEEPSEEK_API_KEY=sk-sentinel-456", encoding="utf-8"
    )
    monkeypatch.setenv("DSH_HOME", str(bound))
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED, "nothing about the surfaces refuses"
    surfaces = result.launch_surfaces
    assert surfaces is not None and surfaces.dsh_home_kind == "bound"
    files = {entry.name: entry for entry in surfaces.dsh_home_files}
    assert files["cordis.patch.yml"].sha256 == "sha256:" + hashlib.sha256(cordis).hexdigest()
    assert files[".env"].present is True and files[".env"].sha256 == ""
    assert surfaces.workspace_env is not None and surfaces.workspace_env.present is False
    assert "sk-sentinel-456" not in result.model_dump_json()
    harness.driver.release(handle.invocation_id)


def test_an_observation_failure_never_changes_the_launch(
    harness_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("hflow.drivers.dsh_surfaces.observe_launch_surfaces", broken)
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.COMPLETED
    assert result.launch_surfaces is None
    notes = [
        note
        for note in result.limitations
        if note.startswith("launch surfaces not recorded: RuntimeError: boom")
    ]
    assert len(notes) == 1
    assert harness.spawn_log().exists(), "the client launched as it would have anyway"
    harness.driver.release(handle.invocation_id)


def test_status_shows_each_invocations_launch_surfaces_and_old_runs_say_not_recorded(
    tmp_path: Path,
    project,
    task_spec,
    project_root: Path,
    harness_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status/report read the launch-surface record from the stored result; nothing is re-observed."""
    from hflow.contracts import EffectiveConfig, LaunchConfig, RoleConfig
    from hflow.controller import inspect_run
    from hflow.report import report_json, status_text

    monkeypatch.delenv("DSH_HOME", raising=False)
    harness = harness_factory("cooperative")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": FakeCheckRunner()}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        inspection = inspect_run(store, outcome.run_id)
        with store.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET result_json = ? WHERE run_id = ?",
                (json.dumps({"invocation_id": "old", "outcome": "completed"}), outcome.run_id),
            )
        legacy = inspect_run(store, outcome.run_id)
        with store.transaction() as conn:
            conn.execute(
                "UPDATE attempts SET result_json = ? WHERE run_id = ?",
                (
                    json.dumps(
                        {"invocation_id": "x", "outcome": "completed", "launch_surfaces": {"bogus": 1}}
                    ),
                    outcome.run_id,
                ),
            )
        malformed = inspect_run(store, outcome.run_id)
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.release(invocation_id)
        store.close()

    surfaces = inspection.attempts[0].launch_surfaces
    assert surfaces is not None and surfaces.dsh_home_kind == "per_invocation"
    assert "dsh home      implementer per-invocation" in status_text(inspection)
    assert report_json(inspection)["attempts"][0]["launch_surfaces"]["dsh_home_kind"] == (
        "per_invocation"
    )

    assert legacy.attempts[0].launch_surfaces is None
    assert "launch        implementer surfaces not recorded" in status_text(legacy)
    assert malformed.attempts[0].launch_surfaces is None

    unbound = EffectiveConfig(
        source="command_line",
        roles=[
            RoleConfig(
                role="implementer",
                agent="a",
                harness="dsh",
                driver="acpx-dsh",
                driver_id=DRIVER_ID,
                launch=LaunchConfig(driver_id=DRIVER_ID, harness="dsh"),
            )
        ],
    )
    rendered = status_text(inspection.model_copy(update={"effective_config": unbound}))
    assert "dsh_home=unbound(per-invocation)" in rendered


def test_probe_names_the_dsh_home_a_child_uses(
    tmp_path: Path, harness_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DSH_HOME", raising=False)
    harness = harness_factory("cooperative")
    report = harness.driver.probe(AgentBinding(harness="dsh", driver=DRIVER_ID))

    home_notes = [note for note in report.notes if note.startswith("DSH home: per-invocation")]
    assert len(home_notes) == 1
    assert "<invocation-id>" in home_notes[0] and "not observed" in home_notes[0]
    assert any(
        note.startswith("client identity (read from files, nothing executed)")
        for note in report.notes
    )
    assert not [note for note in report.notes if ".credentials" in note]
    assert report.capabilities["cancel"] is CapabilityState.UNSUPPORTED
    assert report.capabilities["model_selection"] is CapabilityState.DOCUMENTED
    assert harness.stub_files("spawn") == [], "probe must not launch the agent"

    bound = tmp_path / "bound"
    bound.mkdir()
    cordis = b"approval: never\n"
    (bound / "cordis.patch.yml").write_bytes(cordis)
    monkeypatch.setenv("DSH_HOME", str(bound))
    bound_report = harness_factory("cooperative").driver.probe(
        AgentBinding(harness="dsh", driver=DRIVER_ID)
    )
    bound_notes = [note for note in bound_report.notes if note.startswith("DSH home: bound")]
    assert len(bound_notes) == 1
    assert "sha256:" + hashlib.sha256(cordis).hexdigest() in bound_notes[0]


def test_output_overflow_is_untrustworthy_not_success(harness_factory) -> None:
    """The retention budget is small for this case, so the overflow path is driven directly.

    The production budget is 32 MiB; generating that much output in a unit test would be slow and
    would prove nothing extra about the behaviour, which is what happens when the budget is spent.
    """
    harness = harness_factory("chatty", max_raw_log_bytes=256 * 1024)
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "output_limit_exceeded"
    harness.driver.release(handle.invocation_id)


def test_the_retention_budget_bounds_what_hflow_keeps(harness_factory) -> None:
    """The reported defect: a 256 KiB budget while 17 MiB was retained and reported as complete.

    What is bounded is what HFlow keeps, and the test says exactly that: the retained protocol log
    and the in-memory event container stay inside the budget, stderr is capped at its share, and
    the client's own file is reported as the measured size it is - HFlow does not truncate a file
    another process is writing, so it does not claim to bound it.
    """
    budget = 256 * 1024
    harness = harness_factory("chatty", max_raw_log_bytes=budget)
    handle, _ = harness.start()
    result = harness.driver.collect(handle)
    try:
        invocation = harness.data_dir / "invocations" / handle.invocation_id
        raw_bytes = (invocation / "stdout.ndjson").stat().st_size
        event_bytes = (invocation / "events.ndjson").stat().st_size
        stderr_bytes = (invocation / "stderr.txt").stat().st_size
        protocol_share = harness.driver.protocol_share_bytes
        peak = harness.driver._peak_raw_bytes[handle.invocation_id]

        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert result.error_code == "output_limit_exceeded"
        assert event_bytes <= protocol_share, (event_bytes, protocol_share)
        assert stderr_bytes <= harness.driver.stderr_share_bytes
        assert harness.driver._overflow[handle.invocation_id] is True
        assert harness.driver._events_capped[handle.invocation_id] is True, (
            "the in-memory event container must stop growing too"
        )
        assert len(harness.driver._events[handle.invocation_id]) <= harness.driver.max_event_records
        # The client's own file is measured, not bounded, and the measurement is reported.
        assert peak == raw_bytes, (peak, raw_bytes)
        assert raw_bytes > budget, (
            "this client really does write past the budget; that is why the claim is about "
            "what HFlow keeps, not about the file"
        )
        capture = harness.driver._stdout_captures[handle.invocation_id]
        assert capture.retained_bytes <= protocol_share
        assert capture.total_bytes == raw_bytes
        assert capture.truncated is True
    finally:
        harness.driver.release(handle.invocation_id)


def test_stderr_is_read_from_a_pipe_and_really_recorded(harness_factory) -> None:
    """The reported defect: the child wrote 2 MiB of stderr into a file nobody read.

    ``process.stderr`` was ``None`` because the child had been given a file handle, so the reader
    recorded "0 bytes, not truncated" for a stream that was never empty. stderr now travels on a
    pipe, so this asserts the bytes *and* the bound.
    """
    harness = harness_factory("stderr-flood", max_raw_log_bytes=128 * 1024)
    handle, _ = harness.start()
    result = harness.driver.collect(handle)
    try:
        capture = harness.driver._stderr_captures[handle.invocation_id]
        assert capture.total_bytes >= 2 * 1024 * 1024, capture.total_bytes
        assert capture.retained_bytes == harness.driver.stderr_share_bytes
        assert capture.truncated is True
        assert (harness.data_dir / "invocations" / handle.invocation_id / "stderr.txt").stat().st_size == (
            capture.retained_bytes
        )
        assert any("stderr retained" in note for note in result.limitations)
    finally:
        harness.driver.release(handle.invocation_id)


def test_a_last_line_that_crosses_the_budget_is_still_reported(harness_factory) -> None:
    """The other reported boundary error: only the *final* line crosses, and overflow was False.

    A cooperative client whose whole stream is a few bytes larger than the budget used to end as
    ``completed`` with ``overflow=False``. The budget is now detected per read, not one line late.
    """
    harness = harness_factory("cooperative", max_raw_log_bytes=1024)
    handle, _ = harness.start()
    result = harness.driver.collect(handle)
    try:
        assert harness.driver._overflow[handle.invocation_id] is True, (
            "the read that spends the budget must record it, not the read after it"
        )
        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert result.error_code == "output_limit_exceeded"
        invocation = harness.data_dir / "invocations" / handle.invocation_id
        assert (invocation / "events.ndjson").stat().st_size <= harness.driver.protocol_share_bytes
    finally:
        harness.driver.release(handle.invocation_id)
    assert result.error_code == "output_limit_exceeded"
    assert harness.driver._overflow[handle.invocation_id] is True
    harness.driver.release(handle.invocation_id)


def test_unparseable_output_is_counted_and_fails_closed(harness_factory, monkeypatch) -> None:
    """A corrupted stream must not be read as a clean completion."""
    harness = harness_factory("cooperative")
    handle, _ = harness.start()

    import hflow.drivers.acpx_dsh as module
    from hflow.drivers.acp_events import ObservedLine

    original = module.project_line
    calls = {"n": 0}

    def corrupting_project_line(line: str, sequence: int, at: str):
        calls["n"] += 1
        if calls["n"] == 3:
            return ObservedLine(False, None, None)  # simulate a mangled line
        return original(line, sequence, at)

    monkeypatch.setattr(module, "project_line", corrupting_project_line)
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "unparseable_output"
    harness.driver.release(handle.invocation_id)


def test_client_timeout_is_reported_as_unknown(tmp_path: Path) -> None:
    """The client exits 3 with a TIMEOUT error: no stop reason, so not a success."""
    harness = DriverHarness(tmp_path, "no-answer")
    harness.timeout = 2.0
    handle, _ = harness.start()
    result = harness.driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code in {"no_stop_reason", "completion_timeout"}
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# group 4: cancellation races
# --------------------------------------------------------------------------


def test_cancel_before_dispatch_reports_no_model_work(harness_factory) -> None:
    """Stopping before the harness received the task must not claim a turn happened."""
    harness = harness_factory("slow-ready")
    handle, _ = harness.start()

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "forced"
    assert receipt.local_process_stopped is True
    assert handle.dispatched is False
    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.CANCELLED
    assert result.agent_turns == 0, "no dispatch means no turn"
    assert any("never received the task" in note for note in result.limitations)
    harness.driver.release(handle.invocation_id)


def test_cancel_in_flight_stops_a_stubborn_process_tree(harness_factory) -> None:
    """The agent ignores cancellation and holds a helper child; the boundary must still win."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()

    helper_file = harness.wait_for_stub_marker("helper")
    helper_pid = int(helper_file.read_text(encoding="utf-8"))
    assert process_gone(helper_pid, 0.5) is False, "the helper must be alive before the stop"

    # Wait until the dispatch marker and the in-progress tool call are observed.
    deadline = time.time() + 20
    saw_tool_call = False
    for event in harness.driver.observe(handle):
        if event.kind is EventKind.PROGRESS and "tool_call" in event.message:
            saw_tool_call = True
            break
        if time.time() > deadline:
            break
    assert handle.dispatched is True
    assert saw_tool_call, "the in-progress marker must be observed before asking to stop"

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "forced", "no protocol cancel exists on this launch path"
    assert receipt.local_process_stopped is True
    assert process_gone(handle.pid, 5.0), "the client process must be gone"
    assert process_gone(helper_pid, 5.0), "the managed descendant must be gone too"

    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.CANCELLED
    assert "cooperative cancel is unavailable" in receipt.detail
    harness.driver.release(handle.invocation_id)


def test_cancel_after_completion_is_a_no_op(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    result = harness.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "none"
    assert "already exited" in receipt.detail
    assert len(harness.stub_files("spawn")) == 1, "no second agent run may be started"
    harness.driver.release(handle.invocation_id)


def test_cancel_is_idempotent_and_sends_no_second_prompt(harness_factory) -> None:
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")
    # The dispatch marker must be observed before stopping, so the prompt line is in the log.
    assert harness.wait_for_dispatch(handle), "the prompt was never dispatched"

    first = harness.driver.cancel_handle(handle)
    second = harness.driver.cancel_handle(handle)
    third = harness.driver.cancel_handle(handle)

    assert first == second == third
    assert len(harness.stub_files("spawn")) == 1
    prompts = [line for line in harness.driver.raw_lines(handle.invocation_id) if '"session/prompt"' in line]
    assert len(prompts) == 1, "cancelling must never submit another prompt"
    harness.driver.release(handle.invocation_id)


def test_controller_cancel_records_intent_and_blocks_late_acceptance(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory
) -> None:
    """A recorded cancellation intent must survive a late success and force BLOCKED."""
    harness = harness_factory("stubborn")
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store, harness.driver, controller_build="test-build", data_dir=harness.data_dir, production=False
    )
    try:
        run = store.create_run(
            run_id="R-cancel",
            project_id=project.project_id,
            spec=task_spec,
            spec_digest=task_spec.spec_digest(),
            controller_build="test-build",
            checks_digest=project.checks_digest(),
            turn_limit=4,
            repair_limit=0,
        )
        run_id = run["run_id"]
        store.claim_run(run_id, "local-controller")
        store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)

        attempt = store.dispatch_attempt(
            run_id=run_id,
            controller_id="local-controller",
            attempt_id="A-cancel",
            role="implementer",
            reservation_id="B-cancel",
            reserved_turns=1,
            reservation_expires_at="2999-01-01T00:00:00Z",
        )
        assert attempt["state"] == AttemptState.ACTIVE.value
        store.record_invocation("A-cancel", "I-cancel")
        handle, _ = harness.start("I-cancel", attempt_id="A-cancel")
        harness.wait_for_stub_marker("helper")

        receipt = controller.cancel(run_id)
        assert receipt.status == "confirmed_stopped"
        intent_at, recorded = store.cancel_state(run_id)
        assert intent_at and recorded is not None
        assert store.get_run(run_id)["task_state"] == TaskState.BLOCKED.value
        assert store.get_run(run_id)["block_code"] == RefusalCode.CANCELLED_BY_OPERATOR.value

        # A late success arriving after the stop must not become ACCEPTED: acceptance refuses
        # while the cancellation intent stands, and the run stays blocked.
        from hflow.contracts import CandidateSnapshot, ResultReceipt, ReviewResult, UsageFacts, VerificationResult
        from hflow.store import StoreError

        late_receipt = ResultReceipt(
            run_id=run_id,
            task_id=task_spec.task_id,
            attempt_id="A-cancel",
            task_revision=1,
            runtime_build="test-build",
            plan_digest=task_spec.spec_digest(),
            harness_outcome=InvocationOutcome.COMPLETED,
            candidate=CandidateSnapshot(base_commit="base", fingerprint="sha256:fp"),
            verification=VerificationResult(status="passed", evidence_ids=["E-late"]),
            review=ReviewResult(status="not_required"),
            task_state=TaskState.ACCEPTED,
            delivery_state=DeliveryState.LOCAL_CANDIDATE,
            usage=UsageFacts(),
        )
        with store.transaction() as conn:
            conn.execute(
                "UPDATE runs SET task_state = ?, phase = ? WHERE run_id = ?",
                (TaskState.CHECKING.value, "verification", run_id),
            )
        store.record_evidence(
            evidence_id="E-late",
            run_id=run_id,
            attempt_id="A-cancel",
            kind="verification",
            status=EvidenceStatus.PASSED,
            candidate_fingerprint="sha256:fp",
            checks_digest=project.checks_digest(),
            check_id="unit",
        )
        with pytest.raises(StoreError) as excinfo:
            store.finalize_acceptance(run_id, late_receipt, checks_digest=project.checks_digest())
        assert "cancellation intent" in str(excinfo.value)
        assert store.get_run(run_id)["task_state"] == TaskState.CHECKING.value, (
            "a refused acceptance must not change the recorded state"
        )

        # Cancelling again returns the stored fact and does not re-stop anything.
        again = controller.cancel(run_id)
        assert again == receipt
    finally:
        for invocation_id in list(harness.driver._handles):
            harness.driver.cancel_handle(harness.driver._handles[invocation_id])
            harness.driver.release(invocation_id)
        store.close()


# --------------------------------------------------------------------------
# group 5: process cleanup
# --------------------------------------------------------------------------


def test_release_leaves_no_boundary_and_no_child(harness_factory) -> None:
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    harness.driver.collect(handle)
    harness.driver.release(handle.invocation_id)

    assert harness.driver._boundaries[handle.invocation_id].handle is None
    assert process_gone(handle.pid, 3.0)


def test_unrelated_control_processes_are_untouched(harness_factory) -> None:
    """Stopping an invocation must not fan out to other Node/Python processes."""
    unrelated = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        harness = harness_factory("stubborn")
        handle, _ = harness.start()
        harness.wait_for_stub_marker("helper")
        harness.driver.cancel_handle(handle)
        time.sleep(0.5)
        assert process_gone(unrelated.pid, 0.5) is False, "an unrelated process must survive"
        harness.driver.release(handle.invocation_id)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


def _refuse_open_process(monkeypatch) -> None:
    """Make every pid-based ``OpenProcess`` answer ``ERROR_ACCESS_DENIED`` from now on.

    Installed after launch: the boundary keeps the kernel32 it opened with
    (``ProcessBoundary._api``), so only the pid-based ``process_gone`` sees the refusal. A
    boundary that called ``_kernel32()`` lazily would break this seam, not production.
    """
    monkeypatch.setattr(
        winjob, "_kernel32", lambda: kernel32_stand_in(open_error=ERROR_ACCESS_DENIED)[0]
    )


@requires_job_object
def test_a_stop_whose_client_cannot_be_opened_is_confirmed_by_its_own_handle(
    harness_factory, monkeypatch
) -> None:
    """Re-opening the pid is refused, but ``Popen``'s own handle saw the exit: that is observed."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    _refuse_open_process(monkeypatch)
    try:
        receipt = harness.driver.cancel_handle(handle)
    finally:
        monkeypatch.undo()

    assert receipt.status == "confirmed_stopped"
    assert receipt.mechanism == "forced"
    assert receipt.local_process_stopped is True
    assert "boundary_empty=True" in receipt.detail
    harness.driver.release(handle.invocation_id)


@requires_job_object
def test_a_stop_whose_client_exit_neither_handle_answers_is_not_confirmed(
    harness_factory, monkeypatch
) -> None:
    """The Job emptied, but neither ``Popen`` nor a re-opened pid shows the client exited."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    monkeypatch.setattr(harness.driver._processes[handle.invocation_id], "poll", lambda: None)
    _refuse_open_process(monkeypatch)
    try:
        receipt = harness.driver.cancel_handle(handle)
    finally:
        monkeypatch.undo()

    assert receipt.status == "unknown"
    assert receipt.mechanism == "none"
    # Not pinned to False: that is the pre-existing mapping for emptied-but-unconfirmed.
    assert receipt.local_process_stopped is not True
    assert "boundary_empty=True" in receipt.detail, (
        "the Job itself was emptied; only the client exit is unanswered"
    )
    harness.driver.release(handle.invocation_id)


def test_an_unanswered_exit_check_is_polled_to_its_bound_and_never_confirms(monkeypatch) -> None:
    calls: list[int] = []

    def unanswered(pid: int, wait_seconds: float = 0.0) -> None:
        calls.append(pid)
        return None

    monkeypatch.setattr("hflow.drivers.acpx_dsh.process_gone", unanswered)
    started = time.monotonic()
    assert AcpxDshDriver._await_process_gone(4242, 0.3) is False
    assert time.monotonic() - started >= 0.3
    assert len(calls) <= 20, "an immediate unanswered failure must not be retried in a tight loop"

    answers = iter([None, None, True])
    monkeypatch.setattr(
        "hflow.drivers.acpx_dsh.process_gone", lambda pid, wait_seconds=0.0: next(answers)
    )
    assert AcpxDshDriver._await_process_gone(4242, 2.0) is True, (
        "an unanswered check that later answers gone still confirms"
    )


def test_an_exit_seen_through_the_drivers_own_handle_counts_as_gone(monkeypatch) -> None:
    """``Popen.poll()`` reads the signal through a handle we hold; no pid re-open is needed."""
    monkeypatch.setattr("hflow.drivers.acpx_dsh.process_gone", lambda pid, wait_seconds=0.0: None)

    exited = types.SimpleNamespace(poll=lambda: 1)
    assert AcpxDshDriver._await_process_gone(4242, 2.0, process=exited) is True  # type: ignore[arg-type]
    running = types.SimpleNamespace(poll=lambda: None)
    assert AcpxDshDriver._await_process_gone(4242, 0.3, process=running) is False  # type: ignore[arg-type]


def test_unconfirmed_stop_blocks_instead_of_claiming_success(harness_factory, monkeypatch) -> None:
    """If the boundary cannot confirm the stop, the receipt must not say confirmed."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    boundary = harness.driver._boundaries[handle.invocation_id]
    monkeypatch.setattr(type(boundary), "terminate", lambda self, exit_code=1: False)
    monkeypatch.setattr(type(boundary), "wait_empty", lambda self, timeout_seconds: False)

    receipt = harness.driver.cancel_handle(handle)

    assert receipt.status in {"still_running", "unknown"}
    assert receipt.mechanism == "none"
    assert receipt.local_process_stopped is not True
    # The stub is still alive: clean it up directly so the fixture is honest.
    harness.driver._processes[handle.invocation_id].kill()
    time.sleep(0.5)


def test_release_is_idempotent_and_closes_the_output_file(harness_factory) -> None:
    """``release`` closes the parent's copy of ``stdout.ndjson``, and a second call is harmless.

    The client's stdout is a file the driver opened and handed to the child. The child got its
    own handle at creation; the parent's copy is only a leak, one per invocation, for as long as
    the controller lives.
    """
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    process = harness.driver._processes[handle.invocation_id]
    process.wait(timeout=60)

    harness.driver.release(handle.invocation_id)

    assert process._hflow_stdout.closed is True, "the parent's stdout file handle must be closed"
    assert harness.driver._boundaries[handle.invocation_id].handle is None
    harness.driver.release(handle.invocation_id)  # idempotent: nothing left to close, no error
    assert process._hflow_stdout.closed is True


@requires_job_object
def test_release_does_not_hang_on_a_stderr_pipe_whose_writer_survived(
    harness_factory, monkeypatch
) -> None:
    """A deadline expired and a holder of the client's stderr write end outlived the teardown.

    That is the case A4 records honestly as ``completion_timeout`` (a process the Job could not
    run down, or any holder outside it). The stderr reader is still blocked in ``read()`` and
    holds the stream's buffer lock, so closing the stream would wait until every writer exits -
    and the controller calls ``release`` right after applying that unknown outcome. The holder
    is stood in for by duplicating the client's ``hStdError`` into this (non-Job) process.
    """
    import _winapi
    import threading

    held: list[int] = []
    real_create = subprocess._winapi.CreateProcess

    def holding_create(executable, args, *rest):
        result = real_create(executable, args, *rest)
        startupinfo = rest[-1]
        if "fake_acpx_client" in str(args) and not held:
            me = _winapi.GetCurrentProcess()
            held.append(
                _winapi.DuplicateHandle(
                    me, startupinfo.hStdError, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS
                )
            )
        return result

    monkeypatch.setattr(subprocess._winapi, "CreateProcess", holding_create)
    harness = harness_factory("stubborn")
    harness.driver.completion_timeout_seconds = 3
    released = threading.Event()
    try:
        handle, _ = harness.start()
        result = harness.driver.collect(handle)
        assert held, "the client's stderr write end was not duplicated"
        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert result.error_code == "completion_timeout"
        process = harness.driver._processes[handle.invocation_id]
        reader = harness.driver._threads[f"{handle.invocation_id}:stderr"]
        assert reader.is_alive(), "the stand-in holder keeps the stderr reader blocked"

        def do_release() -> None:
            harness.driver.release(handle.invocation_id)
            released.set()

        started = time.monotonic()
        threading.Thread(target=do_release, daemon=True).start()
        assert released.wait(10.0), "release() blocked on the stderr pipe its reader still holds"
        assert time.monotonic() - started < 8.0
        assert process.stderr.closed is False, "a stream under a live reader is left to it"
        assert any("stderr" in note for note in harness.driver.release_notes(handle.invocation_id))
    finally:
        for duplicate in held:
            _winapi.CloseHandle(duplicate)
    # The last writer is gone: the reader sees EOF and closes the pipe itself.
    reader.join(timeout=10.0)
    assert not reader.is_alive()
    assert process.stderr.closed is True
    assert released.wait(10.0)


# --------------------------------------------------------------------------
# group 5b: what an exited client leaves behind in its boundary
# --------------------------------------------------------------------------

#: A descendant that outlives its client and keeps rewriting a workspace file, so "it is still
#: running" is visible in the tree under review and not only as a pid.
LINGERING_DESCENDANT = """\
import sys, time
from pathlib import Path
target = Path(sys.argv[1])
for tick in range(1200):
    try:
        target.write_text(str(tick), encoding="utf-8")
    except OSError:
        pass
    time.sleep(0.05)
"""

#: A client that settles its turn properly - ``session/prompt`` answered with ``end_turn``, exit
#: 0 - but leaves the descendant above running inside the managed boundary.
LINGERING_CLIENT = """\
import json, os, subprocess, sys, time
from pathlib import Path
sys.stdin.buffer.read()
target = Path(os.environ["PROBE_WRITE_TARGET"])
child = subprocess.Popen(
    [sys.executable, os.environ["PROBE_DESCENDANT"], str(target)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
Path(os.environ["PROBE_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
deadline = time.monotonic() + 20
while not target.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
print(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {}}), flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}), flush=True)
"""


class LingeringClient:
    """The production driver launching a client that exits and leaves a descendant behind."""

    def __init__(self, tmp_path: Path) -> None:
        client = tmp_path / "lingering_client.py"
        client.write_text(LINGERING_CLIENT, encoding="utf-8")
        descendant = tmp_path / "lingering_descendant.py"
        descendant.write_text(LINGERING_DESCENDANT, encoding="utf-8")
        self.data_dir = (tmp_path / "data").resolve()
        self.workspace = (tmp_path / "ws").resolve()
        (self.workspace / "src").mkdir(parents=True)
        self.target = self.workspace / "src" / "late.txt"
        self.pid_file = tmp_path / "descendant.pid"
        self.driver = AcpxDshDriver(
            data_dir=self.data_dir,
            acpx_cli=client,
            python_executable=sys.executable,
            completion_timeout_seconds=60,
            agent_argv_override=[sys.executable, "-c", "pass"],
        )
        self.driver.extra_env.update(
            PROBE_DESCENDANT=str(descendant),
            PROBE_WRITE_TARGET=str(self.target),
            PROBE_CHILD_PID=str(self.pid_file),
        )
        self.handle = None
        self.descendant_pid = 0

    def start(self):
        self.handle = self.driver.start_handle(
            InvocationRequest(
                invocation_id="I-linger",
                attempt_id="A-linger",
                run_id="R-linger",
                role="implementer",
                task_id="T-linger",
                task_revision=1,
                goal="leave a descendant behind",
                acceptance=[],
                write_allow=["src/late.txt"],
                write_deny=[],
                workspace=str(self.workspace),
                deadline_seconds=60,
                spec_digest="sha256:test",
                data_dir=str(self.data_dir),
            )
        )
        return self.handle

    @property
    def process(self) -> subprocess.Popen:
        return self.driver._processes[self.handle.invocation_id]

    @property
    def boundary(self) -> winjob.ProcessBoundary:
        return self.driver._boundaries[self.handle.invocation_id]

    def wait_for_client_exit(self) -> int:
        """Let the client finish on its own; return the pid of what it left running."""
        assert self.process.wait(timeout=30) == 0
        self.descendant_pid = int(self.pid_file.read_text(encoding="utf-8"))
        assert self.boundary.contains(self.descendant_pid) is True, (
            "the descendant must be inside the managed boundary, or this case tests nothing"
        )
        assert process_gone(self.descendant_pid, 0.2) is False, "the descendant must outlive its client"
        return self.descendant_pid

    def cleanup(self) -> None:
        if self.handle is not None:
            self.driver.release(self.handle.invocation_id)
        if self.descendant_pid and not process_gone(self.descendant_pid, 3.0):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.descendant_pid)],
                capture_output=True,
                text=True,
                timeout=30,
            )


def _terminated_after_exit(limitations: list[str]) -> int | None:
    """The count in the ``descendants_terminated_after_client_exit`` limitation, if present."""
    prefix = "descendants_terminated_after_client_exit: "
    for note in limitations:
        if note.startswith(prefix):
            return int(note[len(prefix) :].split()[0])
    return None


@requires_job_object
def test_collect_stops_what_an_exited_client_left_running_and_closes_what_it_opened(
    tmp_path: Path,
) -> None:
    """A completed turn whose client left a descendant: the turn stays COMPLETED, the tree does not.

    The descendant keeps rewriting a workspace file. Returning the result while it runs would let
    it change the tree the controller fingerprints, freezes, checks and reviews next.
    """
    lingering = LingeringClient(tmp_path)
    lingering.start()
    try:
        descendant = lingering.wait_for_client_exit()

        result = lingering.driver.collect(lingering.handle)

        assert result.outcome is InvocationOutcome.COMPLETED, (
            "the turn did settle with end_turn; the cleanup is a limitation, not a new outcome"
        )
        terminated = _terminated_after_exit(result.limitations)
        assert terminated is not None and terminated >= 1, result.limitations
        assert process_gone(descendant, 5.0), "the descendant must not outlive the collected result"
        assert lingering.boundary.handle is None, "the job is closed once its result is collected"
        assert lingering.process._hflow_stdout.closed is True, "the stdout file handle is closed"
        before = lingering.target.read_text(encoding="utf-8")
        time.sleep(0.5)
        assert lingering.target.read_text(encoding="utf-8") == before, (
            "the workspace is still being changed after the result was returned"
        )
    finally:
        lingering.cleanup()


@requires_job_object
def test_a_boundary_that_cannot_be_emptied_after_the_client_exits_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """end_turn plus a tree that cannot be stopped is not a completed invocation."""
    lingering = LingeringClient(tmp_path)
    lingering.start()
    try:
        lingering.wait_for_client_exit()
        boundary = lingering.boundary
        monkeypatch.setattr(type(boundary), "terminate", lambda self, exit_code=1: False)
        monkeypatch.setattr(type(boundary), "wait_empty", lambda self, timeout_seconds: False)

        result = lingering.driver.collect(lingering.handle)

        assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
        assert result.error_code == "boundary_not_empty"
        assert boundary.handle is None, (
            "the job is closed anyway: kill-on-close is the last thing left to do, and the "
            "result already says it was not confirmed"
        )
        assert lingering.process._hflow_stdout.closed is True
    finally:
        monkeypatch.undo()
        lingering.cleanup()


@requires_job_object
def test_cancel_after_the_client_exited_stops_the_descendant_it_left(tmp_path: Path) -> None:
    """The parent exiting is not the boundary being empty: the stop has to reach the tree."""
    lingering = LingeringClient(tmp_path)
    lingering.start()
    try:
        descendant = lingering.wait_for_client_exit()

        receipt = lingering.driver.cancel_handle(lingering.handle)

        assert receipt.status == "confirmed_stopped"
        assert receipt.mechanism == "forced", "a process was killed to confirm this stop"
        assert receipt.local_process_stopped is True
        assert process_gone(descendant, 5.0), "a confirmed stop must leave no descendant running"
        assert lingering.boundary.handle is None, "a confirmed stop closes the boundary"
        assert "already exited" in receipt.detail
    finally:
        lingering.cleanup()


@requires_job_object
def test_cancel_after_the_client_exited_does_not_confirm_a_tree_it_could_not_empty(
    tmp_path: Path, monkeypatch
) -> None:
    lingering = LingeringClient(tmp_path)
    lingering.start()
    try:
        lingering.wait_for_client_exit()
        boundary = lingering.boundary
        monkeypatch.setattr(type(boundary), "terminate", lambda self, exit_code=1: False)
        monkeypatch.setattr(type(boundary), "wait_empty", lambda self, timeout_seconds: False)

        receipt = lingering.driver.cancel_handle(lingering.handle)

        assert receipt.status == "still_running"
        assert receipt.mechanism == "none"
        assert receipt.local_process_stopped is False
        assert boundary.handle is not None, "an unconfirmed boundary stays open to be observed"
    finally:
        monkeypatch.undo()
        lingering.cleanup()


# --------------------------------------------------------------------------
# group 6: reconcile is a conservative query
# --------------------------------------------------------------------------


def test_reconcile_reports_still_running_without_starting_work(harness_factory) -> None:
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value == "still_running"
    assert result.local_process_alive is True
    assert result.protocol_cancel_supported is False
    assert len(harness.stub_files("spawn")) == 1, "reconcile must not launch anything"
    # Only assert on the prompt count once the dispatch marker was actually observed.
    if harness.wait_for_dispatch(handle):
        prompts = [line for line in harness.driver.raw_lines(handle.invocation_id) if '"session/prompt"' in line]
        assert len(prompts) == 1, "reconcile must never send a prompt"
    harness.driver.cancel_handle(handle)
    harness.driver.release(handle.invocation_id)


def test_reconcile_unknown_after_process_disappears(harness_factory) -> None:
    """A gone process does not imply success; without a recorded result it stays unknown."""
    harness = harness_factory("no-answer")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("spawn")
    harness.driver._processes[handle.invocation_id].kill()
    time.sleep(0.5)

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value == "unknown"
    assert result.local_process_alive is False
    harness.driver.release(handle.invocation_id)


@requires_job_object
def test_reconcile_does_not_read_an_unopenable_process_as_gone(harness_factory, monkeypatch) -> None:
    """A process this user may not open is not a process that exited."""
    harness = harness_factory("stubborn")
    handle, _ = harness.start()
    harness.wait_for_stub_marker("helper")

    _refuse_open_process(monkeypatch)
    try:
        result = harness.driver.reconcile_handle(handle)
    finally:
        monkeypatch.undo()

    assert result.outcome.value == "still_running"
    assert result.local_process_alive is True
    harness.driver.cancel_handle(handle)
    harness.driver.release(handle.invocation_id)


def test_reconcile_without_a_handle_is_not_started(harness_factory) -> None:
    harness = harness_factory("cooperative")
    result = harness.driver.reconcile("I-never-started")

    assert result.outcome.value == "not_started"
    assert harness.stub_files("spawn") == []


def test_reconcile_after_completion_reports_unprocessed_result(harness_factory) -> None:
    """Reconcile reports a recorded result, and never invents one for a vanished process.

    Both outcomes are honest here and the test accepts either: the invocation may have
    exited through the normal path (``finished_result_unprocessed``) or hit the terminal
    check in ``observe`` before any result was folded (``unknown``). What must never happen
    is a claim of success built from "the process is gone".
    """
    harness = harness_factory("cooperative")
    handle, _ = harness.start()
    collected = harness.driver.collect(handle)
    assert collected.outcome is InvocationOutcome.COMPLETED

    result = harness.driver.reconcile_handle(handle)

    assert result.outcome.value in {"finished_result_unprocessed", "unknown"}
    assert result.local_process_alive is False
    assert result.protocol_cancel_supported is False
    harness.driver.release(handle.invocation_id)


# --------------------------------------------------------------------------
# group 7: feeding the prompt to a client that does not read it
# --------------------------------------------------------------------------

#: Exits with an argument-parse style error before reading stdin (acpx does this on a bad flag).
EXIT_BEFORE_STDIN_CLIENT = """\
import sys
sys.stderr.write("config error: stand-in for an argument parse failure\\n")
sys.stderr.flush()
sys.exit(1)
"""

#: Hangs before it ever reads stdin - the case the invocation deadline exists for.
SLEEP_BEFORE_STDIN_CLIENT = """\
import time
time.sleep(60)
"""

#: Fills more than a pipe's worth of stderr before reading stdin, then settles its turn.
STDERR_FLOOD_CLIENT = """\
import json, sys
sys.stderr.write("e" * 300_000)
sys.stderr.flush()
data = sys.stdin.buffer.read()
sys.stderr.write("\\nread %d prompt bytes\\n" % len(data))
sys.stderr.flush()
prompt = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": "s-1"}}
print(json.dumps(prompt), flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}), flush=True)
"""

#: Closes its stdin without reading it, and still settles a turn with end_turn. (A client that
#: reads part of the prompt and then closes stdin is not detectable: the pipe accepts a write
#: before the reader takes it, so the writer cannot see what was discarded unread.)
UNREAD_STDIN_CLIENT = """\
import json, os, sys, time
os.close(0)
time.sleep(0.5)
prompt = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": "s-1"}}
print(json.dumps(prompt), flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}), flush=True)
"""

#: Far past the Windows anonymous pipe buffer (about 4 KiB) and past the 64 KiB a pipe was seen
#: to accept ahead of its reader here.
LARGE_GOAL = "x" * 300_000


def _stdin_driver(tmp_path: Path, source: str) -> AcpxDshDriver:
    client = tmp_path / "stdin_client.py"
    client.write_text(source, encoding="utf-8")
    (tmp_path / "ws").mkdir(exist_ok=True)
    return AcpxDshDriver(
        data_dir=(tmp_path / "data").resolve(),
        acpx_cli=client,
        python_executable=sys.executable,
        completion_timeout_seconds=90,
        agent_argv_override=[sys.executable, "-c", "pass"],
    )


def _stdin_request(
    tmp_path: Path, *, deadline_seconds: int = 60, goal: str = LARGE_GOAL
) -> InvocationRequest:
    return InvocationRequest(
        invocation_id="I-stdin",
        attempt_id="A-stdin",
        run_id="R-stdin",
        role="implementer",
        task_id="T-stdin",
        task_revision=1,
        goal=goal,
        acceptance=[],
        write_allow=["src/x.py"],
        write_deny=[],
        workspace=str((tmp_path / "ws").resolve()),
        deadline_seconds=deadline_seconds,
        spec_digest="sha256:test",
        writes_allowed=False,
        data_dir=str((tmp_path / "data").resolve()),
    )


def _start_bounded(driver: AcpxDshDriver, request: InvocationRequest, seconds: float = 10.0):
    """``start_handle`` on a helper thread, so a regression fails here instead of hanging."""
    import threading

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["handle"] = driver.start_handle(request)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "start_handle blocked writing the prompt"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["handle"], time.monotonic() - started


def _assert_released(driver: AcpxDshDriver, invocation_id: str) -> None:
    driver.release(invocation_id)
    process = driver._processes[invocation_id]
    assert process.poll() is not None
    assert driver._boundaries[invocation_id].handle is None, "the boundary was not closed"
    assert process.stdin.closed
    assert driver.release_notes(invocation_id) == []


def test_a_client_that_exits_before_reading_a_large_prompt_is_collected(tmp_path: Path) -> None:
    """``close`` flushes what the buffer still holds and raised ``EINVAL`` into ``start_handle``
    after the child was published: no handle, no stderr, a boundary nobody released."""
    driver = _stdin_driver(tmp_path, EXIT_BEFORE_STDIN_CLIENT)

    handle, elapsed = _start_bounded(driver, _stdin_request(tmp_path))
    result = driver.collect(handle)

    assert elapsed < 5.0
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "no_stop_reason"
    assert "exited 1" in (result.error_message or "")
    assert any(note.startswith("prompt_write_incomplete:") for note in result.limitations)
    stderr = (driver.data_dir / "invocations" / "I-stdin" / "stderr.txt").read_text(encoding="utf-8")
    assert "config error: stand-in for an argument parse failure" in stderr
    _assert_released(driver, handle.invocation_id)


def test_the_deadline_applies_to_a_client_that_never_reads_its_prompt(tmp_path: Path) -> None:
    """The prompt write used to block ``start_handle`` until the client exited, so the deadline -
    applied only in ``collect`` - was never reached for the one client it exists for."""
    driver = _stdin_driver(tmp_path, SLEEP_BEFORE_STDIN_CLIENT)

    started = time.monotonic()
    handle, elapsed = _start_bounded(driver, _stdin_request(tmp_path, deadline_seconds=2))
    result = driver.collect(handle)
    total = time.monotonic() - started

    assert elapsed < 2.0, "start_handle returned only after the write"
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "completion_timeout"
    assert any(note.startswith("prompt_write_incomplete:") for note in result.limitations)
    # The 2 s deadline, then the boundary teardown and the short writer join - not the client's 60 s.
    assert 1.5 <= total < 20.0, total
    _assert_released(driver, handle.invocation_id)


def test_a_client_that_floods_stderr_before_reading_stdin_does_not_deadlock(tmp_path: Path) -> None:
    """stderr is drained from the spawn on, so the client gets past its stderr and reads the
    whole prompt; the turn settles normally."""
    driver = _stdin_driver(tmp_path, STDERR_FLOOD_CLIENT)

    handle, elapsed = _start_bounded(driver, _stdin_request(tmp_path, deadline_seconds=30))
    result = driver.collect(handle)

    assert elapsed < 5.0
    assert result.outcome is InvocationOutcome.COMPLETED, result.error_message
    assert not any("prompt_write_incomplete" in note for note in result.limitations)
    prompt_size = len((driver.data_dir / "invocations" / "I-stdin" / "task.txt").read_bytes())
    stderr = (driver.data_dir / "invocations" / "I-stdin" / "stderr.txt").read_bytes()
    assert f"read {prompt_size} prompt bytes".encode() in stderr
    _assert_released(driver, handle.invocation_id)


def test_a_turn_settled_without_the_whole_prompt_is_never_completed(tmp_path: Path) -> None:
    """The reported prompt digest is of the prompt HFlow meant to send; a client that did not
    receive it settled some other task, whatever its stop reason says."""
    driver = _stdin_driver(tmp_path, UNREAD_STDIN_CLIENT)

    # Well past what the pipe accepts ahead of its reader, so some write must fail.
    handle, _elapsed = _start_bounded(driver, _stdin_request(tmp_path, goal="x" * 600_000))
    result = driver.collect(handle)

    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "prompt_write_incomplete"
    assert any(note.startswith("prompt_write_incomplete:") for note in result.limitations)
    _assert_released(driver, handle.invocation_id)


def test_a_failure_after_the_child_is_published_tears_down_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that fails once the process exists is recorded and released, never raised past a
    published handle (the controller's generic error path releases nothing)."""
    driver = _stdin_driver(tmp_path, SLEEP_BEFORE_STDIN_CLIENT)

    def broken_start(*_args, **_kwargs) -> None:
        raise RuntimeError("stand-in for a thread that could not start")

    monkeypatch.setattr(driver, "_start_io_threads", broken_start)
    handle, _elapsed = _start_bounded(driver, _stdin_request(tmp_path), seconds=30.0)
    process = driver._processes[handle.invocation_id]

    assert process.poll() is not None, "the published child was torn down"
    assert driver._boundaries[handle.invocation_id].handle is None
    result = driver.collect(handle)
    assert result.outcome is InvocationOutcome.OUTCOME_UNKNOWN
    assert result.error_code == "reader_failed"
    assert "RuntimeError" in (result.error_message or "")
    _assert_released(driver, handle.invocation_id)
