"""Shared fixtures. Every test runs fully offline against the fake driver."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    MachineProfile,
    ProjectConfig,
    ProfileLimits,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    ReviewRequirement,
    RunRequest,
    Scope,
    TaskSpec,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.runtime import controller_build
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

PROJECT_ID = "demo-project"

#: The checked-in stand-in for the acpx client. A test that resolves a *real* binding needs the
#: launcher to resolve, and the real acpx install is machine-local (`.probe/` is not committed),
#: so tests point at this file instead. Nothing here is a compatibility proof.
FAKE_ACPX_CLIENT = Path(__file__).resolve().parent / "fixtures" / "fake_acpx_client.py"


@pytest.fixture(autouse=True)
def _isolated_default_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Point the platform data dir at a per-test temp dir.

    Anything that falls back to :func:`hflow.paths.default_data_dir` (a CLI call without
    ``--data-dir``, an in-memory ledger) would otherwise read the user's real profiles and write
    into their real ``%LOCALAPPDATA%/HFlow``. A profile id inherited from the shell would name a
    profile in that real directory, so it is cleared too.
    """
    directory = tmp_path_factory.mktemp("hflow-default-data")
    monkeypatch.setenv("HFLOW_DATA_DIR", str(directory))
    monkeypatch.delenv("HFLOW_PROFILE", raising=False)
    return directory


@pytest.fixture(autouse=True)
def _dsh_launcher_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Put a stand-in DSH launcher first on PATH, shaped like the real install.

    A real launch resolves ``dsh`` to an absolute file on PATH and is not resolvable without one,
    so whether a test's launch resolves would otherwise depend on what this machine has
    installed - and a real DSH must never be what a test finds. The stand-in is an npm-style batch
    shim on Windows (the shape ADR 0001 records) that only exits 1; nothing is meant to run it.
    """
    directory = tmp_path_factory.mktemp("dsh-launcher")
    if sys.platform == "win32":
        (directory / "dsh.CMD").write_text("@exit /b 1\r\n", encoding="utf-8")
    else:
        launcher = directory / "dsh"
        launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return directory


@pytest.fixture()
def acpx_client(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the production launch resolve to the checked-in client stand-in.

    Without this, whether a live binding resolves would depend on what happens to be installed
    on the machine running the tests.
    """
    monkeypatch.setenv("HFLOW_ACPX_CLI", str(FAKE_ACPX_CLIENT))
    monkeypatch.delenv("HFLOW_ACPX_NODE", raising=False)
    return FAKE_ACPX_CLIENT


@pytest.fixture()
def worktree_task(task_spec: TaskSpec) -> TaskSpec:
    """The same task as an isolated real delivery: a worktree with a fixed base commit.

    A task that declares write paths cannot run in place, so this is the shape a real
    (production) run has to have - and the shape a preview must be exercised against.
    """
    return task_spec.model_copy(
        update={
            "workspace": WorkspaceSpec(mode="worktree", base_commit="0" * 40),
        }
    )


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
    (root / "tests" / "test_parser.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def project(project_root: Path) -> ProjectConfig:
    return ProjectConfig(
        project_id=PROJECT_ID,
        checks=[
            CheckDef(id="unit", kind="fake"),
            CheckDef(id="docs-check", kind="fake"),
        ],
        write_deny=[".hflow/**", ".github/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=1),
        min_risk_for_review="standard",
        review_required=True,
    )


@pytest.fixture()
def task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="T-001",
        revision=1,
        goal="Fix the empty-input crash while keeping existing behaviour",
        acceptance=[
            AcceptanceCriterion(
                id="AC-1", statement="empty input returns the agreed empty result", check_ids=["unit"]
            ),
            AcceptanceCriterion(
                id="AC-2", statement="existing tests keep passing", check_ids=["unit", "docs-check"]
            ),
        ],
        scope=Scope(write_allow=["src/parser.py", "tests/test_parser.py"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:parser-library-choice",
            reason="reuses the existing parser component; no new dependency",
        ),
        review=ReviewRequirement(required=True),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=1),
    )


@pytest.fixture()
def live_project(project: ProjectConfig) -> ProjectConfig:
    """The same contract with approved *command* checks: what a real delivery requires.

    Admission refuses ``kind=fake`` for a real Harness run, so a live-profile test needs a
    project whose checks actually execute something.
    """
    return project.model_copy(
        update={
            "checks": [
                CheckDef(id="unit", kind="command", argv=[sys.executable, "-c", "pass"]),
                CheckDef(id="docs-check", kind="command", argv=[sys.executable, "-c", "pass"]),
            ]
        }
    )


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    instance = Store(tmp_path / "data" / "hflow.sqlite")
    yield instance
    instance.close()


@pytest.fixture()
def check_runner() -> FakeCheckRunner:
    return FakeCheckRunner()


@pytest.fixture()
def fake_script() -> FakeScript:
    return FakeScript(
        write_plan={
            "src/parser.py": "def parse(text):\n    if not text:\n        return None\n    return text\n"
        },
        agent_turns=1,
    )


@pytest.fixture()
def driver(project_root: Path, fake_script: FakeScript) -> FakeDriver:
    return FakeDriver(project_root, fake_script)


@pytest.fixture()
def controller(
    store: Store, driver: FakeDriver, check_runner: FakeCheckRunner
) -> Controller:
    return Controller(
        store,
        driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": check_runner, "command": check_runner}),
    )


@pytest.fixture()
def run_request(project: ProjectConfig, task_spec: TaskSpec, project_root: Path) -> RunRequest:
    return RunRequest(
        task=task_spec,
        project=project,
        project_root=project_root,
        workspace_root=project_root,
    )


@pytest.fixture()
def profile() -> MachineProfile:
    return MachineProfile(
        profile_id="dsh-local",
        role_bindings={"implementer": "dsh-default", "reviewer": "dsh-default"},
        agents={
            "dsh-default": {
                "harness": "dsh",
                "driver": "fake-offline",
                "model_selection": "native_profile",
                "capability_record": "local-capability-record-id",
            }
        },
    )


def write_task(path: Path, spec: TaskSpec) -> Path:
    path.write_text(json.dumps(spec.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def write_profile(data_dir: Path, profile: MachineProfile) -> Path:
    """Write one machine profile where the loader looks for it."""
    path = data_dir / "profiles" / f"{profile.profile_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def live_profile() -> MachineProfile:
    """Two roles, two agents, two model selections: what per-role binding is for."""
    return MachineProfile(
        profile_id="dsh-local",
        role_bindings={"implementer": "dsh-implementer", "reviewer": "dsh-reviewer"},
        agents={
            "dsh-implementer": {
                "harness": "dsh",
                "driver": "acpx-dsh",
                "model_selection": "implementer-model",
                "capability_record": "local-capability-record-id",
            },
            "dsh-reviewer": {
                "harness": "dsh",
                "driver": "acpx-dsh",
                "model_selection": "reviewer-model",
                "capability_record": "local-capability-record-id",
            },
        },
        limits=ProfileLimits(max_parallel_workers=1, max_native_children=0),
    )


def unit_only(spec: TaskSpec) -> TaskSpec:
    """Same task, restricted to the single approved check `unit`.

    Used by tests that replace the project contract with a narrower one; admission
    would otherwise (correctly) refuse an acceptance criterion for a missing check.
    """
    return spec.model_copy(
        update={
            "acceptance": [
                AcceptanceCriterion(
                    id="AC-1",
                    statement=spec.acceptance[0].statement,
                    check_ids=["unit"],
                )
            ]
        }
    )


def write_project(path: Path, project: ProjectConfig) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(project.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path
