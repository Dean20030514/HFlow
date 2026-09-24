"""The task contract must reach the Harness, through the production wire.

The defect this file closes (plan 3.2, P0): ``AcpxDshDriver.start_handle`` sent
``request.goal`` and nothing else, so the acceptance criteria, the write scope, the frozen
candidate identity and the check results never arrived. A fixed-answer stub could not see
that, because it answered ``accepted`` no matter what it was sent.

So the agent used here (`tests/fixtures/checking_acp_agent.py`) votes on its *input*: it
reviews as ``accepted`` only when every fragment the test requires is present in the prompt it
actually received. Each negative case drops exactly one fragment in transit and asserts that
the same run blocks, which is what makes these tests fail on unwired code rather than merely
passing on wired code.

Three kinds of evidence are kept apart here, and the difference matters:

* **fake client** (``tests/fixtures/fake_acpx_client.py``) - the bulk of the cases. It
  reproduces acpx's contract so the production driver is exercised end to end, but it is a
  Python stand-in: it proves the driver, not the installed client.
* **real pinned acpx + input-sensitive stub** (``_require_real_client``) - the two cases marked
  ``real_client``. The installed Node client is launched for real, and the assertion is still
  made against the prompt the stub *received*. This is the strongest claim this repository can
  make without a model.
* **real DSH** - not attempted here at all. No provider credential is read, no model is called
  and no session is resumed by anything in this file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    EvidenceStatus,
    InvocationRequest,
    ProjectConfig,
    ProjectLimits,
    RefusalCode,
    ReuseDecision,
    ReuseStatus,
    RunRequest,
    Scope,
    TaskSpec,
    TaskState,
    WorkspaceSpec,
)
from hflow.controller import Controller
from hflow.drivers.acpx_dsh import ENV_ALLOW_WRITES, PROMPT_ENVELOPE, AcpxDshDriver, effective_prompt
from hflow.packet import (
    OUTPUT_CONTRACT_NOTE,
    RenderedPacket,
    packet_digest,
    render_implementer_packet,
    render_reviewer_packet,
)
from hflow.store import Store
from hflow.verify import CheckRunners

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLIENT = FIXTURES / "fake_acpx_client.py"
CHECKING_AGENT = FIXTURES / "checking_acp_agent.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
#: The pinned client the probe installs. Never downloaded here; a missing copy skips.
INSTALLED_ACPX = REPO_ROOT / ".probe" / "acpx" / "node_modules" / "acpx" / "dist" / "cli.js"

GOAL = "Fix the empty-input crash in src/parser.py and keep valid input working"
CHECK_ARGV = [sys.executable, "-c", "raise SystemExit(0)"]

#: Fragments the *implementer* packet must carry, keyed by field group. The implementer is told
#: the task, its scope and how the run ends; it is never told the candidate identity or the check
#: results, because it produces them.
IMPLEMENTER_FRAGMENTS: dict[str, str] = {
    "task": "goal (verbatim from the TaskSpec):",
    "acceptance": "proved by check(s):",
    "scope": "- src/parser.py",
    "side_effects": "no network calls, installs, dependency upgrades or downloads",
    "permission": "you may change files inside the allowed write paths below",
    "completion": "## When you finish",
}

#: Fragments the *reviewer* packet must carry, one entry per field group in the plan's reviewer
#: table. The two maps overlap only where the same sentence legitimately appears in both packets
#: ("task", "acceptance", "scope").
REVIEWER_FRAGMENTS: dict[str, str] = {
    "task": "goal (verbatim from the TaskSpec):",
    "acceptance": "proved by check(s):",
    "scope": "- allowed write paths:",
    "candidate": "- candidate commit:",
    "evidence": "- verification status:",
    "permission": "your invocation is read-only",
    "output_contract": "Required keys: verdict, findings.",
}
OUTPUT_CONTRACT_FRAGMENT = "Required keys: verdict, findings."


def _require_real_client() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not on PATH; the pinned acpx client cannot be launched")
    if not INSTALLED_ACPX.is_file():
        pytest.skip(f"pinned acpx is not installed at {INSTALLED_ACPX}")


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class CheckedRun:
    """One prepared project + task, so each case differs only in what the stub is told.

    Every instance owns a fresh project and data directory. That matters: the controller keys a
    run by its TaskSpec digest and returns the existing run for an identical submission, so two
    cases sharing a database would silently assert against the first case's outcome.
    """

    #: Which client this case runs through. ``FAKE_CLIENT`` by default; the ``real_client``
    #: cases set the pinned acpx so the installed Node client is what carries the prompt.
    client: Path = FAKE_CLIENT

    def __init__(self, tmp_path: Path) -> None:
        self.run_dir = (tmp_path / "checked-run").resolve()
        self.repo = self.run_dir / "repo"
        self.scratch = self.run_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "parser.py").write_text(
            "def parse(text):\n    return text\n", encoding="utf-8"
        )
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "hflow@example.invalid")
        self._git("config", "user.name", "HFlow Test")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "baseline with an empty-input bug")
        self.base_commit = self._git("rev-parse", "HEAD").strip()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed argv, test fixture only
            ["git", *args], cwd=str(self.repo), capture_output=True, text=True, check=True
        )
        return completed.stdout

    def project(self) -> ProjectConfig:
        """The approved checks for this case: one real command, so the run is a delivery."""
        return ProjectConfig(
            project_id="checked-project",
            checks=[CheckDef(id="unit", kind="command", argv=CHECK_ARGV, timeout_seconds=120)],
            write_deny=[".git/**", ".hflow/**"],
            limits=ProjectLimits(max_agent_turns=2, max_repair_cycles=0),
            min_risk_for_review="standard",
            review_required=True,
        )

    def task(self) -> TaskSpec:
        return TaskSpec(
            task_id="T-packet-wire",
            revision=1,
            goal=GOAL,
            acceptance=[
                AcceptanceCriterion(
                    id="AC-1",
                    statement="empty input returns the agreed empty result",
                    check_ids=["unit"],
                )
            ],
            scope=Scope(write_allow=["src/parser.py"], write_deny=[".git/**"]),
            risk="standard",
            reuse=ReuseDecision(
                status=ReuseStatus.EXISTING_DECISION,
                reference="project:python-stdlib-only",
                reason="standard library only; no new dependency",
            ),
            budget=BudgetRequest(max_agent_turns=2, max_repair_cycles=0),
            workspace=WorkspaceSpec(mode="worktree", base_commit=self.base_commit, keep=True),
        )

    def driver(self, *, inputs: dict[str, list[str]] | None, wire_log: Path) -> AcpxDshDriver:
        """The production driver, whose agent records the role's prompt at ``wire_log``.

        One file per invocation is needed to assert on both prompts: the agent process is the same
        program for both roles, so a single log would show the last writer only.

        ``self.client`` decides which client carries the prompt: the Python stand-in for the bulk
        of the cases, or the real pinned Node client for the ``real_client`` cases.
        """
        argv = [
            sys.executable,
            "-u",
            str(CHECKING_AGENT),
        ]
        if inputs is not None:
            inputs_path = self.run_dir / "required-inputs.json"
            inputs_path.write_text(json.dumps(inputs, indent=2), encoding="utf-8")
            argv += ["--inputs", str(inputs_path)]
        driver = AcpxDshDriver(
            data_dir=self.run_dir / "data",
            acpx_cli=self.client,
            python_executable=sys.executable,
            agent_argv_override=argv,
            completion_timeout_seconds=120,
        )
        driver.extra_env["CHECKING_AGENT_PROMPT_DIR"] = str(wire_log)
        # The client announces the agent it launched; the stub keeps its own markers. Both are
        # harness scaffolding, so they are pointed outside the tree the controller snapshots -
        # otherwise the run would be refused as an out-of-scope write, which is a different
        # failure than the one these tests are about.
        driver.extra_env["STUB_SPAWN_LOG"] = str(self.scratch / "agent-spawns.jsonl")
        driver.extra_env["STUB_SCRATCH_DIR"] = str(self.scratch)
        driver.extra_env["STUB_IMPLEMENTER_PATH"] = "src/parser.py"
        return driver

    def client_argv(self, driver: AcpxDshDriver) -> list[str]:
        """The argv this run's client is launched with (used for the no-task-text check)."""
        return driver._client_argv(self.run_dir, self.repo, 60)

    def run(self, driver, *, production: bool = True, omits: list[tuple[str, str]] | None = None):
        """Run this case. ``omits`` replaces the *controller's* renderer for named roles.

        That seam is what lets a case model a renderer that never wrote a required field: the
        controller's own digest then describes the incomplete packet, so the run fails on the
        agent's check rather than on the digest binding. A driver that corrupts a *complete*
        packet in transit is a different defect, covered by its own test.
        """
        store = Store(self.run_dir / "hflow.sqlite")
        controller = OmittingController(
            store,
            driver,
            controller_build="packet-wire-test",
            runners=CheckRunners.offline_default(),
            data_dir=self.run_dir / "data",
            production=production,
            omits=omits,
        )
        try:
            outcome = controller.run_task(
                RunRequest(
                    task=self.task(),
                    project=self.project(),
                    project_root=self.repo,
                    workspace_root=self.repo,
                    deadline_seconds=120,
                )
            )
            review_evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "review")]
            notes = list(store.notes_for(outcome.run_id))
        finally:
            for invocation_id in list(driver._handles):
                driver.release(invocation_id)
            store.close()
        return outcome, review_evidence, notes

    def spawn_record(self) -> dict:
        """What the client recorded about the agent it launched: the prompt text."""
        path = self.scratch / "agent-spawns.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(lines[0])

    def wire_prompts(self) -> dict[str, str]:
        """Both invocations' prompts, as the agent itself recorded them."""
        directory = self.run_dir / "wire"
        assert directory.is_dir(), f"the stub wrote no prompt records in {directory}"
        records = {
            path.name.split(".")[0]: str(json.loads(path.read_text(encoding="utf-8"))["prompt"])
            for path in directory.glob("*.json")
        }
        assert records, f"the stub wrote no prompt records in {directory}"
        return records

    def reviewer_prompt(self, *, omit: str | None = None) -> str:
        """The reviewer's prompt, asserting the omitted field really is absent.

        The assertion matters more than the accessor: a mutation that silently changed nothing
        would leave every assertion below passing for the wrong reason.
        """
        prompt = self.wire_prompts()["reviewer"]
        if omit is not None:
            assert omit not in prompt, f"{omit!r} was expected to be absent from the reviewer prompt"
        return prompt

    def implementer_prompt(self, *, omit: str | None = None) -> str:
        prompt = self.wire_prompts()["implementer"]
        if omit is not None:
            assert omit not in prompt, f"{omit!r} was expected to be absent from the implementer prompt"
        return prompt

    def reviewer_check(self) -> dict:
        """What the reviewer invocation decided, and which fragments it did not find."""
        path = self.scratch / "reviewer-input-check.json"
        assert path.is_file(), (
            "the reviewer turn never ran; the implementer's own report is a different file"
        )
        return json.loads(path.read_text(encoding="utf-8"))


class RealAcpxCheckedRun(CheckedRun):
    """The same case, carried by the real pinned Node client instead of the Python stand-in."""

    client = INSTALLED_ACPX

@pytest.fixture()
def checked_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A factory, because parametrized cases must not share one run's state."""
    monkeypatch.setenv(ENV_ALLOW_WRITES, "1")
    created: list[CheckedRun] = []

    def make(real_client: bool = False) -> CheckedRun:
        run = (RealAcpxCheckedRun if real_client else CheckedRun)(tmp_path / f"case-{len(created)}")
        created.append(run)
        return run

    return make


def role_expectations() -> dict[str, dict[str, list[str]]]:
    """What the stub demands of each role, in the shape the stub reads.

    Roles are named explicitly rather than shared through a ``common`` group: the stub merges a
    ``common`` group into *both* roles, which would require the reviewer packet to contain the
    implementer's own instructions. The two packets are different documents, so their
    expectations are different maps.
    """
    return {
        "implementer": {
            section: [fragment] for section, fragment in IMPLEMENTER_FRAGMENTS.items()
        },
        "reviewer": {section: [fragment] for section, fragment in REVIEWER_FRAGMENTS.items()},
    }


#: How to remove exactly one required field *from the packet in transit*, so the stub's own
#: expectations stay complete. Each value is the first line of the field's block in the
#: *reviewer* packet; the stub then sees a packet missing that field and refuses, which is the
#: behaviour under test. The two packets name the same concepts differently (the reviewer is told
#: "- allowed write paths:", the implementer "## Allowed write paths"), so the implementer case
#: uses the constant below instead.
FIELD_LINES: dict[str, str] = {
    "task": "- goal (verbatim from the TaskSpec):",
    "acceptance": "- AC-1:",
    "scope": "- allowed write paths:",
    "candidate": "- candidate commit:",
    "evidence": "- verification status:",
    "permission": "- your invocation is read-only",
    "output_contract": "Required keys: verdict, findings.",
}

#: The implementer's scope sentence, used to drive a renderer that omits that block.
IMPLEMENTER_SCOPE_FIELD = "- src/parser.py"


class OmittingController(Controller):
    """The real controller, rendering packets through a renderer that omits one field.

    Overriding ``render`` (not the controller's logic) is the point: everything else - budget,
    freeze, verification, evidence, review decoding, acceptance - stays exactly the production
    path, so the only variable under test is what one role's prompt contained.
    """

    def __init__(self, *args: object, omits: list[tuple[str, str]] | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.omits = list(omits or [])

    def render(self, renderer):  # noqa: ANN001, ANN201
        if not self.omits:
            return renderer
        return _OmittingRenderer(renderer, self.omits)


class _OmittingRenderer:
    """A renderer that removes one required block from one role's packet.

    The role matters: the implementer and reviewer packets are different documents, so a field
    that is required of the reviewer (the candidate commit, the check results) is legitimately
    absent from the implementer's. Removing it from both would fail the run for a reason that has
    nothing to do with the field under test.
    """

    def __init__(self, inner, omits: list[tuple[str, str]]) -> None:  # noqa: ANN001
        self._inner = inner
        self._omits = dict(omits)

    def __call__(self, **kwargs: object):
        packet = self._inner(**kwargs)
        omit = self._omits.get(packet.role)
        if omit is None:
            return packet
        kept: list[str] = []
        skipping = False
        removed = False
        for line in packet.text.splitlines(keepends=True):
            if not removed and omit in line:
                removed = True
                skipping = True
                continue
            if skipping and line.startswith("  "):
                continue
            skipping = False
            kept.append(line)
        assert removed, (
            f"the renderer did not emit {omit!r} in the {packet.role} packet, so this case "
            "would assert nothing"
        )
        text = "".join(kept)
        return RenderedPacket(
            role=packet.role, text=text, digest=packet_digest(text), byte_length=len(text.encode())
        )


def _drive(
    checked: CheckedRun,
    *,
    inputs: dict[str, dict[str, list[str]]],
):
    """A driver for this case: the production one, with its own prompt recording."""
    return checked.driver(inputs=inputs, wire_log=checked.run_dir / "wire")


def _sections_missing(absent: list[str], section: str) -> bool:
    """Did the stub report this field group as not carrying its required fragment?"""
    return section in absent


# --------------------------------------------------------------------------
# 1. the packet renderer itself
# --------------------------------------------------------------------------


def test_implementer_packet_carries_the_task_facts(checked_factory) -> None:
    checked = checked_factory()
    spec = checked.task()
    prompt = render_implementer_packet(
        task_id=spec.task_id,
        task_revision=spec.revision,
        goal=spec.goal,
        acceptance=spec.acceptance,
        scope=spec.scope,
        workspace="/tmp/assigned-worktree",
        spec_digest=spec.spec_digest(),
        deadline_seconds=120,
        writes_allowed=True,
    )

    assert prompt.role == "implementer"
    assert GOAL in prompt.text
    assert "AC-1: empty input returns the agreed empty result (proved by check(s): unit)" in prompt.text
    assert "- src/parser.py" in prompt.text
    assert "/tmp/assigned-worktree" in prompt.text
    assert "no network calls, installs, dependency upgrades or downloads" in prompt.text
    assert "you may change files inside the allowed write paths below" in prompt.text
    assert prompt.digest.startswith("sha256:")
    assert prompt.byte_length == len(prompt.text.encode("utf-8"))


def test_reviewer_packet_carries_the_candidate_and_the_evidence(checked_factory) -> None:
    checked = checked_factory()
    spec = checked.task()
    prompt = render_reviewer_packet(
        task_id=spec.task_id,
        task_revision=spec.revision,
        goal=spec.goal,
        acceptance=spec.acceptance,
        scope=spec.scope,
        workspace="/tmp/frozen-worktree",
        spec_digest=spec.spec_digest(),
        candidate_fingerprint="sha256:deadbeef",
        deadline_seconds=120,
        candidate={
            "fingerprint": "sha256:deadbeef",
            "base_commit": "a" * 40,
            "git_commit": "b" * 40,
            "git_tree": "c" * 40,
            "worktree": "/tmp/frozen-worktree",
            "paths": ["src/parser.py"],
        },
        verification_status="passed",
        verification_detail="1 check(s) passed for candidate sha256:deadbeef",
        check_summaries=[
            {"check_id": "unit", "status": "passed", "exit_code": 0, "command": "python -c pass", "detail": ""}
        ],
        evidence_rows=[
            {"evidence_id": "E-1", "check_id": "unit", "status": "passed", "exit_code": 0}
        ],
    )

    assert prompt.role == "reviewer"
    assert GOAL in prompt.text
    assert "- candidate commit: " + "b" * 40 in prompt.text
    assert "- candidate content fingerprint: sha256:deadbeef" in prompt.text
    assert "- verification status: passed" in prompt.text
    assert "E-1 check=unit status=passed" in prompt.text
    assert OUTPUT_CONTRACT_FRAGMENT in prompt.text
    assert "do not edit, create or delete any file" in prompt.text
    assert "Style, naming and formatting preferences are not blocking findings." in prompt.text
    # The reviewer is never handed the implementer's own account of its work.
    assert "candidate=None" not in prompt.text


# --------------------------------------------------------------------------
# 2. end to end: the prompt the Harness actually received
# --------------------------------------------------------------------------


def test_role_packets_reach_the_harness_and_the_reviewer_votes_on_them(checked_factory) -> None:
    """The positive case: both invocations receive a complete packet, so the run is accepted."""
    checked = checked_factory()
    driver = checked.driver(inputs=role_expectations(), wire_log=checked.run_dir / "wire")
    outcome, review_evidence, notes = checked.run(driver)

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 1
    assert outcome.receipt is not None
    assert outcome.receipt.review.status == "accepted"
    assert len(review_evidence) == 1 and review_evidence[0]["status"] == EvidenceStatus.PASSED.value
    assert checked.reviewer_check()["absent"] == []

    implementer_prompt = checked.implementer_prompt()
    reviewer_prompt = checked.reviewer_prompt()
    for prompt in (implementer_prompt, reviewer_prompt):
        assert GOAL in prompt
        assert "- AC-1: empty input returns the agreed empty result" in prompt
        assert "- src/parser.py" in prompt, "the write scope must reach both roles"
    assert "- candidate commit:" in reviewer_prompt
    assert OUTPUT_CONTRACT_FRAGMENT in reviewer_prompt

    # The controller recorded what it sent, and the transport reported the same digest.
    assert notes, "the run must record what it sent"
    packet_notes = [note for note in notes if "role_input_packet" in note]
    assert any("role=implementer" in note for note in packet_notes)
    assert any("role=reviewer" in note for note in packet_notes)
    digest_notes = [note for note in notes if "prompt_digest" in note]
    assert not any("MISMATCH" in note for note in digest_notes), digest_notes

    # No task text, criterion or credential is ever placed in a command line.
    argv = driver._client_argv(checked.run_dir, checked.repo, 60)
    assert GOAL not in " ".join(argv)
    assert all("AC-1" not in part for part in argv)


@pytest.mark.parametrize("section", sorted(REVIEWER_FRAGMENTS))
def test_dropping_one_reviewer_field_from_the_packet_makes_the_run_block(
    checked_factory, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    """Every reviewer field is load-bearing: the agent notices its absence and refuses.

    The agent's expectations stay complete here; what changes is the packet. One field block is
    removed in transit, exactly as a renderer that forgot the field would leave it, and the
    reviewer must answer ``changes_requested`` naming that field. A controller that sent an
    incomplete reviewer packet therefore cannot reach an accepted run.

    The implementer runs in its permissive mode for these cases: this test is about the reviewer
    packet, and the implementer's own refusal is covered beside it.
    """
    monkeypatch.setenv("STUB_IMPLEMENTER_MODE", "skip")
    fragment = REVIEWER_FRAGMENTS[section]
    checked = checked_factory()
    driver = _drive(checked, inputs=role_expectations())
    outcome, review_evidence, _notes = checked.run(driver, omits=[("reviewer", FIELD_LINES[section])])

    assert checked.reviewer_prompt(omit=fragment)
    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED, outcome.block_reason
    assert outcome.receipt is None
    assert len(review_evidence) == 1
    assert review_evidence[0]["status"] == EvidenceStatus.FAILED.value
    assert _sections_missing(checked.reviewer_check()["absent"], section), checked.reviewer_check()


def test_the_agent_refuses_a_packet_that_omits_a_required_field(tmp_path: Path) -> None:
    """The check behind every case above, exercised on its own.

    A stub that always answered the same way would make the cases above assert nothing, so this
    test drives the agent directly: a complete packet is refused for the field the run's renderer
    left out, and the refusal names that field. It is the negative control for the *instrument*,
    independent of the controller.
    """
    prompt = render_implementer_packet(
        task_id="T-1",
        task_revision=1,
        goal=GOAL,
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="empty input returns None", check_ids=["unit"])
        ],
        scope=Scope(write_allow=["src/parser.py"]),
        workspace=str(tmp_path),
        spec_digest="sha256:test",
        deadline_seconds=60,
        writes_allowed=True,
    )
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps({"implementer": {"scope": ["- src/parser.py"]}}), encoding="utf-8"
    )
    scratch = tmp_path / "scratch"
    environment = dict(os.environ)
    environment["STUB_SCRATCH_DIR"] = str(scratch)
    environment["CHECKING_AGENT_PROMPT_DIR"] = str(tmp_path / "wire")

    def run_agent(packet_text: str) -> None:
        """Feed one packet to the agent and fail loudly if it did not answer."""
        message = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {
                    "sessionId": "s-1",
                    "prompt": [{"type": "text", "text": packet_text}],
                },
            }
        )
        completed = subprocess.run(  # noqa: S603 - fixed argv, test fixture only
            [sys.executable, "-u", str(CHECKING_AGENT), "--inputs", str(inputs)],
            input=message,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=str(tmp_path),
            timeout=60,
            check=True,
        )
        assert '"stopReason":"end_turn"' in completed.stdout, completed.stdout

    def refusal_record() -> dict:
        path = scratch / "implementer-refused.json"
        assert path.is_file(), "the agent did not record a refusal"
        return json.loads(path.read_text(encoding="utf-8"))

    # A packet with the required field: the agent proceeds, so no refusal record is written.
    run_agent(prompt.text)
    assert not (scratch / "implementer-refused.json").exists()

    # The same packet without the field: the agent refuses and names it.
    without_scope = "\n".join(
        line for line in prompt.text.splitlines() if "- src/parser.py" not in line
    )
    run_agent(without_scope)

    # The instrument must be able to see the difference, or the cases above prove nothing.
    assert "- src/parser.py" in prompt.text and "- src/parser.py" not in without_scope
    assert refusal_record()["absent"] == ["scope"]


def test_a_packet_without_the_output_contract_cannot_be_accepted(
    checked_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the verdict contract the reviewer is asked for a shape it was never told."""
    monkeypatch.setenv("STUB_IMPLEMENTER_MODE", "skip")
    checked = checked_factory()
    driver = _drive(checked, inputs=role_expectations())

    outcome, review_evidence, _notes = checked.run(driver, omits=[("reviewer", FIELD_LINES["output_contract"])])

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED, outcome.block_reason
    assert review_evidence[0]["status"] == EvidenceStatus.FAILED.value
    checked.reviewer_prompt(omit=OUTPUT_CONTRACT_FRAGMENT)
    assert _sections_missing(checked.reviewer_check()["absent"], "output_contract"), (
        checked.reviewer_check()
    )


# --------------------------------------------------------------------------
# 2b. the same wire, carried by the real pinned acpx client
# --------------------------------------------------------------------------


def test_real_acpx_carries_a_complete_packet_to_the_input_sensitive_agent(
    checked_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed Node client carries both packets, and the agent checks their contents.

    This is the strongest claim available without a model: the real client, the real driver, the
    real controller, an input-sensitive ACP agent - and the assertions are on the values the
    agent received (acceptance criterion, write scope, candidate commit and fingerprint, check
    result), not on the presence of a fixed heading.
    """
    _require_real_client()
    checked = checked_factory(real_client=True)
    driver = _drive(checked, inputs=role_expectations())

    outcome, review_evidence, notes = checked.run(driver)

    assert outcome.task_state is TaskState.ACCEPTED, outcome.block_reason
    assert outcome.implementer_invocations == 1 and outcome.reviewer_invocations == 1
    assert outcome.receipt is not None
    assert outcome.receipt.review.status == "accepted"
    assert review_evidence[0]["status"] == EvidenceStatus.PASSED.value
    assert checked.reviewer_check()["absent"] == []

    # The values the agent actually received, read from its own record of the prompt.
    prompt = checked.reviewer_prompt()
    assert GOAL in prompt
    assert "- AC-1: empty input returns the agreed empty result (proved by check(s): unit)" in prompt
    assert "- src/parser.py" in prompt
    candidate_commit = outcome.receipt.candidate.git_commit
    candidate_fingerprint = outcome.receipt.candidate.fingerprint
    assert candidate_commit and f"- candidate commit: {candidate_commit}" in prompt
    assert f"- candidate content fingerprint: {candidate_fingerprint}" in prompt
    assert "- verification status: passed" in prompt

    # Every check result the run recorded is in the prompt the reviewer received.
    evidence_rows = [dict(row) for row in review_evidence]
    assert evidence_rows, "the review verdict must be recorded as evidence"
    for check_id in ("unit",):
        assert f"{check_id}: passed exit=0" in prompt, prompt

    assert any("role=implementer" in note for note in notes if "role_input_packet" in note)
    assert any("role=reviewer" in note for note in notes if "role_input_packet" in note)

    # No task text or criterion is ever placed in a command line, on this client either.
    argv = checked.client_argv(driver)
    assert str(INSTALLED_ACPX) in argv and argv[-2:] == ["-f", "-"]
    assert GOAL not in " ".join(argv)
    assert all("AC-1" not in part for part in argv)


def test_real_acpx_run_blocks_when_a_required_reviewer_field_never_arrives(
    checked_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative case on the real client: one missing reviewer field and the run cannot pass.

    Same client, same agent, same controller as the case above; only one field block is missing
    from the packet. That is what makes the positive case above evidence rather than decoration.
    """
    _require_real_client()
    monkeypatch.setenv("STUB_IMPLEMENTER_MODE", "skip")
    checked = checked_factory(real_client=True)
    driver = _drive(checked, inputs=role_expectations())

    outcome, review_evidence, _notes = checked.run(
        driver, omits=[("reviewer", FIELD_LINES["candidate"])]
    )

    assert outcome.task_state is TaskState.BLOCKED
    assert outcome.block_code is RefusalCode.REVIEW_REJECTED, outcome.block_reason
    assert outcome.receipt is None
    assert review_evidence[0]["status"] == EvidenceStatus.FAILED.value
    assert _sections_missing(checked.reviewer_check()["absent"], "candidate"), (
        checked.reviewer_check()
    )
    assert REVIEWER_FRAGMENTS["candidate"] not in checked.reviewer_prompt()


def test_a_long_artifact_path_reaches_the_reviewer_intact(checked_factory) -> None:
    """The reported defect: a 752-character artifact path was cut by the detail-text limit.

    The references are rendered as their own fields now, so their length is limited by the packet
    bound alone - a path is either present in full or the packet is refused, never halved.
    """
    checked = checked_factory()
    spec = checked.task()
    long_path = (
        "C:/Users/16097/AppData/Local/Temp/"
        + "a-very-long-run-directory-name/" * 20
        + "artifact.json"
    )
    assert len(long_path) > 600, "the case must exceed the prose limit on purpose"
    summary = {
        "check_id": "unit",
        "status": "passed",
        "exit_code": 0,
        "reason": "completed",
        "artifact": long_path,
        "stdout": {
            "retained_bytes": "1234",
            "total_bytes": "1234",
            "truncated": "False",
            "digest": "sha256:deadbeef",
        },
        "stderr": {
            "retained_bytes": "0",
            "total_bytes": "0",
            "truncated": "False",
            "digest": "sha256:" + "0" * 64,
        },
        "command": "python -m pytest -q " + "x" * 800,
        "detail": "check unit: exit=0 elapsed=1.0s " + "y" * 800,
    }

    prompt = render_reviewer_packet(
        task_id=spec.task_id,
        task_revision=spec.revision,
        goal=spec.goal,
        acceptance=spec.acceptance,
        scope=spec.scope,
        workspace="/tmp/frozen-worktree",
        spec_digest=spec.spec_digest(),
        candidate_fingerprint="sha256:deadbeef",
        deadline_seconds=120,
        verification_status="passed",
        check_summaries=[summary],
        evidence_rows=[
            {"evidence_id": "E-1", "check_id": "unit", "status": "passed", "artifact": long_path}
        ],
    )

    assert f"artifact={long_path}" in prompt.text, "the full path must survive rendering"
    assert prompt.text.count(long_path) >= 2, "both the check line and the evidence row name it"
    assert "sha256:deadbeef" in prompt.text
    assert "truncated=False" in prompt.text
    assert "command truncated for display" in prompt.text
    assert "detail truncated; see the artifact reference" in prompt.text
    assert prompt.byte_length <= 32 * 1024


def test_a_packet_whose_references_do_not_fit_is_refused_not_clipped(checked_factory) -> None:
    """If the references alone exceed the packet bound, nothing is silently shortened."""
    from hflow.packet import MAX_PACKET_BYTES, PacketTooLargeError

    checked = checked_factory()
    spec = checked.task()
    huge_path = "C:/tmp/" + "z" * MAX_PACKET_BYTES + "/artifact.json"

    with pytest.raises(PacketTooLargeError) as excinfo:
        render_reviewer_packet(
            task_id=spec.task_id,
            task_revision=spec.revision,
            goal=spec.goal,
            acceptance=spec.acceptance,
            scope=spec.scope,
            workspace="/tmp/ws",
            spec_digest=spec.spec_digest(),
            candidate_fingerprint="sha256:deadbeef",
            deadline_seconds=120,
            verification_status="passed",
            check_summaries=[{"check_id": "unit", "status": "passed", "artifact": huge_path}],
        )

    assert "Nothing was truncated" in str(excinfo.value)
    assert "above the" in str(excinfo.value)
    assert huge_path not in str(excinfo.value), "the refusal must not echo a giant path"


# --------------------------------------------------------------------------
# 3. the dispatched prompt is exactly the rendered packet
# --------------------------------------------------------------------------


def test_effective_prompt_is_the_packet_and_never_a_rebuilt_goal() -> None:
    """A driver transports the packet; it does not re-render or extend it."""
    request = InvocationRequest(
        invocation_id="I-1",
        attempt_id="A-1",
        run_id="R-1",
        role="implementer",
        task_id="T-1",
        task_revision=1,
        goal="the bare goal",
        acceptance=[],
        write_allow=[],
        write_deny=[],
        workspace="C:/ws",
        deadline_seconds=60,
        spec_digest="sha256:test",
        packet="[HFlow implementer task]\n\n## Task\n- goal: the bare goal\n",
    )
    assert effective_prompt(request) == request.packet

    # A direct caller that passes only a goal still gets a traceable prompt: the envelope names
    # the digest of the text that was sent.
    bare = request.model_copy(update={"packet": ""})
    sent = effective_prompt(bare)
    assert sent.startswith(PROMPT_ENVELOPE + ": ")
    assert sent.endswith("the bare goal")
    assert os.linesep not in sent.split("\n", 1)[0]


def test_the_output_contract_note_is_the_one_the_parser_accepts() -> None:
    """The instructions and the decoder must describe the same shape."""
    from hflow.review import decode_review

    example = json.dumps({"verdict": "accepted", "findings": []})
    assert "```json" in OUTPUT_CONTRACT_NOTE
    assert decode_review(f"```json\n{example}\n```").verdict == "accepted"
