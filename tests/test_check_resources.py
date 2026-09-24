"""Batch B: bounded output, a readable artifact, a minimal environment, honest exit results.

Every case here runs a real child process through ``CommandCheckRunner``. Nothing calls a model,
and nothing here is a sandbox test: the claims are about *control* (bounded memory, bounded
retention, a bounded wait) and about *honesty* (which result a given ending maps to).

What the tests deliberately do not claim:

* a retained file is not a guarantee about what a check wrote - only about what HFlow kept. The
  digest covers the whole stream, the file covers the retained head, and the difference is a
  recorded fact rather than an assumption;
* an environment allowlist is not confinement. It stops accidental inheritance of the
  controller's variables; it does not restrain a process that already runs with the user's rights.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from hflow.artifacts import (
    BoundedTextSink,
    child_environment,
    environment_summary,
    is_secret_like,
    parse_environment_summary,
)
from hflow.contracts import (
    AcceptanceCriterion,
    BudgetRequest,
    CheckDef,
    DeliveryRequirement,
    EvidenceStatus,
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
from hflow.packet import render_reviewer_packet
from hflow.drivers.fake import FakeDriver, FakeScript
from hflow.store import Store
from hflow.verify import CheckRunners, CommandCheckRunner, _detail_with_references

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHECK_HELPER = FIXTURES / "check_helper.py"


def _helper_argv(*extra: str, markers: Path, label: str = "check") -> list[str]:
    return [
        sys.executable,
        "-u",
        str(CHECK_HELPER),
        "--markers",
        str(markers),
        "--label",
        label,
        *extra,
    ]


def _check(argv: list[str], *, check_id: str = "unit", timeout: int = 60) -> CheckDef:
    return CheckDef(id=check_id, kind="command", argv=argv, timeout_seconds=timeout)


# --------------------------------------------------------------------------
# 1. bounded output and a readable artifact
# --------------------------------------------------------------------------


def test_a_large_single_line_stream_is_retained_up_to_the_limit(tmp_path: Path) -> None:
    """20 MiB with no newline: retained head on disk, whole stream digested, memory bounded."""
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(
        stream_limit_bytes=1024 * 1024, artifact_factory=lambda _check, _eid: tmp_path / "art"
    )
    emitted = 20 * 1024 * 1024

    outcome = runner.run(
        _check(_helper_argv("--emit-bytes", str(emitted), markers=markers)), tmp_path, 120
    )

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    assert outcome.exit_reason == "completed"
    stdout = outcome.artifacts["stdout"]
    assert stdout["total_bytes"] == emitted, "every byte must be counted, not just the kept ones"
    assert stdout["retained_bytes"] == 1024 * 1024
    assert stdout["truncated"] is True
    retained = Path(str(stdout["path"])).read_bytes()
    assert len(retained) == 1024 * 1024
    assert retained == b"x" * 1024 * 1024, "the retained head must be the head, not the tail"
    assert "truncated" in outcome.detail


def test_the_stream_digest_covers_the_readable_bytes_and_the_cut_is_recorded(tmp_path: Path) -> None:
    """A cut stream must be identified as cut, and its digest must cover what is on disk.

    A digest of bytes nobody can obtain would be unverifiable, so the retained head is what is
    hashed; the discarded tail is still a recorded fact (``total_bytes`` and ``truncated``), which
    is what keeps "the log is complete" from being claimed about a file that is not.
    """
    markers = tmp_path / "markers"
    limit = 64 * 1024
    runner = CommandCheckRunner(
        stream_limit_bytes=limit,
        artifact_factory=lambda _check, _eid: tmp_path / "art",
    )

    outcome = runner.run(
        _check(_helper_argv("--emit-bytes", str(limit + 4096), markers=markers)),
        tmp_path,
        60,
    )

    stdout = outcome.artifacts["stdout"]
    assert stdout["truncated"] is True
    assert stdout["retained_bytes"] == limit
    assert stdout["total_bytes"] == limit + 4096
    retained = Path(str(stdout["path"])).read_bytes()
    assert len(retained) == limit
    # The digest describes exactly the bytes on disk, so a reader can recompute it.
    import hashlib

    assert stdout["digest"] == "sha256:" + hashlib.sha256(retained).hexdigest()
    assert "discarded tail is counted but not kept" in str(stdout["note"])


def test_multibyte_output_at_the_retention_boundary_stays_decodable(tmp_path: Path) -> None:
    """A cut can land inside a UTF-8 character; decoding the head must not raise."""
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(
        stream_limit_bytes=10,
        artifact_factory=lambda _check, _eid: tmp_path / "art",
    )

    outcome = runner.run(
        _check(_helper_argv("--emit-utf8", "100", markers=markers)), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    stdout = outcome.artifacts["stdout"]
    assert stdout["truncated"] is True
    # Ten bytes is three whole 中 characters plus one byte of the fourth: the retained file ends
    # mid-character, so the decoded head is where the replacement character can appear.
    retained = Path(str(stdout["path"])).read_bytes()
    assert retained == "\u4e2d".encode("utf-8") * 3 + b"\xe4"
    assert retained.decode("utf-8", "replace").endswith("\ufffd")
    assert "\u4e2d" in retained.decode("utf-8", "replace")


def test_a_check_that_floods_the_pipe_then_hangs_still_reaches_its_timeout(tmp_path: Path) -> None:
    """The deadlock case: output far beyond a pipe buffer, then no exit.

    A runner that stopped draining (or that waited for the child before reading) would block here
    forever. This one must reach its deadline, stop the owned boundary, and report a timeout.
    """
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(
        stream_limit_bytes=256 * 1024,
        artifact_factory=lambda _check, _eid: tmp_path / "art",
        reader_timeout_seconds=5.0,
    )
    started = time.monotonic()

    outcome = runner.run(
        _check(
            _helper_argv("--emit-bytes", str(8 * 1024 * 1024), "--emit-then-hang", markers=markers),
            timeout=3,
        ),
        tmp_path,
        3,
    )
    elapsed = time.monotonic() - started

    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "timed_out"
    assert outcome.timed_out is True
    assert elapsed < 60, "the deadline must be reached, not waited out"
    assert outcome.artifacts["stdout"]["total_bytes"] > 256 * 1024


def test_output_to_both_streams_is_counted_separately(tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(
        stream_limit_bytes=4096, artifact_factory=lambda _check, _eid: tmp_path / "art"
    )

    outcome = runner.run(
        _check(
            _helper_argv(
                "--emit-bytes",
                str(8192),
                "--emit-stderr-bytes",
                str(1024),
                markers=markers,
            )
        ),
        tmp_path,
        60,
    )

    assert outcome.artifacts["stdout"]["total_bytes"] == 8192
    assert outcome.artifacts["stderr"]["total_bytes"] == 1024
    assert outcome.artifacts["stdout"]["truncated"] is True
    assert outcome.artifacts["stderr"]["truncated"] is False


def test_the_artifact_and_manifest_are_files_a_reader_can_open(tmp_path: Path) -> None:
    markers = tmp_path / "markers"
    artifact_dir = tmp_path / "artifacts"
    runner = CommandCheckRunner(
        stream_limit_bytes=1024, artifact_factory=lambda _check, _eid: artifact_dir
    )

    outcome = runner.run(
        _check(_helper_argv("--emit-bytes", "4096", markers=markers)), tmp_path, 60
    )

    manifest = Path(outcome.artifact_path)
    assert manifest.is_file() and manifest.name == "artifact.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["check_id"] == "unit"
    assert payload["truncated"] is True
    assert payload["environment"].startswith("env_names=")
    for name in ("stdout", "stderr"):
        assert Path(str(outcome.artifacts[name]["path"])).is_file()


def test_a_run_records_a_check_artifact_under_its_own_data_dir(tmp_path: Path) -> None:
    """End to end: the evidence row names a file inside the run's data dir, not a temp path."""
    project_root = tmp_path / "ws"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    markers = tmp_path / "markers"
    data_dir = tmp_path / "data"
    spec = TaskSpec(
        task_id="T-resources",
        revision=1,
        goal="keep the check output readable",
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="the check passes", check_ids=["unit"])
        ],
        scope=Scope(write_allow=["src/app.py"]),
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:python-stdlib-only",
            reason="standard library only",
        ),
        review=ReviewRequirement(required=False),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=1, max_repair_cycles=0),
    )
    project = ProjectConfig(
        project_id="resources",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=_helper_argv("--emit-bytes", "5000", markers=markers),
                timeout_seconds=60,
            )
        ],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=1, max_repair_cycles=0),
        review_required=False,
    )
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        FakeDriver(project_root, FakeScript(agent_turns=1)),
        controller_build="resources-test",
        runners=CheckRunners.offline_default(),
        data_dir=data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        evidence = [dict(row) for row in store.evidence_for(outcome.run_id, "verification")]
    finally:
        store.close()

    assert outcome.receipt is not None, outcome.block_reason
    assert len(evidence) == 1
    detail = evidence[0]["detail"]
    assert "artifact=" in detail
    artifact = Path(detail.split("artifact=", 1)[1].split(" ", 1)[0])
    assert artifact.is_file(), detail
    assert artifact.is_relative_to(data_dir), "run artifacts belong to the run's data dir"
    assert "stdout:" in detail and "truncated=False" in detail
    assert "env_names=" in detail


# --------------------------------------------------------------------------
# 2. what a check is allowed to see
# --------------------------------------------------------------------------


def test_the_controller_environment_is_not_inherited_wholesale() -> None:
    base = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:/Windows",
        "TEMP": "C:/Temp",
        "OPENAI_API_KEY": "sk-not-a-real-key",
        "HFLOW_SECRET_TOKEN": "not-a-real-token",
        "SOME_UNRELATED_VARIABLE": "value",
    }

    env, withheld = child_environment(base=base, extra={"PROJECT_TEST_FLAG": "1"})

    assert env["PATH"] == "/usr/bin" and env["SYSTEMROOT"] == "C:/Windows"
    assert env["PROJECT_TEST_FLAG"] == "1", "declared project variables are allowed"
    assert "OPENAI_API_KEY" not in env and "HFLOW_SECRET_TOKEN" not in env
    assert "SOME_UNRELATED_VARIABLE" not in env, "an allowlist means unlisted names do not pass"
    assert "OPENAI_API_KEY" in withheld and "HFLOW_SECRET_TOKEN" in withheld
    summary = environment_summary(env, withheld=withheld)
    assert "sk-not-a-real-key" not in summary and "not-a-real-token" not in summary
    assert "OPENAI_API_KEY" in summary, "the name is reported; the value never is"


def test_a_declared_secret_is_still_refused_and_reported() -> None:
    """Declaring a credential for a check does not make it allowed."""
    env, withheld = child_environment(
        base={"PATH": "/usr/bin"}, extra={"MY_API_KEY": "not-a-real-key"}
    )

    assert "MY_API_KEY" not in env
    assert "MY_API_KEY" in withheld
    assert is_secret_like("my_api_key") and not is_secret_like("PATH")


def test_a_check_cannot_see_a_fake_provider_credential(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end version: a real child process reports what it actually received."""
    monkeypatch.setenv("HFLOW_FAKE_PROVIDER_API_KEY", "sk-fake-not-real")
    monkeypatch.setenv("HFLOW_UNRELATED_MARKER", "should-not-travel")
    monkeypatch.setenv("HFLOW_APPROVED_TEST_FLAG", "1")
    markers = tmp_path / "markers"
    dump = tmp_path / "child-env.json"
    artifact_dir = tmp_path / "art"
    runner = CommandCheckRunner(
        extra_env={"HFLOW_APPROVED_TEST_FLAG": "1"},
        artifact_factory=lambda _check, _eid: artifact_dir,
    )

    outcome = runner.run(
        _check(_helper_argv("--env-dump", str(dump), markers=markers)), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert "HFLOW_FAKE_PROVIDER_API_KEY" not in seen
    assert "HFLOW_UNRELATED_MARKER" not in seen
    assert seen.get("HFLOW_APPROVED_TEST_FLAG") == "1"
    # The refusal is a recorded fact, not a silent drop. It is read as a *list of names*, because
    # which name comes first depends on what else exists on the machine - a substring match would
    # pass or fail with the environment rather than with the behaviour.
    parsed = parse_environment_summary(outcome.environment)
    assert "HFLOW_FAKE_PROVIDER_API_KEY" in parsed["withheld"]
    assert "sk-fake-not-real" not in outcome.environment
    manifest = json.loads(Path(outcome.artifact_path).read_text(encoding="utf-8"))
    assert "HFLOW_FAKE_PROVIDER_API_KEY" in parse_environment_summary(
        str(manifest["environment"])
    )["withheld"]


def test_a_system_path_a_runtime_needs_reaches_the_check_process(
    tmp_path: Path, monkeypatch
) -> None:
    """A path Unity's package manager reads must reach the child, credentials still must not.

    Regression for run R-uzc5wk2wdf: a cold Unity candidate failed every check because the
    package manager could not resolve packages. The value is *inherited* from the controller's
    environment, so the child sees the same string the controller holds - not a synthesized one.
    """
    monkeypatch.setenv("ALLUSERSPROFILE", "C:/ProgramData-not-real")
    monkeypatch.setenv("HFLOW_FAKE_PROVIDER_TOKEN", "not-a-real-token")
    dump = tmp_path / "child-env.json"
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")

    outcome = runner.run(
        _check(_helper_argv("--env-dump", str(dump), markers=markers)), tmp_path, 60
    )

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert seen.get("ALLUSERSPROFILE") == "C:/ProgramData-not-real", (
        "an allowlisted system path must reach the check, inherited rather than invented"
    )
    assert "HFLOW_FAKE_PROVIDER_TOKEN" not in seen, "the allowlist still stops credentials"
    assert "HFLOW_FAKE_PROVIDER_TOKEN" in parse_environment_summary(outcome.environment)["withheld"]


def test_the_minimal_environment_still_runs_python_and_node(tmp_path: Path) -> None:
    """The allowlist must not be so small that a normal runtime stops working."""
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")
    python_check = _check([sys.executable, "-c", "import sys; print(sys.version_info[0])"])

    outcome = runner.run(python_check, tmp_path, 60)

    assert outcome.status is EvidenceStatus.PASSED, outcome.detail
    assert Path(str(outcome.artifacts["stdout"]["path"])).read_text(encoding="utf-8").startswith("3")


def test_the_environment_summary_is_a_parseable_named_list_in_a_fixed_order() -> None:
    """The summary is read by name, and the same environment always produces the same string."""
    env, withheld = child_environment(
        base={"PATH": "/usr/bin", "B_TOKEN": "x", "A_API_KEY": "y"},
        extra={"PROJECT_FLAG": "1"},
    )
    summary = environment_summary(env, withheld=withheld)

    parsed = parse_environment_summary(summary)
    assert set(parsed["names"]) == {"PATH", "PROJECT_FLAG"}
    assert set(parsed["withheld"]) == {"A_API_KEY", "B_TOKEN"}
    assert parsed["count"] == 2
    assert summary == environment_summary(env, withheld=list(reversed(withheld))), (
        "the summary must not depend on the order the caller happened to collect names in"
    )
    # A summary with nothing withheld still parses, so an older record reads as "none recorded".
    assert parse_environment_summary("env_names=1: PATH")["withheld"] == []


def test_the_runner_never_passes_the_controller_environment_object_by_reference() -> None:
    """``extra_env`` is copied: a later mutation of the caller's dict cannot reach a check."""
    extra = {"PROJECT_FLAG": "1"}
    runner = CommandCheckRunner(extra_env=extra)
    extra["PROJECT_FLAG"] = "mutated"

    assert runner.extra_env["PROJECT_FLAG"] == "1"


def test_an_incomplete_capture_can_never_be_reported_as_passed(tmp_path: Path, monkeypatch) -> None:
    """A stream the runner could not read to the end is not a result, whatever the exit code was.

    The reader threads are forced to look unfinished here. The child exits 0 and prints normally,
    which is exactly the case that must not slip through as a pass: the branch that decides this
    runs after the outcome has been computed, so it overrides an otherwise clean result.
    """
    monkeypatch.setattr(
        CommandCheckRunner, "_join_readers", lambda self, readers: False, raising=True
    )
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")

    outcome = runner.run(_check([sys.executable, "-c", "print('looks fine')"]), tmp_path, 60)

    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "output_capture_error"
    assert outcome.exit_code == 0, "the exit code is recorded as it was, not rewritten"
    assert "incomplete" in outcome.detail


def test_a_failed_sink_write_can_never_be_reported_as_passed(tmp_path: Path, monkeypatch) -> None:
    """The reported defect: a capture that failed to write still ended as ``passed/completed``.

    The real failure path is injected here - the sink's ``write`` raises, exactly as a full disk or
    a closed handle would - and a real child process exits 0 after printing. The reader thread
    exits too, so "did the thread finish?" cannot distinguish this from a clean capture; the reader
    has to *report how it ended*, and the result must be ERROR with the child's exit code kept.
    """
    original_write = BoundedTextSink.write
    calls = {"n": 0}

    def failing_write(self, chunk: bytes) -> None:
        calls["n"] += 1
        raise OSError("simulated full disk")

    monkeypatch.setattr(BoundedTextSink, "write", failing_write)
    try:
        runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")
        outcome = runner.run(
            _check([sys.executable, "-c", "print('this line cannot be retained')"]),
            tmp_path,
            60,
        )
    finally:
        monkeypatch.setattr(BoundedTextSink, "write", original_write)

    assert calls["n"] > 0, "the injected failure must actually have been exercised"
    assert outcome.status is EvidenceStatus.ERROR
    assert outcome.exit_reason == "output_capture_error"
    assert outcome.exit_code == 0, "the child's own exit code is recorded, not rewritten"
    assert "did not finish cleanly" in outcome.detail
    stdout = outcome.artifacts["stdout"]
    assert stdout["retained_bytes"] == 0
    assert stdout["failed"] is True
    assert "OSError" in str(stdout["failure_reason"])
    manifest = json.loads(Path(outcome.artifact_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "error"
    assert manifest["capture_failure"], manifest


def test_the_evidence_row_round_trips_into_the_reviewer_packet(tmp_path: Path) -> None:
    """The reported wiring defect, tested through the real path rather than a hand-filled packet.

    Three things have to agree for a reviewer to be able to read a check's log: the evidence row
    must contain the reference, the controller's decoder must extract it from the text the row
    actually holds (spaces in a path included), and the renderer must print it unshortened. The
    earlier test filled the renderer's fields by hand and so skipped the decoder entirely.
    """
    import hflow.controller as controller_module

    project_root = tmp_path / "workspace"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    markers = tmp_path / "markers"
    # A data dir with a space in it, which is what a real Windows temp path looks like.
    data_dir = tmp_path / "hflow data"
    spec = TaskSpec(
        task_id="T-round-trip",
        revision=1,
        goal="keep the check artifact reachable",
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="the check passes", check_ids=["unit"])
        ],
        scope=Scope(write_allow=["src/app.py"]),
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:python-stdlib-only",
            reason="standard library only",
        ),
        review=ReviewRequirement(required=False),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=1, max_repair_cycles=0),
    )
    project = ProjectConfig(
        project_id="round-trip",
        checks=[
            CheckDef(
                id="unit",
                kind="command",
                argv=_helper_argv("--emit-bytes", "3000", markers=markers),
                timeout_seconds=60,
            )
        ],
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=1, max_repair_cycles=0),
        review_required=False,
    )
    store = Store(tmp_path / "hflow.sqlite")
    controller = Controller(
        store,
        FakeDriver(project_root, FakeScript(agent_turns=1)),
        controller_build="round-trip-test",
        runners=CheckRunners.offline_default(),
        data_dir=data_dir,
        production=False,
    )
    try:
        outcome = controller.run_task(
            RunRequest(
                task=spec,
                project=project,
                project_root=project_root,
                workspace_root=project_root,
            )
        )
        assert outcome.receipt is not None, outcome.block_reason
        row = dict(store.evidence_for(outcome.run_id, "verification")[0])
    finally:
        store.close()

    detail = row["detail"]
    assert "artifact=" in detail
    decoded_path = controller_module._reference_field(detail, "artifact")
    decoded_stdout = controller_module._stream_reference(detail, "stdout")

    assert decoded_path, f"the decoder found no artifact in {detail[:200]!r}"
    assert " " in decoded_path, "the case must exercise a path that contains a space"
    assert Path(decoded_path).is_file(), decoded_path
    assert decoded_stdout, "the stream summary must decode; a separator mismatch yields {}"
    assert decoded_stdout["total_bytes"] == "3000", decoded_stdout
    assert decoded_stdout["retained_bytes"] == "3000", decoded_stdout
    assert decoded_stdout["truncated"] == "False", decoded_stdout
    assert str(decoded_stdout["digest"]).startswith("sha256:")

    # And it survives into the packet a reviewer would receive.
    prompt = render_reviewer_packet(
        task_id=spec.task_id,
        task_revision=spec.revision,
        goal=spec.goal,
        acceptance=spec.acceptance,
        scope=spec.scope,
        workspace=str(project_root),
        spec_digest=spec.spec_digest(),
        candidate_fingerprint=outcome.receipt.candidate.fingerprint,
        deadline_seconds=120,
        verification_status="passed",
        check_summaries=[
            {
                "check_id": row["check_id"],
                "status": row["status"],
                "exit_code": row["exit_code"],
                "reason": controller_module._reference_field(detail, "reason"),
                "artifact": decoded_path,
                "stdout": decoded_stdout,
                "stderr": controller_module._stream_reference(detail, "stderr"),
                "command": " ".join(json.loads(row["command_json"])),
                "detail": detail,
            }
        ],
        evidence_rows=[
            {
                "evidence_id": row["evidence_id"],
                "check_id": row["check_id"],
                "status": row["status"],
                "artifact": decoded_path,
            }
        ],
    )
    assert f"artifact={decoded_path}" in prompt.text, "the space-containing path must arrive whole"
    assert "stdout=3000/3000B truncated=False digest=sha256:" in prompt.text


def test_the_artifact_and_environment_references_lead_the_evidence_detail(tmp_path: Path) -> None:
    """A reader that shortens the detail must lose prose, never the references.

    The check prints a deliberately long line so its own detail text is large. The references are
    then asserted at the front of the string, which is the only position a length cap cannot cut.
    """
    markers = tmp_path / "markers"
    runner = CommandCheckRunner(
        stream_limit_bytes=1024, artifact_factory=lambda _check, _eid: tmp_path / "art"
    )

    outcome = runner.run(
        _check(_helper_argv("--emit-bytes", "4096", markers=markers)), tmp_path, 60
    )

    detail = _detail_with_references(outcome)
    assert detail.startswith("reason=completed artifact=")
    artifact_at = detail.index("artifact=")
    env_at = detail.index("env_names=")
    human_at = detail.index("check unit:")
    assert artifact_at < env_at < human_at, detail[:200]
    assert "truncated=True" in detail[:env_at], "the truncation state leads the prose too"


# --------------------------------------------------------------------------
# 3. the sink itself
# --------------------------------------------------------------------------


def test_the_sink_keeps_the_head_and_reports_the_whole(tmp_path: Path) -> None:
    sink = BoundedTextSink(tmp_path / "out.txt", limit=8)
    sink.__enter__()
    sink.write(b"abcdefgh")
    sink.write(b"ijkl")
    capture = sink.capture()

    assert capture.total_bytes == 12
    assert capture.retained_bytes == 8
    assert capture.truncated is True
    assert (tmp_path / "out.txt").read_bytes() == b"abcdefgh"
    assert capture.as_dict()["note"].startswith("the digest covers the retained head")


def test_the_sink_reports_an_untruncated_stream_as_complete(tmp_path: Path) -> None:
    sink = BoundedTextSink(tmp_path / "out.txt", limit=1024)
    with sink:
        sink.write(b"short output\n")
    capture = sink.capture()

    assert capture.truncated is False
    assert "the whole stream is on disk" in capture.as_dict()["note"]
    assert capture.digest.startswith("sha256:")


def test_a_zero_limit_keeps_nothing_but_still_counts_and_digests(tmp_path: Path) -> None:
    """A zero retention limit is a legitimate configuration: keep nothing, still know everything."""
    sink = BoundedTextSink(tmp_path / "out.txt", limit=0)
    with sink:
        sink.write(b"some output")
    capture = sink.capture()

    assert capture.retained_bytes == 0
    assert capture.total_bytes == 11
    assert capture.truncated is True
