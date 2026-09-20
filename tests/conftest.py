"""Shared fixtures. Every test runs fully offline against the fake driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    MachineProfile,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    ReviewRequirement,
    RunRequest,
    Scope,
    TaskSpec,
)
from hflow.controller import Controller
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.runtime import controller_build
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

PROJECT_ID = "demo-project"


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
