"""The pinned real acpx client, a mock ACP agent, and the production review wire.

This is still zero model work: the client is the installed ``acpx`` (Node), the agent is the
project's own mock ACP process, and no provider credential is read or injected. What it adds
over the Python stand-in is the part the stand-in cannot prove - that the *real* client's
``--format json`` stream is what the driver's review extraction actually parses, end to end
through the controller to a receipt.

If the pinned client is not installed the module is skipped, and the skip reason says so
rather than pretending the wire was verified.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    ProjectConfig,
    ProjectLimits,
    ReuseDecision,
    ReuseStatus,
    RefusalCode,
    RefusedError,
    ReviewRequirement,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import AcpxDshDriver
from hflow.packet import packet_digest
from hflow.store import Store
from hflow.verify import CheckRunners, FakeCheckRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLED_ACPX = REPO_ROOT / ".probe" / "acpx" / "node_modules" / "acpx" / "dist" / "cli.js"
MOCK_AGENT = REPO_ROOT / "tools" / "m0_probe" / "mock_acp_agent.py"


def _require_real_client() -> tuple[str, Path]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH; the pinned acpx client cannot be launched")
    if not INSTALLED_ACPX.is_file():
        pytest.skip(f"pinned acpx is not installed at {INSTALLED_ACPX}")
    return node, INSTALLED_ACPX


def _spec_and_project() -> tuple[TaskSpec, ProjectConfig]:
    spec = TaskSpec(
        task_id="T-real-client-review",
        revision=1,
        goal="Fix the empty-input crash in src/parser.py and keep valid input working",
        acceptance=[
            AcceptanceCriterion(
                id="AC-1", statement="empty input returns the agreed empty result", check_ids=["unit"]
            )
        ],
        scope=Scope(write_allow=["src/parser.py"], write_deny=[".git/**"]),
        risk="standard",
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:python-stdlib-only",
            reason="standard library only; no new dependency",
        ),
        review=ReviewRequirement(required=True),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
    )
    project = ProjectConfig(
        project_id="real-client-review",
        checks=[CheckDef(id="unit", kind="fake")],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=0),
        min_risk_for_review="standard",
        review_required=True,
    )
    return spec, project


def _real_client_driver(tmp_path: Path, run_dir: Path, wire_log: Path) -> AcpxDshDriver:
    return AcpxDshDriver(
        data_dir=run_dir / "data",
        acpx_cli=INSTALLED_ACPX,
        python_executable=sys.executable,
        agent_argv_override=[
            sys.executable,
            "-u",
            str(MOCK_AGENT),
            "--scenario",
            "role-answer",
            "--wire-log",
            str(wire_log),
            "--ready-file",
            str(run_dir / "mock-ready.txt"),
        ],
        completion_timeout_seconds=120,
    )


@pytest.mark.parametrize("review_mode", ["fenced", "prose"])
def test_real_client_delivers_the_reviewer_verdict_to_a_receipt(tmp_path: Path, review_mode: str) -> None:
    """Real client + mock agent => the production collector and controller decide the run.

    ``fenced`` is the recorded reviewer's shape and must end in a controller receipt;
    ``prose`` is the same turn without a structured verdict and must block. The contrast is
    the point: the outcome follows the *content contract*, not the fact that the turn
    completed.
    """
    _require_real_client()
    run_dir = (tmp_path / "real-client-mock").resolve()
    workspace = run_dir / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")

    driver = _real_client_driver(tmp_path, run_dir, run_dir / "mock-wire.jsonl")
    driver.extra_env["MOCK_IMPLEMENTER_PATH"] = "src/parser.py"
    driver.extra_env["MOCK_REVIEW_MODE"] = review_mode

    spec, project = _spec_and_project()
    store = Store(run_dir / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=run_dir / "data",
        # Real client and real transport, but an offline run with fake checks: the production
        # gates are stated as off rather than inferred away.
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=project,
                project_root=workspace,
                workspace_root=workspace,
                deadline_seconds=120,
            )
        )
        review_evidence = [
            dict(row) for row in store.evidence_for(outcome.run_id, "review")
        ]
    finally:
        for invocation_id in list(driver._handles):
            driver.release(invocation_id)
        store.close()

    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 1, (
        "both invocations must have gone through the real client"
    )
    if review_mode == "fenced":
        assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
        assert outcome.receipt is not None
        assert outcome.receipt.review.status == "accepted"
        assert len(review_evidence) == 1
        assert review_evidence[0]["status"] == "passed"
    else:
        assert outcome.task_state is TaskState.BLOCKED
        assert outcome.receipt is None
        assert outcome.block_code is not None
        assert outcome.block_code.value == "review_protocol_error"
        assert len(review_evidence) == 1
        assert review_evidence[0]["status"] == "error"

    # The mock agent really was launched by the real client, and no session was resumed.
    assert (run_dir / "mock-ready.txt").is_file()


def test_real_client_launch_path_stays_the_production_one(tmp_path: Path) -> None:
    """The driver still launches the pinned Node entry point through its own launcher."""
    node, entry = _require_real_client()
    driver = AcpxDshDriver(data_dir=tmp_path / "data", acpx_cli=entry, python_executable=sys.executable)

    argv = driver._client_argv(tmp_path, tmp_path, 60)

    assert argv[0] == node, "the installed client is a Node program, not a Python script"
    assert argv[1] == str(entry)
    assert argv[-2:] == ["-f", "-"], "the task travels on stdin, never in a command line"


def _received_prompts(wire_log: Path) -> list[str]:
    """The prompt text the mock agent itself read, one entry per ``session/prompt``."""
    prompts = []
    for line in wire_log.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        message = entry["payload"]
        if entry["dir"] == "in" and message.get("method") == "session/prompt":
            blocks = message["params"]["prompt"]
            assert [block["type"] for block in blocks] == ["text"], blocks
            prompts.append(blocks[0]["text"])
    return prompts


def test_real_client_turn_completes_under_the_hardened_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C1 hardening leaves the real client path working, and makes the digest exact.

    An ambient ``DSH_PERMISSION_MODE``/``DSH_TOOLS_MODE`` is present in this process and is
    removed from the child environment; both turns still complete to a receipt. And because the
    rendered packets carry no surrounding whitespace, the text the agent received after acpx's
    own trimming is byte-for-byte the text whose digest the driver reported.
    """
    _require_real_client()
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    monkeypatch.setenv("DSH_TOOLS_MODE", "full")
    run_dir = (tmp_path / "real-client-hardened").resolve()
    workspace = run_dir / "ws"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
    wire_log = run_dir / "mock-wire.jsonl"

    driver = _real_client_driver(tmp_path, run_dir, wire_log)
    driver.extra_env["MOCK_IMPLEMENTER_PATH"] = "src/parser.py"
    driver.extra_env["MOCK_REVIEW_MODE"] = "fenced"
    assert "DSH_PERMISSION_MODE" not in driver._child_env(workspace)

    spec, project = _spec_and_project()
    store = Store(run_dir / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=run_dir / "data",
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=project,
                project_root=workspace,
                workspace_root=workspace,
                deadline_seconds=120,
            )
        )
    finally:
        for invocation_id in list(driver._handles):
            driver.release(invocation_id)
        store.close()

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.receipt is not None
    sent = sorted(
        path.read_bytes().decode("utf-8")
        for path in (run_dir / "data" / "invocations").glob("*/task.txt")
    )
    received = sorted(_received_prompts(wire_log))
    assert len(sent) == 2 and received == sent, "acpx must deliver the packet unchanged"
    assert {packet_digest(text) for text in received} == {packet_digest(text) for text in sent}


@pytest.mark.parametrize("planted", ["at_submission", "at_launch"])
def test_real_client_is_never_launched_on_a_workspace_with_client_config(
    tmp_path: Path, planted: str
) -> None:
    """A planted ``.acpxrc.json`` would replace the agent argv inside the real client.

    So the real client is not started at all. A file already there when the task is submitted is
    refused by the run gate before anything is recorded; one that appears after admission is
    caught at the spawn gate, and the run blocks with the workspace-config code. Either way the
    mock agent never comes up.
    """
    _require_real_client()
    run_dir = (tmp_path / "real-client-acpxrc").resolve()
    workspace = run_dir / "ws"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
    config = workspace / ".acpxrc.json"
    config_text = json.dumps({"agents": {"acpx-dsh-acp": {"command": "cmd.exe"}}})
    driver = _real_client_driver(tmp_path, run_dir, run_dir / "mock-wire.jsonl")
    if planted == "at_submission":
        config.write_text(config_text, encoding="utf-8")
    else:
        original = driver.start_handle

        def start_handle(request):  # noqa: ANN001, ANN202 - the driver's own shape
            config.write_text(config_text, encoding="utf-8")
            return original(request)

        driver.start_handle = start_handle  # type: ignore[method-assign]

    spec, project = _spec_and_project()
    store = Store(run_dir / "hflow.sqlite")
    runner = FakeCheckRunner()
    controller = Controller(
        store,
        driver,
        controller_build="test-build",
        runners=CheckRunners({"fake": runner}),
        data_dir=run_dir / "data",
        production=False,
    )
    request = RunRequest(
        task=spec, project=project, project_root=workspace, workspace_root=workspace
    )
    try:
        if planted == "at_submission":
            with pytest.raises(RefusedError) as excinfo:
                controller.run_task(request)
            assert excinfo.value.code is RefusalCode.WORKSPACE_CLIENT_CONFIG
            assert store.find_run_by_spec_digest(project.project_id, spec.spec_digest()) is None
        else:
            outcome = controller.run_task(request)
            assert outcome.task_state is TaskState.BLOCKED
            assert outcome.block_code is RefusalCode.WORKSPACE_CLIENT_CONFIG, outcome.block_reason
    finally:
        store.close()

    assert driver._processes == {}
    assert not (run_dir / "mock-ready.txt").exists(), "the mock agent must never have started"
