"""Launch-time refusals on a real driver: a workspace ``.env`` and a ``DSH_HOME`` in the workspace.

User rulings 2026-10-03. DSH loads ``<cwd>/.env`` at launch and its home's patch layers,
``AGENTS.md``, skills and ``.env`` (documented upstream, not observed). On a real driver HFlow
refuses a workspace whose root holds a ``.env`` (listed only, never opened) and a bound
``DSH_HOME`` that is relative, unresolvable, inside the workspace / the user's checkout / the
worktree directory, or around one of them - at admission (``prepare`` and the run gate) when it is
already knowable, and again at the driver's spawn gate before any process exists. The offline fake
driver is unaffected, and so is the per-invocation home HFlow creates when DSH_HOME is unbound.

Everything here is offline: the "real" driver runs a test-only client and stub agent.
"""

from __future__ import annotations

import builtins
import io
import os
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    InvocationRequest,
    RefusalCode,
    RefusedError,
    RunRequest,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import (
    _workspace_env_file,
    dsh_home_workspace_problem,
    spawn_workspaces,
)
from hflow.drivers.fake import FakeDriver
from hflow.prepare import build_prepare_report, start_workspace_env_file
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner
from tests.test_driver_acpx_dsh import DriverHarness, harness_factory  # noqa: F401 - fixture
from tests.test_prepare import _git, _resolve_live

ENV_CODE = RefusalCode.WORKSPACE_ENV_FILE.value
HOME_CODE = RefusalCode.DSH_HOME_IN_WORKSPACE.value


def codes(resolved) -> list[str]:
    return [issue.code.value for issue in resolved.dispatch_preconditions]


def issue_for(resolved, code: str):
    return next(issue for issue in resolved.dispatch_preconditions if issue.code.value == code)


def request_for(harness: DriverHarness, workspace: Path, *, role: str, facts: list, iid: str):
    return InvocationRequest(
        invocation_id=iid,
        attempt_id="A-1",
        run_id="R-1",
        role=role,
        task_id="T-1",
        task_revision=1,
        goal="do the thing",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace=str(workspace),
        deadline_seconds=60,
        spec_digest="sha256:test",
        data_dir=str(harness.data_dir),
        on_spawn=facts.append,
    )


def assert_nothing_launched(harness: DriverHarness, facts: list) -> None:
    assert [fact.created for fact in facts] == [False]
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == [], "no agent may be launched"
    assert not harness.spawn_log().exists(), "the client itself must not be launched"


@pytest.fixture()
def never_open_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every attempt to open a file named ``.env`` (any case); the list must stay empty.

    Recorded rather than raised: launch-surface observation swallows its own errors (a record
    never stops a launch or a preview), so a raise there would hide the very open it caught.
    """
    opened: list[str] = []
    original_open = io.open
    original_os_open = os.open

    def is_env(file) -> bool:  # noqa: ANN001 - whatever open() accepts
        try:
            return os.path.basename(os.fspath(file)).casefold() == ".env"
        except TypeError:
            return False  # a file descriptor

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_env(file):
            opened.append(os.fspath(file))
        return original_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_env(path):
            opened.append(os.fspath(path))
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    return opened


# --------------------------------------------------------------------------
# workspace .env: admission
# --------------------------------------------------------------------------


def test_an_in_place_env_is_refused_at_admission_on_a_real_driver(
    tmp_path: Path, live_project, task_spec, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, never_open_env: list[str],
) -> None:
    """The project root is the in-place workspace: a root ``.env`` refuses before anything."""
    monkeypatch.delenv("DSH_HOME", raising=False)
    (project_root / ".env").write_text("DEEPSEEK_API_KEY=sk-sentinel-h4\n", encoding="utf-8")
    never_open_env.clear()  # planting it was the test's own open

    resolved = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)

    assert ENV_CODE in codes(resolved), resolved.dispatch_preconditions
    issue = issue_for(resolved, ENV_CODE)
    assert issue.location == "workspace"
    assert ".env" in issue.detail and "never opens it" in issue.detail, issue.detail
    assert "before anything was dispatched or charged" in issue.detail
    assert resolved.ready_to_dispatch is False
    report = build_prepare_report(resolved)
    assert "sk-sentinel-h4" not in report.model_dump_json()
    assert never_open_env == [], "the .env must never be opened"

    (project_root / ".env").unlink()
    cleared = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)
    assert ENV_CODE not in codes(cleared), cleared.dispatch_preconditions


def test_an_in_place_env_does_not_refuse_the_offline_fake_driver(
    tmp_path: Path, project, task_spec, project_root: Path, profile, controller, run_request,
) -> None:
    """The fake driver reaches no DSH, so nothing reads the file: neither prepare nor run refuses."""
    (project_root / ".env").write_text("A=1\n", encoding="utf-8")

    resolved = _resolve_live(tmp_path, project, task_spec, project_root, profile)
    assert ENV_CODE not in codes(resolved), resolved.dispatch_preconditions
    assert HOME_CODE not in codes(resolved)

    outcome = controller.run_task(run_request)
    assert outcome.block_code is not RefusalCode.WORKSPACE_ENV_FILE, outcome.block_reason


def test_an_in_place_env_is_refused_by_the_run_gate_before_anything_is_spent(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory,  # noqa: F811
    never_open_env: list[str],
) -> None:
    """Through the controller: no run row, no reservation, no process - and resubmitting works."""
    harness = harness_factory("cooperative")
    (project_root / ".env").write_text("A=1\n", encoding="utf-8")
    never_open_env.clear()  # planting it was the test's own open
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
        assert excinfo.value.code is RefusalCode.WORKSPACE_ENV_FILE
        assert "before anything was dispatched or charged" in excinfo.value.message
        assert store.find_run_by_spec_digest(project.project_id, task_spec.spec_digest()) is None
        assert harness.driver._processes == {}
        assert harness.stub_files("spawn") == []
        assert never_open_env == []

        (project_root / ".env").unlink()
        outcome = controller.run_task(request)
    finally:
        store.close()
    assert outcome.block_code is not RefusalCode.WORKSPACE_ENV_FILE, outcome.block_reason
    assert harness.stub_files("spawn"), "the same TaskSpec dispatched once the file was gone"


@pytest.mark.parametrize("committed", [".ENV", ".Env", ".env"])
def test_a_worktree_base_commit_with_any_spelling_of_env_is_refused(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, committed: str, never_open_env: list[str],
) -> None:
    """A worktree checks out what the base commit tracks: its root ``.env`` refuses, any case.

    A nested ``.env`` is not DSH's workspace file, and an untracked copy in the user's checkout
    is not in the worktree, so neither refuses once the root entry's removal is committed.
    """
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    _git(project_root, "init", "-q", "-b", "main")
    (project_root / committed).write_text("A=1\n", encoding="utf-8")
    (project_root / "src" / ".env").write_text("B=2\n", encoding="utf-8")
    _git(project_root, "add", "-f", ".")
    _git(project_root, "commit", "-q", "-m", "base with a root env file")
    never_open_env.clear()  # planting it was the test's own open
    task = worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )

    resolved = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert ENV_CODE in codes(resolved), resolved.dispatch_preconditions
    detail = issue_for(resolved, ENV_CODE).detail
    assert committed in detail and "commit its removal" in detail, detail
    assert resolved.ready_to_dispatch is False

    _git(project_root, "rm", "-q", "--cached", committed)
    _git(project_root, "commit", "-q", "-m", "drop the root env file")
    assert (project_root / committed).exists(), "the user's copy stays; it is not in the worktree"
    removed = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert ENV_CODE not in codes(removed), removed.dispatch_preconditions
    assert start_workspace_env_file(task, project_root, "main") == ""
    assert never_open_env == []


def test_only_a_root_entry_named_env_counts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("A=\n", encoding="utf-8")
    (tmp_path / "x.env").write_text("A=\n", encoding="utf-8")
    assert _workspace_env_file(tmp_path) is None
    assert _workspace_env_file(tmp_path / "missing") is None
    (tmp_path / ".ENV").mkdir()
    found = _workspace_env_file(tmp_path)
    assert found is not None and found.name == ".ENV", "a directory counts, spelled as on disk"


# --------------------------------------------------------------------------
# workspace .env: spawn gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
@pytest.mark.parametrize("planted", [".env", ".ENV"])
def test_an_env_in_the_launch_workspace_refuses_before_any_process(
    harness_factory, role: str, planted: str, never_open_env: list[str],  # noqa: F811
) -> None:
    harness = harness_factory("cooperative")
    (harness.workspace / planted).write_text("DSH_HOME=x\n", encoding="utf-8")
    never_open_env.clear()  # planting it was the test's own open
    facts: list = []

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(
            request_for(harness, harness.workspace, role=role, facts=facts, iid="I-env")
        )

    assert excinfo.value.code is RefusalCode.WORKSPACE_ENV_FILE
    assert planted in excinfo.value.message
    assert "new revision" in excinfo.value.message
    assert "identical TaskSpec returns this blocked run" in excinfo.value.message
    assert_nothing_launched(harness, facts)
    assert never_open_env == [], "listed and lstat'ed only, never opened"


def test_an_env_in_a_candidate_worktree_refuses_the_reviewer(
    tmp_path: Path, harness_factory,  # noqa: F811
) -> None:
    """The reviewer runs in ``<checkout>.hflow-worktrees/<run>``; a ``.env`` there refuses it."""
    harness = harness_factory("cooperative")
    worktree = tmp_path / "repo.hflow-worktrees" / "R-1"
    worktree.mkdir(parents=True)
    (worktree / ".env").write_text("A=1\n", encoding="utf-8")
    facts: list = []

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(
            request_for(harness, worktree, role="reviewer", facts=facts, iid="I-rv")
        )

    assert excinfo.value.code is RefusalCode.WORKSPACE_ENV_FILE
    assert [fact.created for fact in facts] == [False]
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


def test_an_env_appearing_after_admission_blocks_the_reviewer_handoff(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory,  # noqa: F811
    fake_script,
) -> None:
    """Admitted clean; the file appears before the reviewer's spawn. The run blocks with the
    env code (not as a review protocol error: no review was attempted) and nothing launched."""
    harness = harness_factory("cooperative")
    original = harness.driver.start_handle

    def start_handle(request):  # noqa: ANN001, ANN202 - the driver's own shape
        (project_root / ".env").write_text("A=1\n", encoding="utf-8")
        return original(request)

    harness.driver.start_handle = start_handle  # type: ignore[method-assign]
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
    assert outcome.block_code is RefusalCode.WORKSPACE_ENV_FILE, outcome.block_reason
    assert outcome.receipt is None
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


# --------------------------------------------------------------------------
# DSH_HOME: the rule
# --------------------------------------------------------------------------


def test_the_dsh_home_rule(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    worktrees = tmp_path / "workspace.hflow-worktrees"
    roots = [project, worktrees]

    assert dsh_home_workspace_problem("", roots) == "", "unbound: the per-invocation home"
    assert dsh_home_workspace_problem(str(tmp_path / "home"), roots) == ""
    assert dsh_home_workspace_problem(str(tmp_path / "workspace-home"), roots) == "", (
        "a sibling sharing the name prefix is outside"
    )
    for inside in (project, project / ".dsh", worktrees / "R-1" / ".dsh"):
        assert "is, or lies inside" in dsh_home_workspace_problem(str(inside), roots), inside
    swapped = str(project / ".dsh").swapcase()
    if os.path.normcase("A") == "a":
        assert "is, or lies inside" in dsh_home_workspace_problem(swapped, roots)
    assert "lies inside DSH_HOME" in dsh_home_workspace_problem(str(tmp_path), roots)
    for relative in (".dsh", "home/.dsh", "~/.dsh", "~"):
        assert "not an absolute path" in dsh_home_workspace_problem(relative, roots), relative
    assert "could not be resolved" in dsh_home_workspace_problem(
        str(tmp_path / "bad\0home"), roots
    )


def test_a_dsh_home_that_cannot_be_resolved_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise OSError("resolution failed")

    monkeypatch.setattr(os.path, "realpath", failing)
    problem = dsh_home_workspace_problem(str(tmp_path / "home"), [tmp_path / "workspace"])
    assert "could not be resolved" in problem and "resolution failed" in problem, problem


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_a_junction_cannot_disguise_a_dsh_home(tmp_path: Path) -> None:
    """Links resolved and as written are both compared; either one inside refuses."""
    import _winapi

    project = tmp_path / "workspace"
    (project / ".dsh").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    into_project = tmp_path / "looks-outside"
    _winapi.CreateJunction(str(project / ".dsh"), str(into_project))
    out_of_project = project / "looks-inside"
    _winapi.CreateJunction(str(outside), str(out_of_project))

    assert "is, or lies inside" in dsh_home_workspace_problem(str(into_project), [project])
    assert "is, or lies inside" in dsh_home_workspace_problem(str(out_of_project), [project])


def test_spawn_workspaces_name_the_checkout_of_a_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "repo.hflow-worktrees" / "R-1"
    assert spawn_workspaces(worktree) == [
        worktree,
        tmp_path / "repo.hflow-worktrees",
        tmp_path / "repo",
    ]
    assert spawn_workspaces(tmp_path / "repo") == [tmp_path / "repo"]


# --------------------------------------------------------------------------
# DSH_HOME: prepare / admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["root", "inside_root", "around_root"])
def test_prepare_refuses_a_dsh_home_in_or_around_the_project_root(
    tmp_path: Path, live_project, task_spec, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch, where: str,
) -> None:
    home = {
        "root": project_root,
        "inside_root": project_root / ".dsh",
        "around_root": project_root.parent,
    }[where]
    monkeypatch.setenv("DSH_HOME", str(home))

    resolved = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)

    assert HOME_CODE in codes(resolved), resolved.dispatch_preconditions
    issue = issue_for(resolved, HOME_CODE)
    assert issue.location == "launch.dsh_home"
    assert str(home) in issue.detail and "outside" in issue.detail, issue.detail
    assert resolved.ready_to_dispatch is False


def test_prepare_refuses_a_dsh_home_in_the_worktree_directory(
    tmp_path: Path, live_project, worktree_task, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    _git(project_root, "init", "-q", "-b", "main")
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", "base")
    task = worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )
    home = project_root.parent / f"{project_root.name}.hflow-worktrees" / "home"
    monkeypatch.setenv("DSH_HOME", str(home))

    resolved = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert HOME_CODE in codes(resolved), resolved.dispatch_preconditions

    monkeypatch.setenv("DSH_HOME", str(tmp_path / "elsewhere" / "home"))
    outside = _resolve_live(tmp_path, live_project, task, project_root, live_profile)
    assert HOME_CODE not in codes(outside), outside.dispatch_preconditions


def test_prepare_refuses_a_relative_dsh_home_and_ignores_an_unbound_one(
    tmp_path: Path, live_project, task_spec, project_root: Path, live_profile, acpx_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DSH_HOME", ".dsh")
    relative = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)
    assert HOME_CODE in codes(relative), relative.dispatch_preconditions
    assert "not an absolute path" in issue_for(relative, HOME_CODE).detail

    monkeypatch.delenv("DSH_HOME", raising=False)
    unbound = _resolve_live(tmp_path, live_project, task_spec, project_root, live_profile)
    assert HOME_CODE not in codes(unbound), unbound.dispatch_preconditions
    assert all(entry.launch.dsh_home == "" for entry in unbound.effective.roles)


def test_a_dsh_home_does_not_refuse_the_offline_fake_driver(
    tmp_path: Path, project, task_spec, project_root: Path, profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DSH_HOME", str(project_root / ".dsh"))
    resolved = _resolve_live(tmp_path, project, task_spec, project_root, profile)
    assert HOME_CODE not in codes(resolved), resolved.dispatch_preconditions


def test_the_run_gate_refuses_a_dsh_home_inside_the_project_before_anything_is_spent(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DSH_HOME", str(project_root / ".dsh"))
    harness = harness_factory("cooperative")
    assert harness.driver.launch.dsh_home == str(project_root / ".dsh")
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
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(
                RunRequest(
                    task=task_spec,
                    project=project,
                    project_root=project_root,
                    workspace_root=project_root,
                )
            )
        assert excinfo.value.code is RefusalCode.DSH_HOME_IN_WORKSPACE
        assert store.find_run_by_spec_digest(project.project_id, task_spec.spec_digest()) is None
    finally:
        store.close()
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


# --------------------------------------------------------------------------
# DSH_HOME: spawn gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["equal", "inside", "around", "relative"])
def test_the_spawn_gate_refuses_a_dsh_home_in_or_around_the_workspace(
    tmp_path: Path, harness_factory, monkeypatch: pytest.MonkeyPatch, where: str,  # noqa: F811
) -> None:
    # The harness workspace is <tmp>/cooperative-0/ws; the home is fixed before it is built.
    workspace = (tmp_path / "cooperative-0" / "ws").resolve()
    home = {
        "equal": str(workspace),
        "inside": str(workspace / ".dsh"),
        "around": str(workspace.parent),
        "relative": ".dsh",
    }[where]
    monkeypatch.setenv("DSH_HOME", home)
    harness = harness_factory("cooperative")
    assert harness.workspace == workspace
    facts: list = []

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(
            request_for(harness, harness.workspace, role="implementer", facts=facts, iid="I-h")
        )

    assert excinfo.value.code is RefusalCode.DSH_HOME_IN_WORKSPACE
    assert "No client process was started" in excinfo.value.message
    assert_nothing_launched(harness, facts)


def test_the_spawn_gate_refuses_a_dsh_home_in_the_checkout_of_a_worktree(
    tmp_path: Path, harness_factory, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """In a run's worktree the user's checkout is named from the path; a home there refuses."""
    checkout = tmp_path / "repo"
    worktree = tmp_path / "repo.hflow-worktrees" / "R-1"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("DSH_HOME", str(checkout / ".dsh"))
    harness = harness_factory("cooperative")
    facts: list = []

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(
            request_for(harness, worktree, role="reviewer", facts=facts, iid="I-co")
        )

    assert excinfo.value.code is RefusalCode.DSH_HOME_IN_WORKSPACE
    assert str(checkout) in excinfo.value.message
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


def test_an_outside_or_unbound_dsh_home_launches(
    tmp_path: Path, harness_factory, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "outside-home"))
    bound = harness_factory("cooperative")
    handle, _ = bound.start()
    assert bound.driver.collect(handle).launch_surfaces is not None
    bound.driver.release(handle.invocation_id)

    monkeypatch.delenv("DSH_HOME", raising=False)
    unbound = harness_factory("cooperative")
    assert unbound.driver.launch.dsh_home == ""
    handle, _ = unbound.start("I-2")
    result = unbound.driver.collect(handle)
    assert result.launch_surfaces is not None
    assert result.launch_surfaces.dsh_home_kind == "per_invocation"
    unbound.driver.release(handle.invocation_id)
