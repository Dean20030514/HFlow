"""Batch E2: the verification chain a bounded repair decision is allowed to read.

What this file asserts, and what it deliberately leaves to ``tests/test_batch_e_repair.py``:

* ``verify_candidate(force_refresh=True)`` re-runs every required check even when a passing row
  with the same candidate fingerprint and checks digest exists, and the default keeps the
  historical reuse rule exactly;
* the reason a check ended is stored as a structured fact, and the vocabulary the repair gate
  measures is the vocabulary the real runner produces;
* ``failed_check_facts`` filters by attempt **and** candidate and keeps ERROR rows visible, so a
  previous round's failure can never describe the current candidate;
* a row written before the reason existed keeps an empty reason across the migration and is
  therefore ineligible - and so is every other ending that is not "the check ran to completion";
* repair decisions round-trip as validated ``RepairRecord`` objects, in order, one row per call.

The dispatch behaviour itself (I1 -> I2, the 3/4-dispatch worst case, cancellation at every
handoff, the cumulative diff) is exercised end to end against the controller in
``tests/test_batch_e_repair.py``. The table at the bottom of this file evaluates the *decision*
through ``RepairPolicy.business_failure_for`` - the only classification helper - plus the
structured facts, which is the same path the controller uses; it never asserts a private boolean.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hflow import migrate
from hflow.contracts import (
    AcceptanceCriterion,
    AttemptState,
    BudgetRequest,
    CheckDef,
    CheckPhase,
    DeliveryRequirement,
    EvidenceStatus,
    InvocationOutcome,
    ProjectConfig,
    ProjectLimits,
    RefusalCode,
    RepairDecision,
    RepairPolicy,
    RepairRecord,
    RepairTrigger,
    ReuseDecision,
    ReuseStatus,
    ReviewRequirement,
    Scope,
    TaskSpec,
    TaskState,
)
from hflow.drivers import winjob
from hflow.store import RunNotFound, Store, StoreError
from hflow.verify import (
    CLEAN_EXIT_REASONS,
    REASON_COMPLETED,
    REASON_NONZERO_EXIT,
    REASON_NOT_LAUNCHED,
    REASON_OUTPUT_CAPTURE_ERROR,
    REASON_SETTLEMENT_FORCED,
    REASON_TIMED_OUT,
    CheckRunners,
    CommandCheckRunner,
    FakeCheckRunner,
    _detail_with_references,
    failed_check_facts,
    verify_candidate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHECK_HELPER = FIXTURES / "check_helper.py"

RUN_ID = "R-e2-verify"
ATTEMPT_ID = "A-e2-verify"
FINGERPRINT = "sha256:candidate-one"
#: A real Job Object is required before anything about a process *tree* may be claimed.
requires_job_object = pytest.mark.skipif(
    not winjob.IS_WINDOWS,
    reason="needs a Windows Job Object: elsewhere the boundary covers the direct child only",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _project(*checks: CheckDef, review_required: bool = False) -> ProjectConfig:
    return ProjectConfig(
        project_id="e2-verify",
        checks=list(checks),
        write_deny=[".git/**", ".hflow/**"],
        limits=ProjectLimits(max_agent_turns=4, max_repair_cycles=1),
        review_required=review_required,
    )


def _task(
    check_ids: list[str],
    *,
    review_required: bool = False,
    repair_policy: RepairPolicy | None = None,
) -> TaskSpec:
    return TaskSpec(
        task_id="T-e2-verify",
        revision=1,
        goal="classify a failed check honestly",
        acceptance=[
            AcceptanceCriterion(id="AC-1", statement="the checks pass", check_ids=check_ids)
        ],
        scope=Scope(write_allow=["src/app.py"]),
        reuse=ReuseDecision(
            status=ReuseStatus.EXISTING_DECISION,
            reference="project:python-stdlib-only",
            reason="standard library only",
        ),
        review=ReviewRequirement(required=review_required),
        delivery=DeliveryRequirement(mode="local_candidate"),
        budget=BudgetRequest(max_agent_turns=4, max_repair_cycles=1),
        repair_policy=repair_policy,
    )


def _seed_checking_run(
    store: Store,
    project: ProjectConfig,
    spec: TaskSpec,
    *,
    run_id: str = RUN_ID,
    attempt_id: str = ATTEMPT_ID,
) -> str:
    """A run in CHECKING with one live attempt: the state verification records against."""
    run = store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="e2-verify-test",
        checks_digest=project.checks_digest(),
        turn_limit=4,
        repair_limit=1,
    )
    store.claim_run(run["run_id"], "local-controller")
    store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)
    store.dispatch_attempt(
        run_id=run["run_id"],
        controller_id="local-controller",
        attempt_id=attempt_id,
        role="implementer",
        reservation_id="B-e2-verify",
        reserved_turns=1,
        reservation_expires_at="2999-01-01T00:00:00Z",
    )
    store.record_invocation(attempt_id, "I-e2-verify")
    store.advance_to_checking(
        run_id=run["run_id"], attempt_id=attempt_id, phase=CheckPhase.VERIFICATION
    )
    return str(run["run_id"])


def _record(
    store: Store,
    project: ProjectConfig,
    run_id: str,
    *,
    check_id: str,
    status: EvidenceStatus,
    exit_code: int | None,
    exit_reason: str,
    kind: str = "verification",
    attempt_id: str = ATTEMPT_ID,
    fingerprint: str = FINGERPRINT,
    detail: str = "",
) -> None:
    store.record_evidence(
        evidence_id=(
            f"E-{attempt_id}-{kind}-{check_id}-{status.value}-{exit_code}"
            f"-{fingerprint.rsplit(':', 1)[-1]}"
        ),
        run_id=run_id,
        attempt_id=attempt_id,
        kind=kind,
        status=status,
        candidate_fingerprint=fingerprint,
        checks_digest=project.checks_digest(),
        check_id=check_id,
        exit_code=exit_code,
        exit_reason=exit_reason,
        detail=detail,
    )


def _facts_for(
    store: Store,
    project: ProjectConfig,
    run_id: str,
    *,
    attempt_id: str = ATTEMPT_ID,
    fingerprint: str = FINGERPRINT,
) -> list[dict[str, object]]:
    return failed_check_facts(
        store,
        run_id=run_id,
        attempt_id=attempt_id,
        candidate_fingerprint=fingerprint,
        checks_digest=project.checks_digest(),
    )


def _may_dispatch_repair(
    facts: list[dict[str, object]], policy: RepairPolicy | None
) -> bool:
    """The plan §5.2 check-failure rule, evaluated the way the controller evaluates it.

    Transcribed here so the table below asserts a *decision* instead of a boolean of its own
    making, and so the "is this exit code a declared business failure" half goes through
    ``RepairPolicy.business_failure_for`` - the only classification helper there is. The other half
    is the structured execution fact: a fact counts only when the check ran to completion
    (``CLEAN_EXIT_REASONS``) and the round contains no ERROR row at all.

    The controller's own instance of this rule is exercised end to end (dispatch counts, no third
    implementer, cancellation at every handoff) in ``tests/test_batch_e_repair.py``.
    """
    if policy is None:
        return False
    failed = [fact for fact in facts if fact["status"] == EvidenceStatus.FAILED.value]
    if not failed or len(failed) != len(facts):
        return False
    return all(
        fact["exit_reason"] in CLEAN_EXIT_REASONS
        and policy.business_failure_for(str(fact["check_id"]), fact["exit_code"])
        for fact in failed
    )


def _helper_argv(markers: Path, label: str, *extra: str) -> list[str]:
    return [sys.executable, str(CHECK_HELPER), "--markers", str(markers), "--label", label, *extra]


def _wait_for(pattern: str, markers: Path, timeout: float = 20.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = sorted(markers.glob(pattern))
        if found:
            return found[0]
        time.sleep(0.02)
    raise AssertionError(f"fixture never produced {pattern} in {markers}")


def _retire(markers: Path, pids: list[int]) -> None:
    """Never leave a fixture process behind, whatever the assertions did."""
    markers.mkdir(parents=True, exist_ok=True)
    (markers / "stop").write_text("stop", encoding="utf-8")
    for pid in pids:
        if pid > 0 and not winjob.process_gone(pid, 0.5):
            subprocess.run(  # noqa: S603 - test-only cleanup of a pid this test started
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


# --------------------------------------------------------------------------
# 1. force_refresh: a repaired candidate gets its own executions
# --------------------------------------------------------------------------


def test_force_refresh_re_runs_every_check_while_the_default_still_reuses(
    tmp_path: Path,
) -> None:
    """The whole point of E2's second round: the first round's pass is not this round's evidence.

    The same candidate fingerprint and checks digest are used for all three calls, so the only
    difference between them is ``force_refresh``. With the default, the second call reuses both
    passing rows (no new runner call); with ``force_refresh=True`` both checks run again, and the
    run keeps the earlier rows as the facts they are.
    """
    project = _project(CheckDef(id="unit", kind="fake"), CheckDef(id="docs-check", kind="fake"))
    spec = _task(["unit", "docs-check"])
    runner = FakeCheckRunner()
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        kwargs = dict(
            store=store,
            spec=spec,
            project=project,
            project_root=tmp_path,
            project_checks_digest=project.checks_digest(),
            candidate_fingerprint=FINGERPRINT,
            attempt_id=ATTEMPT_ID,
            run_id=run_id,
            runners=CheckRunners({"fake": runner}),
        )
        first = verify_candidate(**kwargs)
        rows_after_first = [dict(row) for row in store.evidence_for(run_id, "verification")]

        assert first.status == "passed", first.detail
        assert runner.calls == ["unit", "docs-check"]
        assert runner.cache_hits == 0

        second = verify_candidate(**kwargs)
        rows_after_second = [dict(row) for row in store.evidence_for(run_id, "verification")]

        assert runner.calls == ["unit", "docs-check"], "the default must not run a check again"
        assert runner.cache_hits == 2
        assert second.evidence_ids == first.evidence_ids
        assert [row["evidence_id"] for row in rows_after_second] == [
            row["evidence_id"] for row in rows_after_first
        ], "reuse writes no new row"

        third = verify_candidate(**kwargs, force_refresh=True)
        rows_after_third = [dict(row) for row in store.evidence_for(run_id, "verification")]

        assert runner.calls == ["unit", "docs-check", "unit", "docs-check"]
        assert runner.cache_hits == 2, "a forced refresh is not a cache hit"
        assert len(rows_after_third) == 4, "the forced run records its own two rows"
        assert [row["evidence_id"] for row in rows_after_third[:2]] == [
            row["evidence_id"] for row in rows_after_first
        ], "the earlier rows are kept, not rewritten"
        assert [row["evidence_id"] for row in rows_after_third[2:]] == third.evidence_ids
        assert set(third.evidence_ids).isdisjoint(first.evidence_ids), (
            "a forced refresh cannot hand back the first round's evidence"
        )
        # The offline runner observed nothing, and says so: a fake pass is not a process fact.
        assert {row["exit_reason"] for row in rows_after_third} == {REASON_NOT_LAUNCHED}
    finally:
        store.close()


def test_a_declared_clean_exit_is_what_an_offline_repair_test_has_to_declare(
    tmp_path: Path,
) -> None:
    """The opt-in that keeps an offline scenario honest: declare the execution fact you model.

    ``FakeCheckRunner`` observes no process, so its default reason is ``not_launched`` and its
    failures can never be classified as business failures. A test that wants the repair gate to
    fire declares ``exit_reasons={"unit": REASON_NONZERO_EXIT}`` - a declared offline scenario, not
    a claim that a real harness produced it.
    """
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"], repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}))
    default_runner = FakeCheckRunner(verdicts={"unit": EvidenceStatus.FAILED})
    declared_runner = FakeCheckRunner(
        verdicts={"unit": EvidenceStatus.FAILED}, exit_reasons={"unit": REASON_NONZERO_EXIT}
    )
    policy = RepairPolicy(check_exit_codes={"unit": [1]})
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        verify_candidate(
            store=store,
            spec=spec,
            project=project,
            project_root=tmp_path,
            project_checks_digest=project.checks_digest(),
            candidate_fingerprint=FINGERPRINT,
            attempt_id=ATTEMPT_ID,
            run_id=run_id,
            runners=CheckRunners({"fake": default_runner}),
        )
        undeclared = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert undeclared[0]["exit_reason"] == REASON_NOT_LAUNCHED
    assert policy.business_failure_for("unit", undeclared[0]["exit_code"]) is True
    assert _may_dispatch_repair(undeclared, policy) is False

    store = Store(tmp_path / "declared.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        verify_candidate(
            store=store,
            spec=spec,
            project=project,
            project_root=tmp_path,
            project_checks_digest=project.checks_digest(),
            candidate_fingerprint=FINGERPRINT,
            attempt_id=ATTEMPT_ID,
            run_id=run_id,
            runners=CheckRunners({"fake": declared_runner}),
        )
        declared = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert declared[0]["exit_reason"] == REASON_NONZERO_EXIT
    assert _may_dispatch_repair(declared, policy) is True, (
        "the declared scenario is the one that may dispatch a repair"
    )


# --------------------------------------------------------------------------
# 2. the reason vocabulary the gate measures is the runner's own
# --------------------------------------------------------------------------


def test_the_reason_vocabulary_is_the_one_the_real_runner_produces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every member of the gate's "ran to completion" set must be produced by the runner itself.

    A set the runner never produces could only ever be reached by a value invented somewhere else,
    and a set that missed the runner's clean-failure value would silently disable every repair.
    Four real endings are produced here: a clean pass, a non-zero exit, a deadline, and an
    incomplete capture.
    """
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")
    clean = runner.run(
        CheckDef(
            id="clean", kind="command", argv=[sys.executable, "-c", "print('ok')"], timeout_seconds=60
        ),
        tmp_path,
        60,
    )
    failed = runner.run(
        CheckDef(
            id="failed",
            kind="command",
            argv=[sys.executable, "-c", "raise SystemExit(3)"],
            timeout_seconds=60,
        ),
        tmp_path,
        60,
    )
    timed_out = runner.run(
        CheckDef(
            id="slow",
            kind="command",
            argv=_helper_argv(tmp_path / "markers", "slow", "--emit-bytes", "1", "--emit-then-hang"),
            timeout_seconds=2,
        ),
        tmp_path,
        2,
    )
    monkeypatch.setattr(
        CommandCheckRunner, "_join_readers", lambda self, readers: False, raising=True
    )
    incomplete = runner.run(
        CheckDef(
            id="incomplete",
            kind="command",
            argv=[sys.executable, "-c", "print('cannot be retained')"],
            timeout_seconds=60,
        ),
        tmp_path,
        60,
    )

    assert clean.status is EvidenceStatus.PASSED and clean.exit_reason == REASON_COMPLETED
    assert failed.status is EvidenceStatus.FAILED and failed.exit_reason == REASON_NONZERO_EXIT
    assert timed_out.status is EvidenceStatus.ERROR and timed_out.exit_reason == REASON_TIMED_OUT
    assert incomplete.status is EvidenceStatus.ERROR
    assert incomplete.exit_reason == REASON_OUTPUT_CAPTURE_ERROR
    assert CLEAN_EXIT_REASONS == {REASON_COMPLETED, REASON_NONZERO_EXIT}
    assert REASON_TIMED_OUT not in CLEAN_EXIT_REASONS
    assert REASON_OUTPUT_CAPTURE_ERROR not in CLEAN_EXIT_REASONS
    assert REASON_SETTLEMENT_FORCED not in CLEAN_EXIT_REASONS
    assert "" not in CLEAN_EXIT_REASONS, "a missing observation is not a clean completion"


@requires_job_object
def test_a_nonzero_exit_that_left_descendants_is_not_a_business_failure(tmp_path: Path) -> None:
    """The case where the exit code alone would buy a repair, and must not.

    A real child exits 3 while the helper it started keeps running inside the owned boundary, so
    the runner reports FAILED with ``settlement_forced`` - the declared exit code is right there.
    Stored and read back through ``failed_check_facts``, the decision is still "no repair": the
    check did not finish cleanly, so its exit code is not an answer about the candidate.
    """
    markers = tmp_path / "markers"
    helper_pid = 0
    runner = CommandCheckRunner(artifact_factory=lambda _check, _eid: tmp_path / "art")
    try:
        outcome = runner.run(
            CheckDef(
                id="unit",
                kind="command",
                argv=_helper_argv(
                    markers,
                    "exits-with-a-helper",
                    "--helper",
                    "--wait-helper",
                    "--linger-after-exit",
                    "--exec",
                    "3",
                ),
                timeout_seconds=60,
            ),
            tmp_path,
            60,
        )
        helper_pid = int(
            _wait_for("exits-with-a-helper-*.helper", markers)
            .read_text(encoding="utf-8")
            .strip()
        )

        assert outcome.status is EvidenceStatus.FAILED
        assert outcome.exit_code == 3
        assert outcome.exit_reason == REASON_SETTLEMENT_FORCED, outcome.detail
    finally:
        _retire(markers, [helper_pid])

    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"])
    policy = RepairPolicy(check_exit_codes={"unit": [3]})
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=outcome.status,
            exit_code=outcome.exit_code,
            exit_reason=outcome.exit_reason,
            detail=_detail_with_references(outcome),
        )
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert facts[0]["exit_reason"] == REASON_SETTLEMENT_FORCED
    assert policy.business_failure_for("unit", 3) is True, "the exit code alone says 'repair'"
    assert _may_dispatch_repair(facts, policy) is False, (
        "the observed lifecycle defect says the check never completed"
    )


# --------------------------------------------------------------------------
# 3. the filter: this attempt, this candidate, and nothing else
# --------------------------------------------------------------------------


def test_failed_check_facts_never_returns_a_previous_round_or_another_candidates_row(
    tmp_path: Path,
) -> None:
    """Round one's failure must not be readable as a fact about round two's candidate.

    Three rows are stored: a failure of round one's attempt, a failure of this attempt against a
    *different* candidate fingerprint, and a failure of this attempt and candidate. Only the third
    may come back - and a passing row never appears at all.
    """
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"])
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        # Another attempt of the same run (the shape a pre-E2 retry left behind: a new revision).
        # Inserted directly because the dispatch gate deliberately owns who may open an attempt;
        # this test is about *reading* old evidence, not about opening one.
        with store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, run_id, task_revision, role, state, reservation_id,
                    reserved_agent_turns, reserved_expires_at, created_at
                ) VALUES ('A-round-one', ?, 2, 'implementer', ?, 'B-round-one', 1,
                          '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')
                """,
                (run_id, AttemptState.FAILED.value),
            )
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
            attempt_id="A-round-one",
            detail="reason=nonzero_exit artifact=round-one.json detail of round one",
        )
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
            fingerprint="sha256:candidate-other",
            detail="reason=nonzero_exit artifact=other.json detail of another candidate",
        )
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.PASSED,
            exit_code=0,
            exit_reason=REASON_COMPLETED,
        )
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
            detail=(
                "reason=nonzero_exit artifact=C:/temp/hflow run 1/artifact.json "
                "stdout: 10/20 bytes truncated=False digest=sha256:aa "
                "stderr: 0/0 bytes truncated=False digest=sha256:bb check unit: exit=1"
            ),
        )
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert len(facts) == 1
    fact = facts[0]
    assert set(fact) == {
        "check_id",
        "status",
        "exit_code",
        "exit_reason",
        "detail",
        "evidence_id",
        "artifact",
        "stdout",
        "stderr",
    }
    assert fact["check_id"] == "unit"
    assert fact["status"] == EvidenceStatus.FAILED.value
    assert fact["exit_reason"] == REASON_NONZERO_EXIT
    assert fact["artifact"] == "C:/temp/hflow run 1/artifact.json", (
        "a path containing a space is one value, not two tokens"
    )
    assert fact["stdout"] == {
        "retained_bytes": "10",
        "total_bytes": "20",
        "truncated": "False",
        "digest": "sha256:aa",
    }
    assert fact["stderr"]["digest"] == "sha256:bb"


def test_an_error_row_is_reported_too_because_mixing_it_in_stops_the_run(
    tmp_path: Path,
) -> None:
    """A helper that hid ERROR rows would make "mixed ERROR" invisible to the caller."""
    project = _project(CheckDef(id="unit", kind="fake"), CheckDef(id="lint", kind="fake"))
    spec = _task(["unit", "lint"])
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
        )
        _record(
            store,
            project,
            run_id,
            check_id="lint",
            status=EvidenceStatus.ERROR,
            exit_code=None,
            exit_reason=REASON_TIMED_OUT,
        )
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert [fact["check_id"] for fact in facts] == ["unit", "lint"], "recorded order is kept"
    assert [fact["status"] for fact in facts] == ["failed", "error"]


# --------------------------------------------------------------------------
# 4. a pre-v5 evidence row has no observed reason, and is not given one
# --------------------------------------------------------------------------


def _build_v1_file(path: Path, spec: TaskSpec) -> None:
    """A genuine pre-versioning file: the module's own v1 DDL, one run and one attempt.

    Built from ``migrate._V1_SCHEMA`` rather than a copy, so the file under test is the shape a
    real pre-E1 database has. It records no storage version, which is what the pre-E1 build
    actually produced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.executescript(migrate._V1_SCHEMA)  # noqa: SLF001 - the DDL a real v1 file has
        connection.execute(
            """
            INSERT INTO runs (
                run_id, project_id, task_id, schema_version, spec_digest, task_spec_json,
                task_revision, task_state, controller_build, checks_digest,
                turn_limit, repair_limit, turns_reserved, repairs_used, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                "legacy-project",
                spec.task_id,
                spec.schema_version,
                spec.spec_digest(),
                spec.model_dump_json(),
                spec.revision,
                TaskState.BLOCKED.value,
                "pre-v5-build",
                "checks-legacy",
                4,
                1,
                1,
                0,
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO attempts (
                attempt_id, run_id, task_revision, role, state, reservation_id,
                reserved_agent_turns, reserved_expires_at, invocation_id, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ATTEMPT_ID,
                RUN_ID,
                spec.revision,
                "implementer",
                AttemptState.FAILED.value,
                "B-legacy",
                1,
                "2026-09-01T01:00:00Z",
                "I-legacy",
                "failed",
                "2026-09-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_a_pre_v5_evidence_row_keeps_an_empty_reason_and_cannot_trigger_a_repair(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    """A genuine pre-v5 file: the reason column is added empty and never back-filled.

    The database is built from the module's own v1 DDL, so its ``evidence`` table has no reason
    column at all - exactly the shape of every row written before this field existed. Opening it
    migrates to the current version; the stored reason must stay empty (a stored exit code is not
    an observation that the process completed cleanly), and the classification must refuse even
    though the policy declares that exit code.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    _build_v1_file(path, task_spec)
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, run_id, attempt_id, kind, status, check_id,
                candidate_fingerprint, checks_digest, exit_code, detail, created_at
            ) VALUES (?, ?, ?, 'verification', 'failed', 'unit', ?, ?, ?, ?, ?)
            """,
            (
                "E-legacy",
                RUN_ID,
                ATTEMPT_ID,
                FINGERPRINT,
                "checks-legacy",
                1,
                "check unit: exit=1 elapsed=0.1s",
                "2026-09-01T00:00:10Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    policy = RepairPolicy(check_exit_codes={"unit": [1]})
    store = Store(path)
    try:
        assert store.storage_version == migrate.STORAGE_VERSION >= 5
        assert store.migrated_from == 1, (
            "a file that never recorded a version is read as v1 by its shape, and the snapshot's "
            ".pre-v1.bak name says the same thing"
        )
        row = dict(store.evidence_for(RUN_ID, "verification")[0])
        record = store.evidence_records_for(RUN_ID)[0]
        facts = failed_check_facts(
            store,
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            candidate_fingerprint=FINGERPRINT,
            checks_digest="checks-legacy",
        )
    finally:
        store.close()

    assert row["exit_reason"] == "", "the migration must not invent a reason for an old row"
    assert record.exit_reason == ""
    assert facts[0]["exit_reason"] == ""
    assert policy.business_failure_for("unit", 1) is True, "the historical code is declared..."
    assert _may_dispatch_repair(facts, policy) is False, (
        "...and it still cannot repair: no reason was ever observed"
    )


def _migrate_to_v4(path: Path) -> None:
    """Bring a fresh v1 file to v4 exactly as an E1 build would, and stop there."""
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        before, now, _backup = migrate.migrate(connection, path, supported=4)
    finally:
        connection.close()
    assert (before, now) == (1, 4)


def test_a_version_less_v4_file_is_read_by_shape_newest_first(tmp_path: Path) -> None:
    """Shape detection reads the newest layout first, so a v4 file is never re-migrated as v1.

    The file is a genuine v4 database whose version row is then removed - the fallback path the
    module keeps for a file whose bootstrap never stamped a version. Reading it as v1 would try to
    re-add the v2 columns (``ALTER TABLE ADD COLUMN`` has no ``IF NOT EXISTS``) and fail on a
    healthy file; reading it as v5 would skip the steps it still needs.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    task = _task(["unit"])
    _build_v1_file(path, task)
    _migrate_to_v4(path)

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("DELETE FROM schema_meta WHERE key = 'storage_version'")
        connection.commit()
        assert migrate.effective_version(connection) == 4
    finally:
        connection.close()

    reopened = Store(path)
    try:
        assert reopened.storage_version == migrate.STORAGE_VERSION
        assert reopened.migrated_from == 4, "a v4 shape must be read as v4, not as version 1"
        assert "exit_reason" in {
            str(row[1]) for row in reopened.conn.execute("PRAGMA table_info(evidence)")
        }
        assert "is_repair" in {
            str(row[1]) for row in reopened.conn.execute("PRAGMA table_info(attempts)")
        }
        assert reopened.repair_records_for(RUN_ID) == []
        attempts = [dict(row) for row in reopened.attempts_for(RUN_ID)]
        assert [row["attempt_id"] for row in attempts] == [ATTEMPT_ID], (
            "the v4 file's attempt row is still there after the rebuild"
        )
        assert attempts[0]["is_repair"] == 0
    finally:
        reopened.close()


def test_the_v5_attempts_rebuild_keeps_rows_columns_indexes_and_foreign_keys(
    tmp_path: Path,
) -> None:
    """A table rebuild is where a silent column loss or a dangling reference would hide.

    A v4 file with a real ``invocations`` row and a real ``evidence`` row pointing at its attempt
    is migrated to v5. Afterwards the schema must have exactly the expected columns, the index and
    the two child tables must still reference ``attempts``, ``PRAGMA foreign_key_check`` must be
    clean, and both child rows must still resolve their ``attempt_id`` - read from a *separate*
    connection, so the assertion is about the file rather than about the writing connection.
    """
    path = tmp_path / "data" / "hflow.sqlite"
    task = _task(["unit"])
    _build_v1_file(path, task)
    _migrate_to_v4(path)

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO invocations (
                invocation_id, root_id, run_id, attempt_id, role, state, reserved_at,
                root_used_at_reservation, authorization_used_at_reservation
            ) VALUES ('I-v4', '', ?, ?, 'implementer', 'settled', '2026-09-01T00:00:00Z', 0, 0)
            """,
            (RUN_ID, ATTEMPT_ID),
        )
        connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, run_id, attempt_id, kind, status, check_id,
                candidate_fingerprint, checks_digest, exit_code, detail, created_at
            ) VALUES ('E-v4', ?, ?, 'verification', 'passed', 'unit', ?, 'checks-legacy', 0,
                      'check unit: exit=0', '2026-09-01T00:00:05Z')
            """,
            (RUN_ID, ATTEMPT_ID, FINGERPRINT),
        )
        connection.commit()
    finally:
        connection.close()

    store = Store(path)
    try:
        assert store.storage_version == migrate.STORAGE_VERSION
        assert store.migrated_from == 4
    finally:
        store.close()

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == [], (
            "a rebuild that left a dangling reference must not reach a committed file"
        )
        columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(attempts)")]
        assert columns == [*migrate._V5_ATTEMPTS_COLUMNS, "is_repair"], (  # noqa: SLF001
            "a rebuild is the one migration where a dropped column stays invisible until later"
        )
        assert [row["attempt_id"] for row in connection.execute("SELECT * FROM attempts")] == [
            ATTEMPT_ID
        ], "the attempt row survived the rebuild"
        assert connection.execute(
            "SELECT is_repair FROM attempts WHERE attempt_id = ?", (ATTEMPT_ID,)
        ).fetchone()[0] == 0, "an attempt written before v5 is a first attempt, not a repair"
        # Both child rows still resolve the attempt, in the file, on a fresh connection.
        assert connection.execute(
            "SELECT i.invocation_id FROM invocations i JOIN attempts a USING (attempt_id)"
        ).fetchall()[0]["invocation_id"] == "I-v4"
        assert connection.execute(
            "SELECT e.evidence_id FROM evidence e JOIN attempts a USING (attempt_id)"
        ).fetchall()[0]["evidence_id"] == "E-v4"
        # The child tables still *name* ``attempts``: a rename of the old table would have
        # rewritten their REFERENCES clauses to the temporary name instead.
        child_sql = " ".join(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name IN ('evidence', 'invocations')"
            )
        )
        assert "attempts_v5" not in child_sql
        assert "REFERENCES attempts(attempt_id)" in child_sql
        assert "attempts_v5" not in {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")
        }, "the temporary table must not survive the migration"
        assert connection.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'ix_attempts_run'"
        ).fetchone()[0] == "attempts", "the v1 index follows the rebuilt table"
        assert "UNIQUE (run_id, task_revision, role, is_repair)" in str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'attempts'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 5. repair decisions are persisted as the documents they are
# --------------------------------------------------------------------------


def test_repair_records_round_trip_in_order_and_keep_every_refusal(tmp_path: Path) -> None:
    """One row per decision, validated on read, in the order the run decided them."""
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"])
    refused = RepairRecord(
        decision=RepairDecision.NOT_A_BUSINESS_FAILURE,
        trigger=RepairTrigger.BUSINESS_CHECK_FAILED,
        reason="the check timed out; a deadline is not an answer about the candidate",
        policy_digest="sha256:policy",
        failed_checks=["unit"],
        exit_codes={"unit": None},
        round=1,
        decided_at="2026-09-25T10:00:00Z",
    )
    allowed = RepairRecord(
        decision=RepairDecision.ALLOWED,
        trigger=RepairTrigger.BUSINESS_CHECK_FAILED,
        reason="check unit exited 1, which the policy declares a business assertion failure",
        policy_digest="sha256:policy",
        failed_checks=["unit"],
        exit_codes={"unit": 1},
        round=2,
        decided_at="2026-09-25T10:00:01Z",
    )
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        store.record_repair_record(run_id, refused)
        store.record_repair_record(run_id, allowed)
        # A repeated identical decision is a second recorded call, not a silently dropped one.
        store.record_repair_record(run_id, refused)
        records = store.repair_records_for(run_id)
        other_run = store.repair_records_for("R-somewhere-else")
        with pytest.raises(RunNotFound):
            store.record_repair_record("R-does-not-exist", refused)
    finally:
        store.close()

    assert [record.decision for record in records] == [
        RepairDecision.NOT_A_BUSINESS_FAILURE,
        RepairDecision.ALLOWED,
        RepairDecision.NOT_A_BUSINESS_FAILURE,
    ]
    assert records[0] == refused, "the stored document is the record, verbatim"
    assert records[1].exit_codes == {"unit": 1}
    assert records[0].decided_at == "2026-09-25T10:00:00Z"
    assert other_run == []


def test_an_unreadable_repair_record_is_reported_rather_than_skipped(tmp_path: Path) -> None:
    """A record that cannot be read is a fact that cannot be read - not a fact that is not there."""
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"])
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO run_repair_records (record_id, run_id, record_json, created_at) "
                "VALUES ('REC-1', ?, '{not json', '2026-09-25T10:00:00Z')",
                (run_id,),
            )
        with pytest.raises(StoreError) as raised:
            store.repair_records_for(run_id)
    finally:
        store.close()

    assert "repair record" in str(raised.value)


# --------------------------------------------------------------------------
# 6. the classification table (plan §7 item 10)
# --------------------------------------------------------------------------
#
# Every row below is an ending that must NOT buy a second implementer. Each case is stored as
# real evidence and read back through ``failed_check_facts``, and the decision goes through
# ``RepairPolicy.business_failure_for`` - so the table asserts the plan's rule, not a boolean
# private to this test. The positive control (a clean, declared business failure) is asserted
# separately below, so the table cannot pass by never allowing anything.


def _v(
    check_id: str, status: EvidenceStatus, exit_code: int | None, exit_reason: str
) -> tuple[str, str, EvidenceStatus, int | None, str]:
    return ("verification", check_id, status, exit_code, exit_reason)


def _review(
    status: EvidenceStatus, exit_reason: str = ""
) -> tuple[str, str, EvidenceStatus, int | None, str]:
    return ("review", "review", status, None, exit_reason)


CLASSIFICATION_CASES = [
    pytest.param(
        [
            _v("unit", EvidenceStatus.FAILED, 1, REASON_NONZERO_EXIT),
            _v("lint", EvidenceStatus.ERROR, None, REASON_TIMED_OUT),
        ],
        {"check_exit_codes": {"unit": [1]}},
        2,
        False,
        id="mixed-error-with-a-declared-business-failure",
    ),
    pytest.param(
        [_v("unit", EvidenceStatus.FAILED, 7, REASON_NONZERO_EXIT)],
        {"check_exit_codes": {"unit": [1]}},
        1,
        False,
        id="undeclared-exit-code",
    ),
    pytest.param(
        [_v("unit", EvidenceStatus.ERROR, None, REASON_TIMED_OUT)],
        {"check_exit_codes": {"unit": [1]}},
        1,
        False,
        id="timeout",
    ),
    pytest.param(
        [_v("unit", EvidenceStatus.FAILED, 1, REASON_SETTLEMENT_FORCED)],
        {"check_exit_codes": {"unit": [1]}},
        1,
        False,
        id="leftover-descendants",
    ),
    pytest.param(
        [_v("unit", EvidenceStatus.ERROR, 0, REASON_OUTPUT_CAPTURE_ERROR)],
        {"check_exit_codes": {"unit": [1]}},
        1,
        False,
        id="capture-error",
    ),
    pytest.param(
        [_v("unit", EvidenceStatus.FAILED, 1, "")],
        {"check_exit_codes": {"unit": [1]}},
        1,
        False,
        id="legacy-row-with-no-observed-reason",
    ),
    pytest.param(
        [],
        {"check_exit_codes": {"unit": [1]}},
        0,
        False,
        id="unknown-outcome-no-check-ever-ran",
    ),
    pytest.param(
        [],
        {"check_exit_codes": {"unit": [1]}},
        0,
        True,
        id="cancelled-no-check-ever-ran",
    ),
    pytest.param(
        [_review(EvidenceStatus.FAILED)],
        {"check_exit_codes": {"unit": [1]}, "allow_reviewer_changes": True},
        0,
        False,
        id="review-changes-requested-with-empty-findings",
    ),
    pytest.param(
        [_review(EvidenceStatus.ERROR)],
        {"check_exit_codes": {"unit": [1]}, "allow_reviewer_changes": True},
        0,
        False,
        id="malformed-review",
    ),
]


@pytest.mark.parametrize("rows, policy_kwargs, expected_facts, cancelled", CLASSIFICATION_CASES)
def test_no_other_ending_may_trigger_a_repair(
    tmp_path: Path,
    rows: list[tuple[str, str, EvidenceStatus, int | None, str]],
    policy_kwargs: dict[str, object],
    expected_facts: int,
    cancelled: bool,
) -> None:
    project = _project(CheckDef(id="unit", kind="fake"), CheckDef(id="lint", kind="fake"))
    spec = _task(["unit", "lint"])
    policy = RepairPolicy(**policy_kwargs)  # type: ignore[arg-type]
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        for kind, check_id, status, exit_code, exit_reason in rows:
            _record(
                store,
                project,
                run_id,
                check_id=check_id,
                status=status,
                exit_code=exit_code,
                exit_reason=exit_reason,
                kind=kind,
            )
        if cancelled:
            # A stop is recorded before anything else happens, and a stopped run finishes no
            # check: there is no fact here to classify in the first place.
            store.record_cancel_intent(run_id)
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert len(facts) == expected_facts, "the fact set is the one the case describes"
    assert _may_dispatch_repair(facts, policy) is False, (
        "this ending must not buy a second implementer"
    )


def test_a_clean_declared_business_failure_is_the_one_ending_that_may_repair(
    tmp_path: Path,
) -> None:
    """The positive control for the table: with everything clean and declared, repair may fire."""
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"], repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}))
    policy = RepairPolicy(check_exit_codes={"unit": [1]})
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
        )
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert len(facts) == 1
    assert policy.business_failure_for("unit", 1) is True
    assert _may_dispatch_repair(facts, policy) is True


def test_a_run_without_a_policy_can_never_repair_however_clean_the_failure(
    tmp_path: Path,
) -> None:
    """The historical ``max_repair_cycles=1`` spends nothing: no policy, no repair."""
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"])  # budget.max_repair_cycles defaults to 1; repair_policy is absent
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_checking_run(store, project, spec)
        _record(
            store,
            project,
            run_id,
            check_id="unit",
            status=EvidenceStatus.FAILED,
            exit_code=1,
            exit_reason=REASON_NONZERO_EXIT,
        )
        facts = _facts_for(store, project, run_id)
    finally:
        store.close()

    assert spec.repair_policy is None
    assert spec.budget.max_repair_cycles == 1
    assert _may_dispatch_repair(facts, None) is False


# --------------------------------------------------------------------------
# 7. the repair attempt row (storage v5)
# --------------------------------------------------------------------------
#
# A run buys one first attempt and at most one repair attempt, on the same revision, from the
# frozen candidate. Before v5 the schema itself made that impossible: ``attempts`` carried
# ``UNIQUE (run_id, task_revision, role)``. These tests pin the rebuilt table and the dispatch
# path that now uses it.

EXPIRES_AT = "2999-01-01T00:00:00Z"


def _seed_ready_run(
    store: Store,
    project: ProjectConfig,
    spec: TaskSpec,
    *,
    run_id: str = RUN_ID,
    turn_limit: int = 4,
) -> str:
    """A claimed READY run with no attempt yet: the state a dispatch starts from."""
    run = store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="e2-verify-test",
        checks_digest=project.checks_digest(),
        turn_limit=turn_limit,
        repair_limit=1,
    )
    store.claim_run(run["run_id"], "local-controller")
    store.set_task_state(run["run_id"], [TaskState.DRAFT], TaskState.READY)
    return str(run["run_id"])


def _dispatch(
    store: Store,
    run_id: str,
    *,
    invocation_id: str,
    role: str = "implementer",
    is_repair: bool = False,
    attempt_id: str | None = None,
):
    """One dispatch through the real reservation path, with no root ledger involved.

    The legacy (root-less) path is the one an offline run uses, and it is the stricter one for
    this test: it has no invocation row to fall back on, so ``is_repair`` can only reach the
    attempt row itself.
    """
    return store.reserve_dispatch(
        run_id=run_id,
        controller_id="local-controller",
        invocation_id=invocation_id,
        role=role,
        reservation_id=f"B-{invocation_id}",
        reserved_turns=1,
        reservation_expires_at=EXPIRES_AT,
        attempt_id=attempt_id,
        is_repair=is_repair,
    )


def test_a_repair_attempt_is_a_second_row_and_a_third_cannot_be_inserted(
    tmp_path: Path,
) -> None:
    """The plan's "one repair" is enforced in words and in the schema.

    A second *first* attempt is refused as a repair-shaped request; the repair attempt becomes its
    own row marked ``is_repair``; a second repair is refused by the dispatch path, and a raw insert
    that tries to bypass it is refused by the table's own unique key.
    """
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"], repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}))
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_ready_run(store, project, spec)
        first = _dispatch(store, run_id, invocation_id="I-1")
        store.finish_attempt(
            run_id=run_id,
            attempt_id=first.attempt_id,
            state=AttemptState.FAILED,
            outcome=InvocationOutcome.COMPLETED,
            result={"round": 1},
        )

        with pytest.raises(StoreError, match="first implementation attempt"):
            _dispatch(store, run_id, invocation_id="I-2", is_repair=False)

        repair = _dispatch(store, run_id, invocation_id="I-2", is_repair=True)
        assert repair.attempt_id != first.attempt_id, "a repair is its own attempt row"
        assert repair.invocation is None, "the root-less path writes no invocation row"
        rows = {row["attempt_id"]: dict(row) for row in store.attempts_for(run_id)}
        assert rows[first.attempt_id]["is_repair"] == 0
        assert rows[repair.attempt_id]["is_repair"] == 1
        assert rows[repair.attempt_id]["task_revision"] == rows[first.attempt_id]["task_revision"], (
            "the repair stays on the same revision; it does not silently become a new one"
        )
        assert store.get_run(run_id)["current_attempt_id"] == repair.attempt_id

        with pytest.raises(StoreError, match="repair attempt"):
            _dispatch(store, run_id, invocation_id="I-3", is_repair=True)

        # And the schema refuses a third attempt even if the dispatch path were bypassed.
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, run_id, task_revision, role, state, created_at, root_id,
                        is_repair
                    ) VALUES ('A-forced', ?, ?, 'implementer', 'ACTIVE', ?, '', 1)
                    """,
                    (run_id, spec.revision, "2026-09-25T00:00:00Z"),
                )
        assert len(store.attempts_for(run_id)) == 2
    finally:
        store.close()


def test_a_reviewer_attaches_to_the_attempt_the_run_is_working_on(tmp_path: Path) -> None:
    """Round two's reviewer reviews round two's candidate, not round one's.

    Both attempts are on the same revision, so "the first row for this revision" would silently
    record round two's verdict against round one's attempt. The reviewer resolves the run's
    *current* attempt instead, and each round's review invocation id stays on its own row.
    """
    project = _project(CheckDef(id="unit", kind="fake"), review_required=True)
    spec = _task(
        ["unit"],
        review_required=True,
        repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}),
    )
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_ready_run(store, project, spec, turn_limit=6)
        first = _dispatch(store, run_id, invocation_id="I-1")
        store.finish_attempt(
            run_id=run_id,
            attempt_id=first.attempt_id,
            state=AttemptState.FAILED,
            outcome=InvocationOutcome.COMPLETED,
            result={"round": 1},
        )
        store.set_task_state(
            run_id, [TaskState.RUNNING], TaskState.CHECKING, phase=CheckPhase.REVIEW
        )
        review_one = _dispatch(store, run_id, role="reviewer", invocation_id="I-r1")
        assert review_one.attempt_id == first.attempt_id
        # A root-less dispatch writes no invocation row, so the controller's own stop-aware
        # registration is what puts the invocation id on the attempt row (see
        # ``register_attempt_invocation_unless_stopped``). Done here for both rounds, because the
        # "one review per attempt" rule reads that column.
        store.record_invocation(first.attempt_id, "I-1")
        assert store.register_attempt_invocation_unless_stopped(
            run_id, first.attempt_id, column="review_invocation_id", invocation_id="I-r1"
        )

        # The repair handoff returns the run to RUNNING with no phase before the second
        # implementer attempt: an implementer dispatch is only reserved before the candidate is
        # checked.
        store.set_task_state(
            run_id, [TaskState.CHECKING], TaskState.RUNNING, clear_phase=True
        )
        repair = _dispatch(store, run_id, invocation_id="I-2", is_repair=True)
        store.finish_attempt(
            run_id=run_id,
            attempt_id=repair.attempt_id,
            state=AttemptState.FAILED,
            outcome=InvocationOutcome.COMPLETED,
            result={"round": 2},
        )
        store.set_task_state(
            run_id, [TaskState.RUNNING], TaskState.CHECKING, phase=CheckPhase.REVIEW
        )
        review_two = _dispatch(store, run_id, role="reviewer", invocation_id="I-r2")

        assert review_two.attempt_id == repair.attempt_id, (
            "round two's verdict must belong to round two's attempt"
        )
        store.record_invocation(repair.attempt_id, "I-2")
        assert store.register_attempt_invocation_unless_stopped(
            run_id, repair.attempt_id, column="review_invocation_id", invocation_id="I-r2"
        )
        rows = {row["attempt_id"]: dict(row) for row in store.attempts_for(run_id)}
        assert rows[first.attempt_id]["review_invocation_id"] == "I-r1"
        assert rows[repair.attempt_id]["review_invocation_id"] == "I-r2"

        # One review per attempt: a third reviewer id is refused, whichever attempt it names.
        with pytest.raises(StoreError, match="one review per candidate"):
            _dispatch(store, run_id, role="reviewer", invocation_id="I-r3")
    finally:
        store.close()


def test_reopen_for_repair_moves_the_phase_back_without_resetting_anything(
    tmp_path: Path,
) -> None:
    """The one exception to "an implementer is only reserved before the checks" - and its limits.

    While the run is in ``verification`` a second implementer is refused, which is the E1 rule.
    ``reopen_for_repair`` is the transaction that makes the single repair possible: the phase goes
    back to none while the run stays live, and nothing is reset - not the current attempt (the
    previous round's reviewer attached to it), not the reserved turns, not the repair counter.
    """
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"], repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}))
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_ready_run(store, project, spec, turn_limit=6)
        first = _dispatch(store, run_id, invocation_id="I-1")
        store.finish_attempt(
            run_id=run_id,
            attempt_id=first.attempt_id,
            state=AttemptState.FAILED,
            outcome=InvocationOutcome.COMPLETED,
            result={"round": 1},
        )
        store.set_task_state(
            run_id, [TaskState.RUNNING], TaskState.CHECKING, phase=CheckPhase.VERIFICATION
        )

        with pytest.raises(StoreError, match="only reserved before the candidate is checked"):
            _dispatch(store, run_id, invocation_id="I-2", is_repair=True)

        before = dict(store.get_run(run_id))
        reopened = store.reopen_for_repair(run_id, "local-controller")

        assert reopened["task_state"] == TaskState.RUNNING.value
        assert reopened["phase"] is None
        assert reopened["current_attempt_id"] == first.attempt_id, (
            "the previous round's reviewer attached to that row; a repair does not erase it"
        )
        assert reopened["turns_reserved"] == before["turns_reserved"], "a repair is not a refund"
        assert reopened["repairs_used"] == before["repairs_used"], (
            "the repair is charged once, by the dispatch that buys it"
        )

        repair = _dispatch(store, run_id, invocation_id="I-2", is_repair=True)
        assert repair.attempt_id != first.attempt_id
    finally:
        store.close()


@pytest.mark.parametrize(
    "refusal, message",
    [
        pytest.param("owner", "claimed by", id="another-controller-owns-the-run"),
        pytest.param("cancelled", "cancellation intent", id="a-stop-was-recorded"),
        pytest.param("blocked", "the run is BLOCKED", id="a-terminal-decision-ended-the-run"),
    ],
)
def test_reopen_for_repair_refuses_a_run_a_stop_or_a_verdict_already_ended(
    tmp_path: Path, refusal: str, message: str
) -> None:
    """A repair is a decision inside a live run, never a resurrection command."""
    project = _project(CheckDef(id="unit", kind="fake"))
    spec = _task(["unit"], repair_policy=RepairPolicy(check_exit_codes={"unit": [1]}))
    store = Store(tmp_path / "hflow.sqlite")
    try:
        run_id = _seed_ready_run(store, project, spec)
        if refusal == "cancelled":
            store.record_cancel_intent(run_id)
        elif refusal == "blocked":
            store.set_blocked(run_id, RefusalCode.INTERNAL_ERROR, "the attempt failed")
        controller_id = "another-controller" if refusal == "owner" else "local-controller"

        with pytest.raises(StoreError, match=message):
            store.reopen_for_repair(run_id, controller_id)

        row = dict(store.get_run(run_id))
        assert row["phase"] is None, "a refused reopen writes nothing"
        if refusal == "owner":
            assert row["task_state"] == TaskState.READY.value
        elif refusal == "cancelled":
            assert row["cancel_intent_at"], "the stop is still recorded"
        else:
            assert row["task_state"] == TaskState.BLOCKED.value
    finally:
        store.close()
