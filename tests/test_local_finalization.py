"""The local finalization action: one later decision, recorded without rewriting history.

The first group copies the *real* recorded ledger into a temporary directory and finalizes
there, so the actual recorded evidence is exercised end to end with the production ledger
untouched. The second group drives the Store guard directly for the fail-closed and
idempotency branches, which would need a corrupted recording to reach through the tool.

No test here dispatches anything: a subprocess trap is active for every one of them.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hflow.contracts import (
    CandidateSnapshot,
    DeliveryState,
    InvocationOutcome,
    ReviewResult,
    ResultReceipt,
    TaskState,
    UsageFacts,
    VerificationResult,
)
from hflow.store import OFFLINE_REPROCESSING_KIND, Store, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / ".probe" / "m2-live"
PRODUCTION_STORE = PROBE / "attempt-2-data" / "hflow.sqlite"
PROJECT_DIR = PROBE / "attempt-2"
RUN_ID = "R-gkb3ld97x8"
ATTEMPT_ID = "A-7f2pbp4teu"
REVIEW_EVIDENCE_ID = "E-zgamyka8s3"
CANDIDATE_FINGERPRINT = "sha256:5c47b12d12031c4b94f4254c7882615ec7afaecbbef24b17e0483d8b4ac1d405"
CHECK_BUILD = "hflow/0.0.1+local-finalization-test"

TOOL = REPO_ROOT / "tools" / "m2_live" / "replay_review.py"


def _require_recorded_ledger() -> None:
    if not PRODUCTION_STORE.is_file():
        pytest.skip(f"the recorded ledger is not present at {PRODUCTION_STORE}")


def _load_tool():
    spec = importlib.util.spec_from_file_location("hflow_replay_finalize", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def no_child_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finalization must not launch anything: no dispatch, no credential helper, no git."""

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"finalization tried to launch a process: {args!r} {kwargs!r}")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, deny)
    monkeypatch.setattr("os.system", deny)
    monkeypatch.setattr("os.popen", deny)


@pytest.fixture()
def ledger_copy(tmp_path: Path) -> Path:
    """A byte copy of the production ledger, so a write here cannot touch the record."""
    _require_recorded_ledger()
    target = tmp_path / "ledger" / "hflow.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PRODUCTION_STORE, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(PRODUCTION_STORE) + suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, Path(str(target) + suffix))
    return target


@pytest.fixture()
def blocked_ledger_copy(ledger_copy: Path) -> Path:
    """The same copy, rewound to the recorded pre-finalization state.

    The production ledger may already carry the offline decision. Rewinding only this copy's
    *decision columns* - never its evidence, notes or authorization rows - is what lets the
    finalization itself be exercised again without touching the record.
    """
    connection = sqlite3.connect(ledger_copy)
    try:
        with connection:
            connection.execute(
                "DELETE FROM evidence WHERE evidence_id <> ?", (REVIEW_EVIDENCE_ID,)
            )
            connection.execute(
                "DELETE FROM run_notes WHERE note LIKE 'offline reprocessing decision%'"
            )
            connection.execute(
                "UPDATE runs SET task_state = 'BLOCKED', phase = NULL, delivery_state = 'NONE', "
                "receipt_json = NULL, block_code = 'review_rejected', "
                "block_reason = 'independent review requested changes; automatic repair is "
                "deferred to M3' WHERE run_id = ?",
                (RUN_ID,),
            )
    finally:
        connection.close()
    return ledger_copy


def _rows(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 1. the real recorded evidence, finalized in a copy
# --------------------------------------------------------------------------


def test_the_recorded_evidence_finalizes_in_a_ledger_copy(
    blocked_ledger_copy: Path, no_child_processes: None
) -> None:
    _require_recorded_ledger()
    tool = _load_tool()
    ledger_copy = blocked_ledger_copy

    diagnostic = tool.replay(
        RUN_ID,
        store_path=ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )

    assert diagnostic["blockers"] == []
    finalization = diagnostic["finalization"]
    assert finalization["written"] is True
    assert finalization["task_state"] == TaskState.ACCEPTED.value
    assert finalization["delivery_state"] == DeliveryState.LOCAL_CANDIDATE.value
    assert diagnostic["acceptance"]["accepted"] is True

    rows = _rows(ledger_copy, "SELECT * FROM runs WHERE run_id = ?", (RUN_ID,))
    run = rows[0]
    assert run["task_state"] == TaskState.ACCEPTED.value
    assert run["delivery_state"] == DeliveryState.LOCAL_CANDIDATE.value
    receipt = ResultReceipt.model_validate(json.loads(run["receipt_json"]))

    # The new decision names the build that processed it, not the original runtime.
    assert receipt.runtime_build == CHECK_BUILD
    assert receipt.provenance["kind"] == OFFLINE_REPROCESSING_KIND
    assert receipt.provenance["original_block_code"] == "review_rejected"
    assert receipt.provenance["original_runtime_build"] == "hflow/0.0.1+3dbfeae"
    assert receipt.provenance["source_evidence_id"] == REVIEW_EVIDENCE_ID
    assert receipt.provenance["model_calls"] == 0
    assert receipt.review.status == "accepted"
    assert receipt.candidate.fingerprint == CANDIDATE_FINGERPRINT
    assert receipt.candidate.git_commit == "499ece7043fe3267b4ff89f9a5b5bc1d70c42481"
    assert receipt.attempt_id == ATTEMPT_ID
    assert any("LATER decision" in line for line in receipt.limitations)

    # The original failure is still readable next to the delivery.
    notes = _rows(
        ledger_copy, "SELECT note FROM run_notes WHERE run_id = ? ORDER BY created_at", (RUN_ID,)
    )
    preserved = [row["note"] for row in notes if "offline reprocessing decision" in row["note"]]
    assert len(preserved) == 1
    assert "BLOCKED/review_rejected" in preserved[0]
    assert "hflow/0.0.1+3dbfeae" in preserved[0]
    assert CHECK_BUILD in preserved[0]

    # The review evidence the receipt names exists, and is the same verdict.
    evidence = _rows(
        ledger_copy,
        "SELECT * FROM evidence WHERE evidence_id = ?",
        (receipt.review.evidence_ids[0],),
    )
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "review"
    assert evidence[0]["status"] == "passed"
    assert evidence[0]["candidate_fingerprint"] == CANDIDATE_FINGERPRINT
    assert json.loads(evidence[0]["detail"])["verdict"] == "accepted"


def test_the_finalization_consumes_no_allowance_and_adds_no_submission(
    ledger_copy: Path, no_child_processes: None
) -> None:
    _require_recorded_ledger()
    tool = _load_tool()
    before = _rows(
        ledger_copy, "SELECT * FROM authorizations"
    )
    before_run = _rows(ledger_copy, "SELECT * FROM runs WHERE run_id = ?", (RUN_ID,))[0]

    tool.replay(
        RUN_ID,
        store_path=ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )

    after = _rows(ledger_copy, "SELECT * FROM authorizations")
    after_run = _rows(ledger_copy, "SELECT * FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    assert after == before, "a local decision must not change any authorization row"
    assert [row["authorization_id"] for row in after] == ["AUTH-m2-live-2"]
    assert after[0]["used_top_level_submissions"] == 2
    # No new attempt, no reserved turns, no observed turn: nothing was dispatched.
    assert _rows(ledger_copy, "SELECT * FROM attempts WHERE run_id = ?", (RUN_ID,))[0][
        "attempt_id"
    ] == ATTEMPT_ID
    assert len(_rows(ledger_copy, "SELECT * FROM attempts WHERE run_id = ?", (RUN_ID,))) == 1
    assert after_run["turns_reserved"] == before_run["turns_reserved"]
    assert after_run["turns_observed"] == before_run["turns_observed"]


def test_a_second_finalization_changes_nothing(blocked_ledger_copy: Path, no_child_processes: None) -> None:
    """Idempotent: the same evidence and candidate must not issue a second delivery."""
    _require_recorded_ledger()
    tool = _load_tool()
    ledger_copy = blocked_ledger_copy
    first = tool.replay(
        RUN_ID,
        store_path=ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )
    first_receipt = _rows(ledger_copy, "SELECT receipt_json, updated_at FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    notes_after_first = _rows(
        ledger_copy, "SELECT COUNT(*) AS n FROM run_notes WHERE run_id = ?", (RUN_ID,)
    )[0]["n"]

    second = tool.replay(
        RUN_ID,
        store_path=ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )

    assert first["finalization"]["written"] is True
    assert second["finalization"]["written"] is False
    assert second["already_finalized"]["runtime_build"] == CHECK_BUILD
    assert second["already_finalized"]["original_block_code"] == "review_rejected"
    assert (
        _rows(ledger_copy, "SELECT receipt_json, updated_at FROM runs WHERE run_id = ?", (RUN_ID,))[0]
        == first_receipt
    ), "the recorded decision must be byte-identical after a repeated call"
    assert (
        _rows(ledger_copy, "SELECT COUNT(*) AS n FROM run_notes WHERE run_id = ?", (RUN_ID,))[0]["n"]
        == notes_after_first
    ), "a repeated call must not append another decision note"


def test_the_read_only_replay_never_writes(ledger_copy: Path, no_child_processes: None) -> None:
    """Without ``finalize`` the same command evaluates and leaves the ledger alone."""
    _require_recorded_ledger()
    tool = _load_tool()
    before = ledger_copy.read_bytes()

    diagnostic = tool.replay(RUN_ID, store_path=ledger_copy, project_dir=PROJECT_DIR)

    assert diagnostic["store_mutated"] is False
    assert "finalization" not in diagnostic
    assert ledger_copy.read_bytes() == before


def test_the_report_keeps_the_original_failure_next_to_the_delivery(
    blocked_ledger_copy: Path, no_child_processes: None
) -> None:
    """A receipt that records a later decision must never read as the execution's own success."""
    _require_recorded_ledger()
    from hflow.report import receipt_text

    tool = _load_tool()
    diagnostic = tool.replay(
        RUN_ID,
        store_path=blocked_ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )
    assert diagnostic["finalization"]["written"] is True
    run = _rows(blocked_ledger_copy, "SELECT receipt_json FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    text = receipt_text(ResultReceipt.model_validate(json.loads(run["receipt_json"])))

    assert "provenance" in text
    assert "original_decision     BLOCKED / review_rejected" in text
    assert "original_runtime      hflow/0.0.1+3dbfeae" in text
    assert "offline_reprocessing" in text
    assert CHECK_BUILD in text
    assert "not the execution's own outcome" in text


# --------------------------------------------------------------------------
# 2. fail-closed branches (Store guard, seeded states)
# --------------------------------------------------------------------------


def _seed_blocked_run(store: Store, state: TaskState = TaskState.BLOCKED, *, reason: str = "review_rejected") -> None:
    from hflow.contracts import (
        AcceptanceCriterion,
        BudgetRequest,
        ProjectConfig,
        ProjectLimits,
        ReuseDecision,
        ReuseStatus,
        Scope,
        TaskSpec,
    )

    spec = TaskSpec(
        task_id="T-guard",
        revision=1,
        goal="guard the offline finalization path",
        acceptance=[AcceptanceCriterion(id="AC-1", statement="x", check_ids=["unit"])],
        scope=Scope(write_allow=["src/x.py"]),
        reuse=ReuseDecision(status=ReuseStatus.EXISTING_DECISION, reference="r", reason="r"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=0),
    )
    project = ProjectConfig(project_id="guard", checks=[], limits=ProjectLimits())
    store.create_run(
        run_id="R-guard",
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="hflow/test",
        checks_digest=project.checks_digest(),
        turn_limit=4,
        repair_limit=0,
    )
    store.claim_run("R-guard", "test")
    # The recorded attempt must exist: the finalization names it, and evidence references it.
    store.dispatch_attempt(
        run_id="R-guard",
        controller_id="test",
        attempt_id="A-guard",
        role="implementer",
        reservation_id="B-guard",
        reserved_turns=1,
        reservation_expires_at="2999-01-01T00:00:00Z",
    )
    store.record_invocation("A-guard", "I-guard")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE attempts SET state = 'SUCCEEDED', outcome = 'completed' WHERE attempt_id = ?",
            ("A-guard",),
        )
    store.set_task_state("R-guard", [TaskState.RUNNING], state, idempotent=True)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE runs SET block_code = ?, block_reason = ? WHERE run_id = ?",
            (None if state is not TaskState.BLOCKED else reason, "seeded", "R-guard"),
        )


def _receipt(*, fingerprint: str, evidence_id: str = "E-review-replay") -> ResultReceipt:
    return ResultReceipt(
        run_id="R-guard",
        task_id="T-guard",
        attempt_id="A-guard",
        task_revision=1,
        runtime_build=CHECK_BUILD,
        plan_digest="sha256:plan",
        harness_outcome=InvocationOutcome.COMPLETED,
        candidate=CandidateSnapshot(fingerprint=fingerprint),
        verification=VerificationResult(status="passed"),
        review=ReviewResult(status="accepted", evidence_ids=[evidence_id]),
        task_state=TaskState.ACCEPTED,
        delivery_state=DeliveryState.LOCAL_CANDIDATE,
        usage=UsageFacts(),
        provenance={
            "kind": OFFLINE_REPROCESSING_KIND,
            "source_evidence_id": "E-source",
            "original_block_code": "review_rejected",
        },
    )


def test_a_cancelled_run_is_never_reopened(tmp_path: Path) -> None:
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        store.record_cancel_intent("R-guard")
        with pytest.raises(StoreError) as excinfo:
            store.finalize_offline_reprocessing(
                "R-guard",
                _receipt(fingerprint="sha256:fp"),
                checks_digest=store.get_run("R-guard")["checks_digest"],
                source_evidence_id="E-source",
                candidate_fingerprint="sha256:fp",
                review_evidence={"status": "passed", "detail": "{}"},
            )
        assert "cancellation intent" in str(excinfo.value)
        assert store.get_run("R-guard")["receipt_json"] is None
    finally:
        store.close()


def test_a_run_that_did_not_end_blocked_is_refused(tmp_path: Path) -> None:
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store, TaskState.CANCELLED)
        with pytest.raises(StoreError) as excinfo:
            store.finalize_offline_reprocessing(
                "R-guard",
                _receipt(fingerprint="sha256:fp"),
                checks_digest=store.get_run("R-guard")["checks_digest"],
                source_evidence_id="E-source",
                candidate_fingerprint="sha256:fp",
                review_evidence={"status": "passed", "detail": "{}"},
            )
        assert "without a recorded failure" in str(excinfo.value)
        assert store.get_run("R-guard")["receipt_json"] is None
    finally:
        store.close()


def test_a_receipt_must_declare_its_provenance(tmp_path: Path) -> None:
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        receipt = _receipt(fingerprint="sha256:fp")
        receipt = receipt.model_copy(update={"provenance": {}})
        with pytest.raises(StoreError) as excinfo:
            store.finalize_offline_reprocessing(
                "R-guard",
                receipt,
                checks_digest=store.get_run("R-guard")["checks_digest"],
                source_evidence_id="E-source",
                candidate_fingerprint="sha256:fp",
                review_evidence={"status": "passed", "detail": "{}"},
            )
        assert "must declare its provenance" in str(excinfo.value)
    finally:
        store.close()


def test_a_different_decision_never_overwrites_a_receipt(tmp_path: Path) -> None:
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        digest = store.get_run("R-guard")["checks_digest"]
        assert store.finalize_offline_reprocessing(
            "R-guard",
            _receipt(fingerprint="sha256:fp"),
            checks_digest=digest,
            source_evidence_id="E-source",
            candidate_fingerprint="sha256:fp",
            review_evidence={"status": "passed", "detail": "{}"},
        )
        # The same decision is a no-op...
        assert (
            store.finalize_offline_reprocessing(
                "R-guard",
                _receipt(fingerprint="sha256:fp", evidence_id="E-review-replay-2"),
                checks_digest=digest,
                source_evidence_id="E-source",
                candidate_fingerprint="sha256:fp",
                review_evidence={"status": "passed", "detail": "{}"},
            )
            is False
        )
        # ...but different evidence, or a different candidate, is a conflict.
        with pytest.raises(StoreError) as excinfo:
            store.finalize_offline_reprocessing(
                "R-guard",
                _receipt(fingerprint="sha256:other"),
                checks_digest=digest,
                source_evidence_id="E-other",
                candidate_fingerprint="sha256:other",
                review_evidence={"status": "passed", "detail": "{}"},
            )
        assert "different decision" in str(excinfo.value)
        stored = ResultReceipt.model_validate(json.loads(store.get_run("R-guard")["receipt_json"]))
        assert stored.candidate.fingerprint == "sha256:fp"
        assert stored.provenance["source_evidence_id"] == "E-source"
    finally:
        store.close()


def test_a_checks_digest_change_blocks_the_finalization(tmp_path: Path) -> None:
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        with pytest.raises(StoreError) as excinfo:
            store.finalize_offline_reprocessing(
                "R-guard",
                _receipt(fingerprint="sha256:fp"),
                checks_digest="sha256:different",
                source_evidence_id="E-source",
                candidate_fingerprint="sha256:fp",
                review_evidence={"status": "passed", "detail": "{}"},
            )
        assert "stale" in str(excinfo.value)
        assert store.get_run("R-guard")["receipt_json"] is None
    finally:
        store.close()


def test_the_review_evidence_and_receipt_are_written_together(tmp_path: Path) -> None:
    """A receipt must never name evidence that is not in the store."""
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        receipt = _receipt(fingerprint="sha256:fp", evidence_id="E-review-together")
        store.finalize_offline_reprocessing(
            "R-guard",
            receipt,
            checks_digest=store.get_run("R-guard")["checks_digest"],
            source_evidence_id="E-source",
            candidate_fingerprint="sha256:fp",
            review_evidence={
                "status": "passed",
                "detail": json.dumps({"verdict": "accepted", "findings": []}),
            },
        )
        rows = [dict(row) for row in store.evidence_for("R-guard", "review")]
        assert [row["evidence_id"] for row in rows] == ["E-review-together"]
        assert rows[0]["status"] == "passed"
        assert rows[0]["check_id"] == "review"
        assert json.loads(rows[0]["detail"])["verdict"] == "accepted"
    finally:
        store.close()


def test_a_verdict_that_is_not_an_acceptance_still_records_a_failed_review(tmp_path: Path) -> None:
    """The write path never turns a rejection into a pass; it records what the verdict says."""
    store = Store(tmp_path / "guard.sqlite")
    try:
        _seed_blocked_run(store)
        receipt = _receipt(fingerprint="sha256:fp")
        receipt = receipt.model_copy(
            update={
                "review": ReviewResult(
                    status="changes_requested", evidence_ids=["E-review-rejected"]
                )
            }
        )
        store.finalize_offline_reprocessing(
            "R-guard",
            receipt,
            checks_digest=store.get_run("R-guard")["checks_digest"],
            source_evidence_id="E-source",
            candidate_fingerprint="sha256:fp",
            review_evidence={"status": "failed", "detail": '{"verdict": "changes_requested"}'},
        )
        rows = [dict(row) for row in store.evidence_for("R-guard", "review")]
        assert rows[0]["status"] == "failed"
        stored = ResultReceipt.model_validate(json.loads(store.get_run("R-guard")["receipt_json"]))
        assert stored.review.status == "changes_requested"
    finally:
        store.close()


def test_the_tool_refuses_to_finalize_when_the_candidate_moved(
    blocked_ledger_copy: Path, tmp_path: Path, no_child_processes: None
) -> None:
    """Changed evidence fails closed: the ledger keeps its original decision."""
    _require_recorded_ledger()
    tool = _load_tool()
    ledger_copy = blocked_ledger_copy
    # Point the recorded run at a tree whose *content* cannot match the recorded fingerprint:
    # the same scoped path in the target project, which is at the base commit, not the candidate.
    altered = tmp_path / "spec-drift.sqlite"
    shutil.copyfile(ledger_copy, altered)
    connection = sqlite3.connect(altered)
    try:
        with connection:
            connection.execute(
                "UPDATE runs SET worktree_path = ? WHERE run_id = ?",
                (str(PROBE / "project"), RUN_ID),
            )
    finally:
        connection.close()
    before = _rows(altered, "SELECT task_state, receipt_json FROM runs WHERE run_id = ?", (RUN_ID,))[0]

    diagnostic = tool.replay(
        RUN_ID, store_path=altered, project_dir=PROJECT_DIR, finalize=True, processing_build=CHECK_BUILD
    )

    assert diagnostic["blockers"], "a drifted candidate must not finalize"
    assert diagnostic["finalization"]["written"] is False
    after = _rows(altered, "SELECT task_state, receipt_json FROM runs WHERE run_id = ?", (RUN_ID,))[0]
    assert after == before
    assert after["receipt_json"] is None
    assert after["task_state"] == TaskState.BLOCKED.value
