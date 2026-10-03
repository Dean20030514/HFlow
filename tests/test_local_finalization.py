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
    # The recorded reviewer was shown the untyped contract; the ledger says so, twice.
    assert receipt.provenance["review_contract"].startswith("untyped_findings")
    assert receipt.provenance["typed_contract_error"]
    assert json.loads(evidence[0]["detail"])["review_contract"] == receipt.provenance["review_contract"]


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
    assert not second["already_finalized"]["review_contract"].startswith("not recorded")
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


UNTYPED_ANSWER = (
    "```json\n"
    + json.dumps(
        {
            "verdict": "accepted",
            "findings": [
                {"id": "AC-1", "status": "pass", "target": "src/x.py", "detail": "looks right"}
            ],
        }
    )
    + "\n```"
)
#: A recorded reviewer prompt from before typed findings: it does not carry the typed marker.
UNTYPED_PROMPT = "Review the frozen candidate. findings is a list of objects."


def _seed_finalizable_run(tmp_path: Path) -> tuple[Path, dict, dict]:
    """A blocked run with current verification evidence, ready for ``finalize_in_store``."""
    store_path = tmp_path / "guard.sqlite"
    store = Store(store_path)
    try:
        _seed_blocked_run(store)
        run = store.get_run("R-guard")
        from hflow.contracts import EvidenceStatus

        store.record_evidence(
            evidence_id="E-verify",
            run_id="R-guard",
            attempt_id="A-guard",
            kind="verification",
            status=EvidenceStatus.PASSED,
            candidate_fingerprint="sha256:fp",
            checks_digest=run["checks_digest"],
            check_id="unit",
            detail="passed",
        )
        with store.transaction() as conn:
            conn.execute(
                "UPDATE runs SET worktree_path = ? WHERE run_id = ?", (str(tmp_path), "R-guard")
            )
        run_row = dict(store.get_run("R-guard"))
        attempt_row = dict(store.attempts_for("R-guard")[0])
    finally:
        store.close()
    return store_path, run_row, attempt_row


def _untyped_replay(tool, run_row: dict, store_path: Path) -> tuple[object, dict, dict]:
    checks: dict = {}
    review = tool._decode_saved_answer(UNTYPED_ANSWER, checks, UNTYPED_PROMPT)
    checks["verdict_digest"] = "sha256:verdict"
    checks["answer_sha256"] = "sha256:answer"
    diagnostic = {"review": checks, "recorded": {}, "candidate": {}}
    verification = next(
        row for row in tool._load_evidence(store_path, "R-guard") if row["kind"] == "verification"
    )
    provenance = tool.reprocessing_provenance(diagnostic, run_row, verification)
    return review, diagnostic, provenance


def test_finalizing_an_untyped_replay_records_the_contract_in_the_ledger(tmp_path: Path) -> None:
    """A verdict read under the pre-typed contract says so in the receipt and the evidence row."""
    tool = _load_tool()
    store_path, run_row, attempt_row = _seed_finalizable_run(tmp_path)
    review, diagnostic, provenance = _untyped_replay(tool, run_row, store_path)
    assert type(review).__name__ == "RecordedUntypedReview"

    result = tool.finalize_in_store(
        store_path=store_path,
        run_row=run_row,
        attempt_row=attempt_row,
        diagnostic=diagnostic,
        provenance=provenance,
        processing_build=CHECK_BUILD,
        review=review,
    )

    assert result["written"] is True
    store = Store(store_path)
    try:
        stored = ResultReceipt.model_validate(json.loads(store.get_run("R-guard")["receipt_json"]))
        assert stored.provenance["review_contract"] == (
            "untyped_findings (recorded before typed findings)"
        )
        assert stored.provenance["typed_contract_error"]
        rows = [dict(row) for row in store.evidence_for("R-guard", "review")]
        assert [row["evidence_id"] for row in rows] == stored.review.evidence_ids
        detail = json.loads(rows[0]["detail"])
        assert detail["review_contract"] == stored.provenance["review_contract"]
        # The verdict stays where every review evidence row keeps it.
        assert detail["verdict"] == "accepted"
        assert detail["findings"][0]["status"] == "pass"
    finally:
        store.close()


def test_a_typed_replay_names_the_typed_contract(tmp_path: Path) -> None:
    tool = _load_tool()
    checks: dict = {}
    typed = '```json\n{"verdict": "accepted", "findings": []}\n```'
    tool._decode_saved_answer(typed, checks, UNTYPED_PROMPT)
    provenance = tool.reprocessing_provenance(
        {"review": checks},
        {"checks_digest": "sha256:c"},
        {"evidence_id": "E-v", "candidate_fingerprint": "sha256:fp"},
    )
    assert provenance["review_contract"] == "typed_findings"
    assert "typed_contract_error" not in provenance


def test_a_finalization_without_a_recorded_contract_is_refused(tmp_path: Path) -> None:
    tool = _load_tool()
    store_path, run_row, attempt_row = _seed_finalizable_run(tmp_path)
    review, diagnostic, provenance = _untyped_replay(tool, run_row, store_path)
    provenance["review_contract"] = ""

    with pytest.raises(tool.ReplayError, match="contract"):
        tool.finalize_in_store(
            store_path=store_path,
            run_row=run_row,
            attempt_row=attempt_row,
            diagnostic=diagnostic,
            provenance=provenance,
            processing_build=CHECK_BUILD,
            review=review,
        )
    store = Store(store_path)
    try:
        assert store.get_run("R-guard")["receipt_json"] is None
        assert list(store.evidence_for("R-guard", "review")) == []
    finally:
        store.close()


def test_a_review_detail_the_store_would_cut_is_refused(tmp_path: Path) -> None:
    """The store keeps 2000 characters; a cut detail would lose the verdict and its contract."""
    tool = _load_tool()
    store_path, run_row, attempt_row = _seed_finalizable_run(tmp_path)
    review, diagnostic, provenance = _untyped_replay(tool, run_row, store_path)
    long_review = review.model_copy(
        update={"findings": [{"id": "AC-1", "detail": "x" * tool.REVIEW_EVIDENCE_DETAIL_LIMIT}]}
    )

    with pytest.raises(StoreError, match="would cut it"):
        tool.finalize_in_store(
            store_path=store_path,
            run_row=run_row,
            attempt_row=attempt_row,
            diagnostic=diagnostic,
            provenance=provenance,
            processing_build=CHECK_BUILD,
            review=long_review,
        )
    store = Store(store_path)
    try:
        assert store.get_run("R-guard")["receipt_json"] is None
        assert store.get_run("R-guard")["task_state"] == TaskState.BLOCKED.value
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


# --------------------------------------------------------------------------
# 3. a reviewer message after the prompt response is a replay blocker
# --------------------------------------------------------------------------


def _chunk(session_id: str, message_id: str, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": message_id,
                "content": {"type": "text", "text": text},
            },
        },
    }


def test_a_reviewer_message_after_the_prompt_response_is_a_replay_blocker() -> None:
    """Synthetic, no recorded ledger: the extractor reports text after the bound response.

    The last message group is the trailing ``accepted``, so without the count the replay would
    decode it as the verdict over the in-turn ``changes_requested``. The caller turns a non-zero
    count into a blocker, and blockers gate ``--finalize``.
    """
    from hflow.review import decode_review

    tool = _load_tool()
    messages = [
        {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": "s-1"}},
        _chunk("s-1", "m-1", '```json\n{"verdict": "changes_requested", "findings": []}\n```\n'),
        {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}},
        _chunk("s-1", "m-9", '```json\n{"verdict": "accepted", "findings": []}\n```\n'),
    ]

    extraction = tool.extract_reviewer_answer(messages)

    assert extraction["bound"] is True
    assert extraction["prompt_response_line"] == 2
    assert extraction["message_chunks_after_prompt_response"] == 1
    assert decode_review(extraction["answer"].text).verdict == "accepted", (
        "the trailing text is what the replay would otherwise take as the verdict"
    )


def test_the_tool_refuses_to_finalize_a_reviewer_stream_with_text_after_its_response(
    blocked_ledger_copy: Path, no_child_processes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded stream with one after-response chunk reported: a blocker, nothing written."""
    _require_recorded_ledger()
    tool = _load_tool()
    original = tool.extract_reviewer_answer

    def with_a_trailing_chunk(messages):
        extraction = original(messages)
        assert extraction["message_chunks_after_prompt_response"] == 0, "the recording has none"
        return {**extraction, "message_chunks_after_prompt_response": 1}

    monkeypatch.setattr(tool, "extract_reviewer_answer", with_a_trailing_chunk)

    diagnostic = tool.replay(
        RUN_ID,
        store_path=blocked_ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )

    assert any(
        "arrived after the session/prompt response" in item for item in diagnostic["blockers"]
    )
    assert diagnostic["finalization"]["written"] is False
    after = _rows(
        blocked_ledger_copy, "SELECT task_state, receipt_json FROM runs WHERE run_id = ?", (RUN_ID,)
    )[0]
    assert after["receipt_json"] is None
    assert after["task_state"] == TaskState.BLOCKED.value


# --------------------------------------------------------------------------
# 4. what the production driver takes no verdict from, the replay refuses too
# --------------------------------------------------------------------------

ACCEPTED_BLOCK = '```json\n{"verdict": "accepted", "findings": []}\n```\n'
PROMPT = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": "s-1"}}
PROMPT_ERROR = {
    "jsonrpc": "2.0",
    "id": 2,
    "error": {"code": -32603, "message": "Internal error: turn failed"},
}


def _settled(stop_reason: str) -> dict:
    return {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": stop_reason}}


def _chunk_without_session(message_id: str, text: str) -> dict:
    chunk = _chunk("s-1", message_id, text)
    del chunk["params"]["sessionId"]
    return chunk


@pytest.mark.parametrize(
    ("answers", "stop_reason", "error_ids"),
    [
        ([PROMPT_ERROR, _settled("end_turn")], "end_turn", ["2"]),
        ([_settled("end_turn"), PROMPT_ERROR], "end_turn", ["2"]),
        ([_settled("paused")], "paused", []),
        ([_settled("refusal")], "refusal", []),
    ],
)
def test_the_extractor_reports_how_the_reviewer_prompt_was_answered(
    answers: list[dict], stop_reason: str, error_ids: list[str]
) -> None:
    """Synthetic: the bound response's stop reason and any error carrying the prompt's id.

    The driver takes no verdict from any of these turns (``prompt_error_response`` /
    ``unknown_stop_reason`` / ``stop_reason_refusal``); the replay used to decode ``accepted``.
    """
    tool = _load_tool()
    messages = [PROMPT, _chunk("s-1", "m-1", ACCEPTED_BLOCK), *answers]

    extraction = tool.extract_reviewer_answer(messages)

    assert extraction["bound"] is True
    assert extraction["prompt_stop_reason"] == stop_reason
    assert extraction["prompt_error_ids"] == error_ids


def test_the_extractor_binds_to_the_session_the_prompt_named() -> None:
    """Synthetic: a chunk with no ``sessionId`` is counted and leaves no answer to decode.

    It is not one of the prompt session's updates, so it is not counted as trailing; it still
    makes the answer unusable, as in the production driver, and the count is a replay blocker.
    """
    tool = _load_tool()
    messages = [
        PROMPT,
        _chunk("s-1", "m-1", ACCEPTED_BLOCK),
        _settled("end_turn"),
        _chunk_without_session(
            "m-9", '```json\n{"verdict": "changes_requested", "findings": []}\n```\n'
        ),
    ]

    extraction = tool.extract_reviewer_answer(messages)

    assert extraction["prompt_session_id"] == "s-1"
    assert extraction["transcript"].skipped_other_session == 1
    assert extraction["message_chunks_after_prompt_response"] == 0
    assert "names session None" in extraction["transcript"].rejected
    assert extraction["answer"] is None


def test_the_extractor_refuses_a_message_chunk_before_the_prompt() -> None:
    tool = _load_tool()
    messages = [_chunk("s-1", "m-0", ACCEPTED_BLOCK), PROMPT, _settled("end_turn")]

    extraction = tool.extract_reviewer_answer(messages)

    assert "before the session/prompt request" in extraction["transcript"].rejected
    assert extraction["answer"] is None


def _with_error_answer(messages: list[dict]) -> list[dict]:
    return [*messages, dict(PROMPT_ERROR)]


def _settled_as_paused(messages: list[dict]) -> list[dict]:
    return [
        {**message, "result": {**message["result"], "stopReason": "paused"}}
        if isinstance(message.get("result"), dict) and "stopReason" in message["result"]
        else message
        for message in messages
    ]


def _with_a_sessionless_chunk(messages: list[dict]) -> list[dict]:
    return [*messages, _chunk_without_session("m-late", ACCEPTED_BLOCK)]


def _with_a_chunk_before_the_prompt(messages: list[dict]) -> list[dict]:
    index = next(
        i for i, message in enumerate(messages) if message.get("method") == "session/prompt"
    )
    early = _chunk(messages[index]["params"]["sessionId"], "m-early", ACCEPTED_BLOCK)
    return [*messages[:index], early, *messages[index:]]


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    [
        (_with_error_answer, "answered with a JSON-RPC error"),
        (_settled_as_paused, "settled with stopReason='paused', not end_turn"),
        (_with_a_sessionless_chunk, "or no usable sessionId; the reviewer's final answer"),
        (_with_a_chunk_before_the_prompt, "arrived before the session/prompt request"),
    ],
)
def test_the_tool_refuses_to_finalize_what_the_driver_takes_no_verdict_from(
    blocked_ledger_copy: Path,
    no_child_processes: None,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    blocker: str,
) -> None:
    """The recorded reviewer stream with one change: a blocker, and ``--finalize`` writes nothing.

    The sessionless chunk is the sharpest case: it is the stream's only defect, the single-session
    check counts string session ids only, and the trailing check never counts it, so the session
    binding is what refuses it - both as a counted exclusion and as an unusable answer.
    """
    _require_recorded_ledger()
    tool = _load_tool()
    original = tool._load_messages

    def mutated(stream_path):
        messages, unparseable = original(stream_path)
        return mutate(messages), unparseable

    monkeypatch.setattr(tool, "_load_messages", mutated)

    diagnostic = tool.replay(
        RUN_ID,
        store_path=blocked_ledger_copy,
        project_dir=PROJECT_DIR,
        finalize=True,
        processing_build=CHECK_BUILD,
    )

    assert any(blocker in item for item in diagnostic["blockers"]), diagnostic["blockers"]
    if mutate is _with_a_sessionless_chunk:
        assert any(
            item.startswith("the reviewer's answer is unusable:") and "names session None" in item
            for item in diagnostic["blockers"]
        ), diagnostic["blockers"]
    assert diagnostic["finalization"]["written"] is False
    after = _rows(
        blocked_ledger_copy, "SELECT task_state, receipt_json FROM runs WHERE run_id = ?", (RUN_ID,)
    )[0]
    assert after["receipt_json"] is None
    assert after["task_state"] == TaskState.BLOCKED.value
