"""The saved live evidence, replayed offline: verdict + isolated acceptance, nothing else.

These tests only run where the recorded attempt's bytes are present (``.probe/`` is local,
untracked material). They are deliberately not skipped *silently*: the skip reason says which
path is missing, so "we verified the saved verdict" is never claimed without the bytes.

What is asserted here is the honest version of the claim:

* the saved reviewer answer decodes to the canonical verdict with its findings intact;
* the original run's history is untouched - still ``BLOCKED``/``review_rejected``, still no
  receipt, and ``AUTH-m2-live-2`` still consumed 2/2;
* the replay launches **no** process at all, so no model call, credential read or live resume
  can hide inside it;
* the frozen candidate is unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / ".probe" / "m2-live"
STORE = PROBE / "attempt-2-data" / "hflow.sqlite"
PROJECT_DIR = PROBE / "attempt-2"
RUN_ID = "R-gkb3ld97x8"
CANDIDATE_COMMIT = "499ece7043fe3267b4ff89f9a5b5bc1d70c42481"
CANDIDATE_FINGERPRINT = "sha256:5c47b12d12031c4b94f4254c7882615ec7afaecbbef24b17e0483d8b4ac1d405"

TOOL = REPO_ROOT / "tools" / "m2_live" / "replay_review.py"


def _require_saved_evidence() -> None:
    if not STORE.is_file():
        pytest.skip(f"the recorded store is not present at {STORE}")
    if not (PROBE / "attempt-2-data" / "invocations" / "I-vc5pcccfog").is_dir():
        pytest.skip("the recorded reviewer invocation is not present")


def _load_tool():
    spec = importlib.util.spec_from_file_location("hflow_replay_review", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture()
def no_child_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to launch a program during the replay fails loudly.

    A model call, a credential helper, a live resume or a Git write all need a child process.
    Trapping the launch points proves the replay is bytes and decisions, rather than asserting
    that it is.
    """

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the replay tried to launch a process: {args!r} {kwargs!r}")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, deny)
    monkeypatch.setattr("os.system", deny)
    monkeypatch.setattr("os.popen", deny)
    monkeypatch.setattr("os.spawnv", deny, raising=False)
    monkeypatch.setattr("os.posix_spawn", deny, raising=False)


def test_saved_review_replays_to_a_validated_verdict(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    diagnostic = tool.replay(RUN_ID, store_path=STORE, project_dir=PROJECT_DIR)

    assert diagnostic["blockers"] == []
    review = diagnostic["review"]
    assert review["verdict"] == "accepted"
    assert review["verdict_digest"].startswith("sha256:")
    assert review["answer_sha256"].startswith("sha256:")
    assert len(review["findings"]) == 5
    assert [finding["id"] for finding in review["findings"]] == [
        "AC-1",
        "AC-2",
        "AC-3",
        "EVIDENCE-E-zgamyka8s3",
        "F-1",
    ]
    # Every finding was checked against the candidate the review was recorded for.
    assert {
        finding.get("target") for finding in review["findings"] if finding["id"].startswith("AC-")
    } == {CANDIDATE_FINGERPRINT}
    assert review["terminal_answers_prompt"] is True
    assert review["reviewer_named_the_recorded_candidate"] is True
    assert review["answer_chars"] > 1000, "the whole final message, not a fragment"


def test_saved_review_replays_through_the_acceptance_predicates(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    diagnostic = tool.replay(RUN_ID, store_path=STORE, project_dir=PROJECT_DIR)

    acceptance = diagnostic["acceptance"]
    assert acceptance["accepted"] is True
    assert acceptance["candidate_unchanged"] is True
    assert acceptance["verification_evidence_current"] is True
    assert acceptance["candidate_fingerprint_recorded"] == CANDIDATE_FINGERPRINT
    assert acceptance["candidate_fingerprint_recomputed"] == CANDIDATE_FINGERPRINT
    assert acceptance["isolated_receipt_written"] is True
    assert "test artifact" in acceptance["receipt_artifact"]


def test_the_replay_leaves_the_original_record_untouched(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()
    before = _sha256(STORE)

    diagnostic = tool.replay(RUN_ID, store_path=STORE, project_dir=PROJECT_DIR)

    assert _sha256(STORE) == before
    assert diagnostic["store_mutated"] is False
    assert diagnostic["store_sha256"] == diagnostic["store_sha256_after"]
    recorded = diagnostic["recorded"]
    assert recorded["task_state"] == "BLOCKED"
    assert recorded["block_code"] == "review_rejected"
    assert recorded["receipt_present"] is False
    assert recorded["controller_build"] == "hflow/0.0.1+3dbfeae"
    assert recorded["attempt_id"] == "A-7f2pbp4teu"
    assert recorded["implementer_invocation"] == "I-xf3sez1lho"
    assert recorded["reviewer_invocation"] == "I-vc5pcccfog"
    # The saved payload keeps the proof of the defect: the review never arrived.
    assert diagnostic["review"]["stored_result_review"] is None
    assert diagnostic["review"]["stored_review_reason"] is None


def test_the_authorization_still_reads_two_of_two(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    diagnostic = tool.replay(RUN_ID, store_path=STORE, project_dir=PROJECT_DIR)

    authorizations = {row["authorization_id"]: row for row in diagnostic["authorizations"]}
    record = authorizations["AUTH-m2-live-2"]
    assert record["used_top_level_submissions"] == 2
    assert record["max_top_level_submissions"] == 2
    assert record["provided_by"] == "user"


def test_the_saved_candidate_is_still_what_was_frozen(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    diagnostic = tool.replay(RUN_ID, store_path=STORE, project_dir=PROJECT_DIR)

    candidate = diagnostic["candidate"]
    assert candidate["fingerprint_matches"] is True
    assert candidate["candidate_ref_sha"] == CANDIDATE_COMMIT
    assert candidate["worktree_head"] == CANDIDATE_COMMIT
    assert candidate["candidate_ref_matches_head"] is True
    assert candidate["scoped_files"] == ["src/reportkit/__init__.py"]


def test_a_missing_run_is_reported_not_invented(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    with pytest.raises(tool.ReplayError):
        tool.replay("R-does-not-exist", store_path=STORE, project_dir=PROJECT_DIR)


def test_the_cli_writes_its_diagnostic_and_exits_zero(tmp_path: Path) -> None:
    """The command line form used for the handoff: one derived diagnostic, no side effects."""
    _require_saved_evidence()
    out_path = tmp_path / "diagnostic.json"
    completed = subprocess.run(  # noqa: S603 - fixed local tool invocation
        [
            sys.executable,
            str(TOOL),
            RUN_ID,
            "--store",
            str(STORE),
            "--project-dir",
            str(PROJECT_DIR),
            "--out",
            str(out_path),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    diagnostic = json.loads(out_path.read_text(encoding="utf-8"))
    assert diagnostic["review"]["verdict"] == "accepted"
    assert diagnostic["acceptance"]["accepted"] is True
    assert diagnostic["model_calls"] == 0
    assert diagnostic["submissions"] == 0
