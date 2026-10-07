"""A real launch with no visible credential source is refused before dispatch.

User ruling 2026-10-07, "refuse before dispatch". DSH takes its credential from the launch
environment, then ``$DSH_HOME/.credentials.yaml``, then ``<cwd>/.env``, then ``$DSH_HOME/.env``
(documented upstream, not observed); without one it fails with a no-API-key error before any model
work (observed in M0), and HFlow would record ``outcome_unknown``, spend the approved submission and
leave a ledger entry for ``ledger settle``. A real launch never starts on a workspace ``.env``
(``workspace_env_file``), so on a real driver a launch is refused when ``DEEPSEEK_API_KEY`` is not
among the child's variable names and the DSH home it would use holds neither home file (stat only;
an unbound ``DSH_HOME`` is the empty per-invocation home, so only the environment counts) - at
admission (``prepare`` and the run gate) and again at the driver's spawn gate. The offline fake
driver is unaffected. A presence check, never a validity check, and nothing is opened.

Everything here is offline: the "real" driver runs a test-only client and stub agent. ``conftest``
gives every test a stand-in key; the tests here remove it where the refusal is the subject.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
from pathlib import Path

import pytest

from hflow.authorization import AuthorizationRecord, current_binding
from hflow.cli import EXIT_OK, EXIT_REFUSED, main
from hflow.contracts import (
    InvocationOutcome,
    RefusalCode,
    RefusedError,
    RunRequest,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import credential_source_problem
from hflow.drivers.dsh_surfaces import credential_sources
from hflow.drivers.fake import FakeDriver
from hflow.paths import database_path
from hflow.prepare import build_prepare_report, render_prepare_text, resolve_run
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner
from tests.test_driver_acpx_dsh import DriverHarness, harness_factory  # noqa: F401 - fixture
from tests.test_launch_refusals import assert_nothing_launched, request_for
from tests.test_m2_slice import _git as _m2_git, _project, _task, sample_repo  # noqa: F401
from tests.test_prepare import _git, _resolve_live

from .conftest import write_profile, write_project, write_task

CODE = RefusalCode.NO_CREDENTIAL_SOURCE.value
KEY = "DEEPSEEK_API_KEY"
#: Planted as the content of every credential file and as a key value: it must never be printed.
SENTINEL = "sk-sentinel-credential-j-c-91d2"
#: The tables that would hold anything a run spent or recorded.
LEDGER_TABLES = ("runs", "attempts", "invocations", "authorizations", "root_budgets")


def codes(resolved) -> list[str]:
    return [issue.code.value for issue in resolved.dispatch_preconditions]


def issue_for(resolved, code: str):
    return next(issue for issue in resolved.dispatch_preconditions if issue.code.value == code)


def ledger_counts(store: Store) -> dict[str, int]:
    return {
        table: store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in LEDGER_TABLES
    }


def home_with(tmp_path: Path, *names: str) -> Path:
    """A DSH home outside every workspace, holding ``names`` as files with sentinel content."""
    home = tmp_path / "bound-home"
    home.mkdir(exist_ok=True)
    (home / "cordis.patch.yml").write_text("approval: never\n", encoding="utf-8")
    for name in names:
        (home / name).write_text(f"secret: {SENTINEL}\n", encoding="utf-8")
    return home


@pytest.fixture()
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``DEEPSEEK_API_KEY`` and no ``DSH_HOME`` - the conftest stand-in key removed."""
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv("DSH_HOME", raising=False)


@pytest.fixture()
def never_open_credentials(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every attempt to open a file named ``.credentials.yaml`` or ``.env`` (any case).

    Recorded rather than raised: launch-surface observation swallows its own errors (a record
    never stops a launch or a preview), so a raise there would hide the very open it caught.
    """
    opened: list[str] = []
    watched = {".credentials.yaml", ".env"}
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open

    def is_watched(file) -> bool:  # noqa: ANN001 - whatever open() accepts
        try:
            return os.path.basename(os.fspath(file)).casefold() in watched
        except TypeError:
            return False  # a file descriptor

    def guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_watched(file):
            opened.append(os.fspath(file))
        return original_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_watched(file):
            opened.append(os.fspath(file))
        return original_io_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if is_watched(path):
            opened.append(os.fspath(path))
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    return opened


@pytest.fixture()
def clean_worktree_task(
    tmp_path: Path, worktree_task, project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """A worktree task on a committed base that a real run admits with nothing else to refuse."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    _git(project_root, "init", "-q", "-b", "main")
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", "base")
    return worktree_task.model_copy(
        update={"workspace": WorkspaceSpec(mode="worktree", base_commit="main", keep=True)}
    )


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------


def test_the_key_counts_by_name_and_a_bound_homes_files_by_stat(tmp_path: Path) -> None:
    unbound = tmp_path / "invocations" / "<invocation-id>" / "home" / ".dsh"
    missing = credential_source_problem({}, dsh_home_kind="per_invocation", dsh_home=unbound)
    assert "DEEPSEEK_API_KEY is not in the launch environment" in missing
    assert "DSH_HOME is unbound" in missing and "created empty" in missing, missing
    assert credential_source_problem(
        {KEY: SENTINEL}, dsh_home_kind="per_invocation", dsh_home=unbound
    ) == ""
    # A presence check: an empty value is still a variable of that name, and passes.
    assert credential_source_problem(
        {KEY: ""}, dsh_home_kind="per_invocation", dsh_home=unbound
    ) == ""

    home = tmp_path / "home"
    home.mkdir()
    neither = credential_source_problem({}, dsh_home_kind="bound", dsh_home=home)
    assert f"the bound DSH home {home}" in neither
    assert ".credentials.yaml absent, .env absent" in neither, neither
    assert "never opened" in neither
    (home / ".credentials.yaml").mkdir()
    assert "present (not a file)" in credential_source_problem(
        {}, dsh_home_kind="bound", dsh_home=home
    ), "a directory is not a credential DSH could read"
    (home / ".env").write_text("A=1\n", encoding="utf-8")
    assert credential_source_problem({}, dsh_home_kind="bound", dsh_home=home) == ""
    assert credential_source_problem(
        {KEY: "x"}, dsh_home_kind="bound", dsh_home=tmp_path / "empty-home"
    ) == "", "the environment comes first"

    relative = credential_source_problem({}, dsh_home_kind="bound", dsh_home=Path(".dsh"))
    assert "not looked into" in relative, relative
    if sys.platform == "win32":
        assert credential_source_problem(
            {KEY.lower(): "x"}, dsh_home_kind="per_invocation", dsh_home=unbound
        ) == "", "Windows variable names are case-insensitive"


@pytest.mark.parametrize("name", [".credentials.yaml", ".env"])
def test_either_home_file_alone_is_a_source(tmp_path: Path, name: str) -> None:
    home = home_with(tmp_path, name)
    sources = credential_sources({}, dsh_home_kind="bound", dsh_home=home)
    assert sources.visible and not sources.api_key_in_env
    assert dict(sources.home_files)[name] == "present"
    assert SENTINEL not in repr(sources), "names and presence only"


def test_the_per_invocation_home_is_never_looked_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbound, the home is created empty for each invocation: only the environment counts."""
    home = tmp_path / "home" / ".dsh"
    home.mkdir(parents=True)
    (home / ".credentials.yaml").write_text("token: x\n", encoding="utf-8")
    statted: list[str] = []
    real_stat = os.stat

    def recording_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        statted.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", recording_stat)
    sources = credential_sources({}, dsh_home_kind="per_invocation", dsh_home=home)

    assert sources.home_files == () and not sources.visible
    assert not [path for path in statted if str(home) in path]


# --------------------------------------------------------------------------
# prepare / admission
# --------------------------------------------------------------------------


def test_prepare_refuses_with_no_key_and_an_unbound_home(
    tmp_path: Path, live_project, clean_worktree_task, project_root: Path, live_profile,
    acpx_client, no_key, never_open_credentials: list[str],
) -> None:
    resolved = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile
    )

    assert codes(resolved) == [CODE], "the only thing standing between this run and dispatch"
    issue = issue_for(resolved, CODE)
    assert issue.location == "launch.credentials"
    assert "DSH_HOME is unbound" in issue.detail, issue.detail
    assert "Refused before anything was dispatched or charged" in issue.detail
    assert "observed in M0" in issue.detail and "outcome_unknown" in issue.detail
    assert "ledger settle" in issue.detail
    assert "does not prove the credential is valid" in issue.detail
    assert resolved.ready_to_dispatch is False
    text = render_prepare_text(build_prepare_report(resolved))
    assert f"refuse      {CODE} at launch.credentials" in text
    assert never_open_credentials == []


def test_hflow_prepare_exits_refused_and_lists_it(
    tmp_path: Path, live_project, clean_worktree_task, project_root: Path, live_profile,
    acpx_client, no_key, capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    argv = [
        "prepare",
        "--task", str(write_task(tmp_path / "task.json", clean_worktree_task)),
        "--project", str(write_project(tmp_path / "hflow" / "project.json", live_project)),
        "--project-root", str(project_root),
        "--profile", live_profile.profile_id,
        "--json",
        "--data-dir", str(data_dir),
    ]

    assert main(argv) == EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert [issue["code"] for issue in payload["dispatch_preconditions"]] == [CODE]
    assert any(note.startswith(f"dispatch precondition: {CODE}: ") for note in payload["notes"])
    assert not (data_dir / "invocations").exists(), "prepare created nothing"


def test_prepare_refuses_a_bound_home_holding_neither_file(
    tmp_path: Path, live_project, clean_worktree_task, project_root: Path, live_profile,
    acpx_client, no_key, monkeypatch: pytest.MonkeyPatch, never_open_credentials: list[str],
) -> None:
    home = home_with(tmp_path)
    monkeypatch.setenv("DSH_HOME", str(home))

    resolved = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile
    )

    assert codes(resolved) == [CODE], resolved.dispatch_preconditions
    detail = issue_for(resolved, CODE).detail
    assert str(home) in detail and ".credentials.yaml absent, .env absent" in detail, detail
    assert never_open_credentials == []


@pytest.mark.parametrize("name", [".credentials.yaml", ".env"])
def test_prepare_accepts_a_bound_home_holding_either_file(
    tmp_path: Path, live_project, clean_worktree_task, project_root: Path, live_profile,
    acpx_client, no_key, monkeypatch: pytest.MonkeyPatch, never_open_credentials: list[str],
    name: str,
) -> None:
    monkeypatch.setenv("DSH_HOME", str(home_with(tmp_path, name)))
    never_open_credentials.clear()  # planting the file was the test's own open

    resolved = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile
    )

    assert codes(resolved) == [], resolved.dispatch_preconditions
    assert resolved.ready_to_dispatch is True
    report = build_prepare_report(resolved)
    assert SENTINEL not in report.model_dump_json()
    assert never_open_credentials == [], "a credential file is stat'ed, never opened"


def test_prepare_judges_the_launch_environment_it_is_given(
    tmp_path: Path, live_project, clean_worktree_task, project_root: Path, live_profile,
    acpx_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key in the launch environment admits the run; the value is never part of anything."""
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.setenv(KEY, SENTINEL)
    present = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile
    )
    assert codes(present) == [], present.dispatch_preconditions
    assert SENTINEL not in build_prepare_report(present).model_dump_json()

    without = {name: value for name, value in os.environ.items() if name.upper() != KEY}
    absent = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile, env=without
    )
    assert codes(absent) == [CODE], "an explicit environment without the key is judged as given"

    monkeypatch.delenv(KEY)
    given = _resolve_live(
        tmp_path, live_project, clean_worktree_task, project_root, live_profile,
        env={**without, KEY: SENTINEL},
    )
    assert codes(given) == [], given.dispatch_preconditions


def test_the_offline_fake_driver_is_unaffected(
    tmp_path: Path, project, task_spec, project_root: Path, profile, controller, run_request,
    no_key,
) -> None:
    resolved = _resolve_live(tmp_path, project, task_spec, project_root, profile)
    assert CODE not in codes(resolved), resolved.dispatch_preconditions

    outcome = controller.run_task(run_request)
    assert outcome.block_code is not RefusalCode.NO_CREDENTIAL_SOURCE, outcome.block_reason
    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason


# --------------------------------------------------------------------------
# the run gate: nothing reserved, dispatched or recorded
# --------------------------------------------------------------------------


def test_the_run_gate_refuses_before_anything_is_spent_and_a_resubmission_works(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory,  # noqa: F811
    no_key, monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    request = RunRequest(
        task=task_spec, project=project, project_root=project_root, workspace_root=project_root
    )
    try:
        before = ledger_counts(store)
        with pytest.raises(RefusedError) as excinfo:
            controller.run_task(request)
        assert excinfo.value.code is RefusalCode.NO_CREDENTIAL_SOURCE
        assert "before anything was dispatched or charged" in excinfo.value.message
        assert ledger_counts(store) == before == dict.fromkeys(LEDGER_TABLES, 0)
        assert harness.driver._processes == {}
        assert harness.stub_files("spawn") == []
        assert not (harness.data_dir / "invocations").exists(), "no invocation was even prepared"

        monkeypatch.setenv(KEY, SENTINEL)
        outcome = controller.run_task(request)
    finally:
        store.close()
    assert outcome.block_code is not RefusalCode.NO_CREDENTIAL_SOURCE, outcome.block_reason
    assert harness.stub_files("spawn"), "the same TaskSpec dispatched once the key was set"


def test_hflow_run_with_a_real_profile_and_no_key_exits_refused_and_records_nothing(
    tmp_path: Path, sample_repo: Path, live_profile, acpx_client,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the CLI with a matching approval: exit 2, no run row, no authorization, no spend."""
    monkeypatch.setenv("HFLOW_ALLOW_WRITES", "true")
    monkeypatch.delenv("DSH_HOME", raising=False)
    data_dir = tmp_path / "data"
    write_profile(data_dir, live_profile)
    project = _project(sample_repo)
    task = _task(sample_repo, _m2_git(sample_repo, "rev-parse", "HEAD").strip())
    project_file = write_project(tmp_path / "project.json", project)
    task_file = write_task(tmp_path / "task.json", task)
    resolved = resolve_run(
        task_path=task_file, project_root=sample_repo, data_dir=data_dir,
        project_path=project_file, profile_id=live_profile.profile_id,
    )
    assert codes(resolved) == [], "admitted while the key is set"
    approval = AuthorizationRecord(
        authorization_id="AUTH-credential-test",
        user_text="I approve this exact bounded run.",
        authorized_at="2026-10-07T00:00:00Z",
        max_top_level_submissions=4,
        binding=current_binding(
            mode="m2-live-change", driver=resolved.effective.role("implementer").driver,
            project=project, request=resolved.request(), spec_path=task_file,
            effective=resolved.effective,
        ),
    )
    approval_file = tmp_path / "approval.json"
    approval_file.write_text(approval.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv(KEY)

    code = main([
        "run", "--task", str(task_file), "--project", str(project_file),
        "--project-root", str(sample_repo), "--profile", live_profile.profile_id,
        "--authorization-file", str(approval_file), "--data-dir", str(data_dir), "--json",
    ])

    captured = capsys.readouterr()
    assert code == EXIT_REFUSED, captured
    assert f"{CODE}: no credential source is visible" in captured.err
    store = Store(database_path(data_dir))
    try:
        assert ledger_counts(store) == dict.fromkeys(LEDGER_TABLES, 0)
    finally:
        store.close()
    assert not (data_dir / "invocations").exists(), "no client was started, not even prepared"


# --------------------------------------------------------------------------
# the spawn gate: the environment the child is about to get
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implementer", "reviewer"])
@pytest.mark.parametrize("home", ["unbound", "bound_without_files"])
def test_the_spawn_gate_refuses_a_launch_with_no_credential_source(
    tmp_path: Path, harness_factory, no_key, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    never_open_credentials: list[str], role: str, home: str,
) -> None:
    if home == "bound_without_files":
        monkeypatch.setenv("DSH_HOME", str(home_with(tmp_path)))
    harness = harness_factory("cooperative")
    facts: list = []

    with pytest.raises(RefusedError) as excinfo:
        harness.driver.start_handle(
            request_for(harness, harness.workspace, role=role, facts=facts, iid="I-cred")
        )

    assert excinfo.value.code is RefusalCode.NO_CREDENTIAL_SOURCE
    message = excinfo.value.message
    assert "No client process was started" in message and "new revision" in message
    assert "identical TaskSpec returns this blocked run" in message
    assert "presence check" in message
    assert_nothing_launched(harness, facts)
    assert never_open_credentials == []


def test_the_spawn_gate_judges_the_environment_it_builds(
    tmp_path: Path, harness_factory, no_key, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    never_open_credentials: list[str],
) -> None:
    """The driver's own extra_env is part of the child environment; so is a bound home's file."""
    carried = harness_factory("cooperative")
    carried.driver.extra_env[KEY] = SENTINEL
    handle, _ = carried.start()
    result = carried.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED, result.limitations
    assert result.launch_surfaces is not None and result.launch_surfaces.deepseek_api_key_inherited
    assert SENTINEL not in result.model_dump_json()
    carried.driver.release(handle.invocation_id)

    monkeypatch.setenv("DSH_HOME", str(home_with(tmp_path, ".credentials.yaml")))
    never_open_credentials.clear()  # planting the file was the test's own open
    stored = harness_factory("cooperative")
    handle, _ = stored.start("I-2")
    result = stored.driver.collect(handle)
    assert result.outcome is InvocationOutcome.COMPLETED, result.limitations
    assert SENTINEL not in result.model_dump_json()
    stored.driver.release(handle.invocation_id)
    assert never_open_credentials == []


@pytest.mark.parametrize("removed_before", ["implementer", "reviewer"])
def test_a_key_removed_after_admission_blocks_the_run_at_the_spawn_gate(
    tmp_path: Path, project, task_spec, project_root: Path, harness_factory,  # noqa: F811
    fake_script, monkeypatch: pytest.MonkeyPatch, removed_before: str,
) -> None:
    """Admitted with the key; it is gone by the spawn. The run blocks with this code, the
    attempt is a failed launch (not an unknown outcome), and no client process exists."""
    monkeypatch.delenv("DSH_HOME", raising=False)
    harness = harness_factory("cooperative")
    original = harness.driver.start_handle

    def start_handle(request):  # noqa: ANN001, ANN202 - the driver's own shape
        monkeypatch.delenv(KEY, raising=False)
        return original(request)

    harness.driver.start_handle = start_handle  # type: ignore[method-assign]
    runner = FakeCheckRunner()
    implementer = harness.driver if removed_before == "implementer" else FakeDriver(
        project_root, fake_script
    )
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        implementer,
        reviewer_driver=harness.driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner, "command": runner}),
        data_dir=harness.data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=task_spec, project=project, project_root=project_root,
                workspace_root=project_root,
            )
        )
        attempts = [dict(row) for row in store.attempts_for(outcome.run_id)]
    finally:
        store.close()

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.NO_CREDENTIAL_SOURCE, outcome.block_reason
    assert outcome.receipt is None
    assert "no credential source is visible" in (outcome.block_reason or "")
    if removed_before == "implementer":
        # The reviewer's invocation belongs to the implementer's attempt row; the implementer's
        # own refused launch is that row's result.
        assert [row["outcome"] for row in attempts] == ["failed"], attempts
        assert f"{CODE}: no credential source is visible" in attempts[0]["result_json"]
    assert harness.driver._processes == {}
    assert harness.stub_files("spawn") == []


# --------------------------------------------------------------------------
# doctor: a fact line, exit code unchanged
# --------------------------------------------------------------------------


def _doctor(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict, str]:
    code = main(["doctor", "--json", "--data-dir", str(data_dir)])
    payload = json.loads(capsys.readouterr().out)
    assert main(["doctor", "--data-dir", str(data_dir)]) == code
    return code, payload, capsys.readouterr().out


def test_doctor_says_plainly_that_a_real_launch_would_be_refused(
    tmp_path: Path, no_key, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, text = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK, "a fact line never changes doctor's exit code"
    found = payload["readiness"]["credentials"]
    assert found["real_launch_refused"] is True
    assert f"would be REFUSED before dispatch ({CODE})" in found["detail"]
    line = next(line for line in text.splitlines() if line.startswith("credentials"))
    assert f"REFUSED before dispatch ({CODE})" in line


@pytest.mark.parametrize("source", ["key", ".credentials.yaml", ".env"])
def test_doctor_names_no_refusal_when_a_source_is_present(
    tmp_path: Path, no_key, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], never_open_credentials: list[str], source: str,
) -> None:
    if source == "key":
        monkeypatch.setenv(KEY, SENTINEL)
    else:
        monkeypatch.setenv("DSH_HOME", str(home_with(tmp_path, source)))
    never_open_credentials.clear()  # planting the file was the test's own open

    code, payload, text = _doctor(tmp_path / "data", capsys)

    assert code == EXIT_OK
    found = payload["readiness"]["credentials"]
    assert found["real_launch_refused"] is False
    assert "not refused for credentials" in found["detail"]
    assert "does not prove the credential is valid" in found["detail"]
    for rendered in (json.dumps(payload), text):
        assert SENTINEL not in rendered, "a credential value reached the output"
    assert never_open_credentials == [], "doctor opened a credential file"
