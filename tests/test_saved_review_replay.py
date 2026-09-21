"""The saved live evidence, replayed offline, and the record of its later finalization.

These tests only run where the recorded attempt's bytes are present (``.probe/`` is local,
untracked material). They are deliberately not skipped *silently*: the skip reason says which
path is missing, so "we verified the saved verdict" is never claimed without the bytes.

Two distinct things are asserted, and they are kept apart on purpose:

* the recorded reviewer stream still decodes to the canonical verdict with its findings intact,
  judged against a copy of the ledger - the original bytes, not the current state;
* the production ledger, whether or not the local finalization has been applied, never loses
  the original failure, never changes its authorization consumption, and never gains a model
  submission.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
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
ORIGINAL_BUILD = "hflow/0.0.1+3dbfeae"
REVIEW_EVIDENCE_ID = "E-zgamyka8s3"

TOOL = REPO_ROOT / "tools" / "m2_live" / "replay_review.py"


def _require_saved_evidence() -> None:
    if not STORE.is_file():
        pytest.skip(f"the recorded ledger is not present at {STORE}")
    if not (PROBE / "attempt-2-data" / "invocations" / "I-vc5pcccfog").is_dir():
        pytest.skip("the recorded reviewer invocation is not present")


def _load_tool():
    spec = importlib.util.spec_from_file_location("hflow_replay_review", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(sql: str, params: tuple = (), path: Path = STORE) -> list[dict]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


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


@pytest.fixture()
def ledger_copy(tmp_path: Path) -> Path:
    """A copy of the ledger, so the recorded bytes can be re-evaluated after finalization."""
    _require_saved_evidence()
    target = tmp_path / "ledger" / "hflow.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STORE, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(STORE) + suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, Path(str(target) + suffix))
    return target


def test_saved_review_replays_to_a_validated_verdict(
    ledger_copy: Path, no_child_processes: None
) -> None:
    """The recorded reviewer bytes still decode to the canonical verdict, from the raw stream."""
    _require_saved_evidence()
    from hflow.review import AnswerTranscript, decode_review

    stream = PROBE / "attempt-2-data" / "invocations" / "I-vc5pcccfog" / "events.ndjson"
    transcript = AnswerTranscript(session_id=None, role="reviewer")
    prompt_ids: set = set()
    terminal_ids: list = []
    sequence = 0
    for index, line in enumerate(stream.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        message = json.loads(line)
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("method") == "session/prompt":
            prompt_ids.add(message.get("id"))
        elif message.get("method") == "session/update":
            transcript.observe_update(
                params.get("update") if isinstance(params.get("update"), dict) else {},
                params=params,
                sequence=sequence,
                line_index=index,
            )
            sequence += 1
        elif isinstance(message.get("result"), dict) and "stopReason" in message["result"]:
            terminal_ids.append(message.get("id"))

    answer = transcript.final_answer()
    assert answer is not None
    assert answer.chunk_count >= 1
    assert any(item in prompt_ids for item in terminal_ids), "the terminal response answers the prompt"
    review = decode_review(answer.text)

    assert review.verdict == "accepted"
    assert len(review.findings) == 5
    assert [finding["id"] for finding in review.findings] == [
        "AC-1",
        "AC-2",
        "AC-3",
        "EVIDENCE-E-zgamyka8s3",
        "F-1",
    ]
    assert {
        finding.get("target") for finding in review.findings if finding["id"].startswith("AC-")
    } == {CANDIDATE_FINGERPRINT}
    assert len(answer.text) > 1000, "the whole final message, not a fragment"


def test_saved_review_replays_through_the_acceptance_predicates(
    ledger_copy: Path, no_child_processes: None
) -> None:
    """The tool's own decision, evaluated on a copy of the ledger, passes its predicates."""
    _require_saved_evidence()
    tool = _load_tool()

    diagnostic = tool.replay(RUN_ID, store_path=ledger_copy, project_dir=PROJECT_DIR)

    # The production ledger already carries the decision, so the tool reports it and writes
    # nothing. The predicates were exercised when it was recorded; `test_local_finalization`
    # exercises them again on a rewound copy.
    if "already_finalized" in diagnostic:
        decision = diagnostic["already_finalized"]
        assert decision["review_status"] == "accepted"
        assert decision["candidate_fingerprint"] == CANDIDATE_FINGERPRINT
        assert decision["git_commit"] == CANDIDATE_COMMIT
        assert decision["original_block_code"] == "review_rejected"
        assert decision["original_runtime_build"] == ORIGINAL_BUILD
        assert decision["source_evidence_id"] == REVIEW_EVIDENCE_ID
        assert diagnostic["blockers"] == []
    else:
        acceptance = diagnostic["acceptance"]
        assert acceptance["accepted"] is True
        assert acceptance["candidate_unchanged"] is True
        assert acceptance["verification_evidence_current"] is True
        assert acceptance["candidate_fingerprint_recorded"] == CANDIDATE_FINGERPRINT
        assert acceptance["candidate_fingerprint_recomputed"] == CANDIDATE_FINGERPRINT
        assert acceptance["isolated_receipt_written"] is True
        assert "test artifact" in acceptance["receipt_artifact"]


def test_the_replay_leaves_the_copy_untouched(ledger_copy: Path, no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()
    before = _sha256(ledger_copy)

    diagnostic = tool.replay(RUN_ID, store_path=ledger_copy, project_dir=PROJECT_DIR)

    assert _sha256(ledger_copy) == before
    assert diagnostic["store_mutated"] is False
    assert diagnostic["store_sha256"] == diagnostic["store_sha256_after"]
    assert diagnostic["model_calls"] == 0
    assert diagnostic["submissions"] == 0


def test_the_record_keeps_the_original_failure_and_the_new_decision_apart() -> None:
    """Whatever the local finalization did, the original execution's facts survive."""
    _require_saved_evidence()

    run = _rows("SELECT * FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    attempt = _rows("SELECT * FROM attempts WHERE run_id = ?", (RUN_ID,))
    assert run["run_id"] == RUN_ID
    assert run["spec_digest"] == (
        "sha256:fcea3247e3508d30b4abdb84e5c8264bdcb7829feaa0c406cbd22068ac001806"
    )
    assert [row["attempt_id"] for row in attempt] == ["A-7f2pbp4teu"]
    assert attempt[0]["invocation_id"] == "I-xf3sez1lho"
    assert attempt[0]["review_invocation_id"] == "I-vc5pcccfog"

    if not run["receipt_json"]:
        # Not finalized (yet): the historical blocked decision stands as the current state.
        assert run["task_state"] == "BLOCKED"
        assert run["block_code"] == "review_rejected"
        assert run["delivery_state"] == "NONE"
        return

    receipt = json.loads(run["receipt_json"])
    assert receipt["provenance"]["kind"] == "offline_reprocessing"
    assert receipt["provenance"]["original_decision"] == "BLOCKED"
    assert receipt["provenance"]["original_block_code"] == "review_rejected"
    assert receipt["provenance"]["original_runtime_build"] == ORIGINAL_BUILD
    assert receipt["provenance"]["source_evidence_id"] == REVIEW_EVIDENCE_ID
    assert receipt["provenance"]["model_calls"] == 0
    assert receipt["candidate"]["git_commit"] == CANDIDATE_COMMIT
    assert receipt["candidate"]["fingerprint"] == CANDIDATE_FINGERPRINT
    assert receipt["review"]["status"] == "accepted"
    assert receipt["review"]["isolation"] == "prompt_only"
    # The receipt names the build that made this decision, not the original runtime.
    assert receipt["runtime_build"] != ORIGINAL_BUILD
    assert any("LATER decision" in line for line in receipt["limitations"])
    notes = [row["note"] for row in _rows(
        "SELECT note FROM run_notes WHERE run_id = ? ORDER BY created_at", (RUN_ID,)
    )]
    assert any("offline reprocessing decision" in note for note in notes)
    assert any(ORIGINAL_BUILD in note for note in notes)


def test_the_authorization_still_reads_two_of_two() -> None:
    _require_saved_evidence()

    rows = _rows(
        "SELECT authorization_id, provided_by, max_top_level_submissions, "
        "used_top_level_submissions FROM authorizations"
    )
    record = {row["authorization_id"]: row for row in rows}["AUTH-m2-live-2"]
    assert record["used_top_level_submissions"] == 2
    assert record["max_top_level_submissions"] == 2
    assert record["provided_by"] == "user"


def test_the_saved_candidate_is_still_what_was_frozen() -> None:
    _require_saved_evidence()
    invoked = _rows("SELECT worktree_path FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    worktree = Path(invoked["worktree_path"])
    assert worktree.is_dir()
    git_file = (worktree / ".git").read_text(encoding="utf-8").strip()
    git_dir = Path(git_file.split("gitdir:", 1)[1].strip())
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    assert head == CANDIDATE_COMMIT, "the frozen worktree must still be at the candidate commit"
    scoped = worktree / "src" / "reportkit" / "__init__.py"
    assert scoped.is_file()
    assert scoped.stat().st_size == 515


def test_a_missing_run_is_reported_not_invented(no_child_processes: None) -> None:
    _require_saved_evidence()
    tool = _load_tool()

    with pytest.raises(tool.ReplayError):
        tool.replay("R-does-not-exist", store_path=STORE, project_dir=PROJECT_DIR)


def test_repeating_the_local_finalization_writes_nothing_more(tmp_path: Path) -> None:
    """The production ledger is either not finalized, or finalized exactly once and stable."""
    _require_saved_evidence()
    before = _rows(
        "SELECT task_state, delivery_state, receipt_json, updated_at FROM runs WHERE run_id = ?",
        (RUN_ID,),
    )[0]
    notes_before = _rows(
        "SELECT COUNT(*) AS n FROM run_notes WHERE run_id = ?", (RUN_ID,)
    )[0]["n"]

    completed = subprocess.run(  # noqa: S603 - fixed local tool invocation
        [sys.executable, str(TOOL), RUN_ID, "--finalize", "--quiet"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    after = _rows(
        "SELECT task_state, delivery_state, receipt_json, updated_at FROM runs WHERE run_id = ?",
        (RUN_ID,),
    )[0]
    if before["receipt_json"]:
        assert after == before, "a repeated finalization must change nothing"
        assert (
            _rows("SELECT COUNT(*) AS n FROM run_notes WHERE run_id = ?", (RUN_ID,))[0]["n"]
            == notes_before
        )
    else:
        assert after["receipt_json"], "the first finalization must record a receipt"
    assert _rows("SELECT used_top_level_submissions AS u FROM authorizations")[0]["u"] == 2
