"""Batch I2: the ``integrations`` record (storage v8) and the store's compare-and-set around it.

Covers the store layer only - no Git, no checks, no CLI: what may be recorded for which run, the
one-active-per-run and one-applying-per-target rules, the CAS discipline of every transition, the
receipt written only with ``integrated``, and the v7 -> v8 migration of an existing ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from hflow import migrate
from hflow.contracts import (
    INTEGRATION_ACTIVE_STATES,
    INTEGRATION_TERMINAL_STATES,
    CandidateSnapshot,
    CheckPhase,
    DeliveryState,
    EvidenceRecord,
    EvidenceStatus,
    IntegrationReceipt,
    IntegrationRecord,
    IntegrationState,
    InvocationOutcome,
    RefusalCode,
    ResultReceipt,
    ReviewResult,
    TaskSpec,
    TaskState,
    UsageFacts,
    VerificationResult,
    canonical_json,
)
from hflow.ids import new_integration_id, utc_now
from hflow.store import (
    NOTE_INTEGRATION,
    IntegrationConflict,
    IntegrationNotFound,
    Store,
    StoreError,
)
from tests import test_batch_e_migration as migration_helpers

BASE = "b" * 40
CANDIDATE = "c" * 40
CANDIDATE_TREE = "d" * 40
MOVED_TIP = "e" * 40
MERGED = "f" * 40
MERGED_TREE = "a" * 40
COMMON_DIR = "C:/repo/.git"
TARGET = "refs/heads/main"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _spec(task_spec: TaskSpec, suffix: str) -> TaskSpec:
    """A distinct task (a distinct spec digest), so one ledger can hold several runs."""
    return task_spec.model_copy(update={"task_id": f"T-{suffix}"})


def _ready_run(store: Store, project, spec: TaskSpec, run_id: str) -> str:
    store.create_run(
        run_id=run_id,
        project_id=project.project_id,
        spec=spec,
        spec_digest=spec.spec_digest(),
        controller_build="integration-store-test",
        checks_digest=project.checks_digest(),
        turn_limit=4,
        repair_limit=0,
    )
    store.claim_run(run_id, "local-controller")
    store.set_task_state(run_id, [TaskState.DRAFT], TaskState.READY)
    return run_id


def _accepted_run(
    store: Store, project, spec: TaskSpec, run_id: str, *, git_commit: str = CANDIDATE
) -> str:
    """An ACCEPTED / LOCAL_CANDIDATE run with a receipt, through the store's own transitions."""
    _ready_run(store, project, spec, run_id)
    attempt_id = f"A-{run_id}"
    store.dispatch_attempt(
        run_id=run_id,
        controller_id="local-controller",
        attempt_id=attempt_id,
        role="implementer",
        reservation_id=f"B-{run_id}",
        reserved_turns=1,
        reservation_expires_at="2999-01-01T00:00:00Z",
    )
    store.record_invocation(attempt_id, f"I-{run_id}")
    store.advance_to_checking(run_id=run_id, attempt_id=attempt_id, phase=CheckPhase.VERIFICATION)
    store.record_evidence(
        evidence_id=f"E-{run_id}",
        run_id=run_id,
        attempt_id=attempt_id,
        kind="verification",
        status=EvidenceStatus.PASSED,
        candidate_fingerprint="sha256:fp",
        checks_digest=project.checks_digest(),
        check_id="unit",
    )
    receipt = ResultReceipt(
        run_id=run_id,
        task_id=spec.task_id,
        attempt_id=attempt_id,
        task_revision=spec.revision,
        runtime_build="integration-store-test",
        plan_digest=spec.spec_digest(),
        harness_outcome=InvocationOutcome.COMPLETED,
        candidate=CandidateSnapshot(
            base_commit=BASE, git_commit=git_commit, git_tree=CANDIDATE_TREE,
            fingerprint="sha256:fp",
        ),
        verification=VerificationResult(status="passed", evidence_ids=[f"E-{run_id}"]),
        review=ReviewResult(status="not_required"),
        task_state=TaskState.ACCEPTED,
        delivery_state=DeliveryState.LOCAL_CANDIDATE,
        usage=UsageFacts(),
        candidate_paths=["src/parser.py"],
    )
    store.finalize_acceptance(run_id, receipt, checks_digest=project.checks_digest())
    return run_id


def _record(
    store: Store,
    run_id: str,
    *,
    target_ref: str = TARGET,
    git_common_dir: str = COMMON_DIR,
    **overrides,
) -> IntegrationRecord:
    run = store.get_run(run_id)
    receipt = ResultReceipt.model_validate_json(run["receipt_json"])
    fields = {
        "integration_id": new_integration_id(),
        "run_id": run_id,
        "task_id": str(run["task_id"]),
        "attempt_id": receipt.attempt_id,
        "git_common_dir": git_common_dir,
        "repo_root": "C:/repo",
        "target_ref": target_ref,
        "target_tip": BASE,
        "base_commit": receipt.candidate.base_commit,
        "candidate_commit": receipt.candidate.git_commit,
        "state": IntegrationState.PREPARING,
        "checks_digest": str(run["checks_digest"]),
        "owner_pid": 4242,
        "owner_created": 133_000_000_000_000_000,
        "owner_host": "host-a",
        "created_at": "2000-01-01T00:00:00Z",
        "updated_at": "2000-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return IntegrationRecord(**fields)


def _to_ready(store: Store, integration_id: str) -> IntegrationRecord:
    store.update_integration(
        integration_id,
        expected=IntegrationState.PREPARING,
        state=IntegrationState.CHECKING,
        mode="squash",
        integration_commit=MERGED,
        integration_tree=MERGED_TREE,
        integration_ref=f"refs/hflow/integrations/x/{integration_id}",
        paths=["src/parser.py"],
        worktree_path="C:/repo.hflow-worktrees/" + integration_id,
        worktree_state="PRESENT",
    )
    return store.update_integration(
        integration_id,
        expected=IntegrationState.CHECKING,
        state=IntegrationState.READY,
        fingerprint="sha256:integration-tree",
        evidence_ids=["E-int-1"],
        worktree_state="REMOVED",
    )


def _to_applying(store: Store, integration_id: str) -> IntegrationRecord:
    _to_ready(store, integration_id)
    return store.update_integration(
        integration_id,
        expected=IntegrationState.READY,
        state=IntegrationState.APPLYING,
        apply_intent_at=utc_now(),
        applied_by="operator",
    )


def _receipt(record: IntegrationRecord, **overrides) -> IntegrationReceipt:
    fields = {
        "integration_id": record.integration_id,
        "run_id": record.run_id,
        "task_id": record.task_id,
        "attempt_id": record.attempt_id,
        "runtime_build": "integration-store-test",
        "candidate": CandidateSnapshot(
            base_commit=record.base_commit, git_commit=record.candidate_commit,
            git_tree=CANDIDATE_TREE, fingerprint="sha256:fp",
        ),
        "target_ref": record.target_ref,
        "target_tip_before": record.target_tip,
        "integration_commit": MERGED,
        "integration_tree": MERGED_TREE,
        "mode": "squash",
        "paths": ["src/parser.py"],
        "verification": VerificationResult(status="passed", evidence_ids=["E-int-1"]),
        "delivery_state": DeliveryState.INTEGRATED,
        "basis": "hflow_ref_update",
        "integrated_at": "2026-10-06T12:00:00Z",
        "applied_by": "operator",
    }
    fields.update(overrides)
    return IntegrationReceipt(**fields)


def _integration_notes(store: Store, run_id: str) -> list[str]:
    return [note for note in store.notes_for(run_id) if note.startswith(f"{NOTE_INTEGRATION}: ")]


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------


def test_state_vocabulary_partitions_into_active_ready_and_terminal() -> None:
    active = set(INTEGRATION_ACTIVE_STATES)
    terminal = set(INTEGRATION_TERMINAL_STATES)
    assert not active & terminal
    assert set(IntegrationState) == active | terminal | {IntegrationState.READY}
    assert new_integration_id().startswith("G-"), "I- is taken by invocations"
    assert {
        RefusalCode.NOT_INTEGRABLE,
        RefusalCode.INTEGRATION_CONFLICT,
        RefusalCode.TARGET_MOVED,
        RefusalCode.TARGET_CHECKED_OUT,
    } <= set(RefusalCode)


@pytest.mark.parametrize(
    "state", [DeliveryState.NONE, DeliveryState.LOCAL_CANDIDATE, DeliveryState.PUBLISHED]
)
def test_an_integration_receipt_is_only_ever_integrated(
    store: Store, project, task_spec: TaskSpec, state: DeliveryState
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-rc")
    record = _record(store, run_id)
    with pytest.raises(ValidationError, match="INTEGRATED"):
        _receipt(record, delivery_state=state)
    assert _receipt(record).delivery_state is DeliveryState.INTEGRATED


def test_the_mode_vocabulary_is_squash_or_replayed(store: Store, project, task_spec) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-mode")
    with pytest.raises(ValidationError):
        _record(store, run_id, mode="fast_forward")
    assert _record(store, run_id, mode="replayed").mode == "replayed"


def test_integration_check_evidence_validates_and_reads_back(
    store: Store, project, task_spec: TaskSpec
) -> None:
    """``inspect_run`` validates every evidence row; an integrated run's rows must still read."""
    record = EvidenceRecord(
        evidence_id="E-ic",
        run_id="R-x",
        attempt_id="A-x",
        kind="integration-check",
        status=EvidenceStatus.PASSED,
    )
    assert record.kind == "integration-check"
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="E-bad", run_id="R-x", attempt_id="A-x", kind="integration",
            status=EvidenceStatus.PASSED,
        )

    run_id = _accepted_run(store, project, task_spec, "R-ic")
    store.record_evidence(
        evidence_id="E-int-1",
        run_id=run_id,
        attempt_id=f"A-{run_id}",
        kind="integration-check",
        status=EvidenceStatus.PASSED,
        candidate_fingerprint="sha256:integration-tree",
        checks_digest=project.checks_digest(),
        check_id="unit",
    )
    kinds = [row.kind for row in store.evidence_records_for(run_id)]
    assert kinds == ["verification", "integration-check"]


# --------------------------------------------------------------------------
# create / read
# --------------------------------------------------------------------------


def test_create_get_and_list(store: Store, project, task_spec: TaskSpec) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-cr")
    record = _record(store, run_id)

    created = store.create_integration(record)

    assert created.state is IntegrationState.PREPARING
    assert created.integration_id == record.integration_id
    assert created.created_at == created.updated_at != "2000-01-01T00:00:00Z", (
        "the store stamps the creation time"
    )
    assert created.model_dump(exclude={"created_at", "updated_at"}) == record.model_dump(
        exclude={"created_at", "updated_at"}
    )
    assert store.integration(record.integration_id) == created
    assert store.integrations_for(run_id) == [created]
    assert store.integrations_for("R-none") == []
    assert store.integration("G-missing") is None
    assert store.integration_receipt(record.integration_id) is None
    assert _integration_notes(store, run_id) == [
        f"integration: {record.integration_id} prepared against {TARGET} at {BASE}"
    ]


def test_create_is_refused_for_a_run_that_is_not_accepted(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-acc")
    ready = _ready_run(store, project, _spec(task_spec, "ready"), "R-ready")
    record = _record(store, run_id).model_copy(update={"run_id": ready})

    with pytest.raises(IntegrationConflict, match="R-ready is READY, not ACCEPTED"):
        store.create_integration(record)
    assert store.integrations_for(ready) == []
    assert _integration_notes(store, ready) == []

    with pytest.raises(IntegrationConflict, match="does not exist"):
        store.create_integration(record.model_copy(update={"run_id": "R-missing"}))


def test_create_is_refused_for_an_accepted_run_without_a_receipt(
    store: Store, project, task_spec: TaskSpec
) -> None:
    template = _record(store, _accepted_run(store, project, task_spec, "R-tpl"))
    run_id = _ready_run(store, project, _spec(task_spec, "norec"), "R-norec")
    store.set_task_state(run_id, [TaskState.READY], TaskState.ACCEPTED)
    assert store.get_run(run_id)["receipt_json"] is None

    with pytest.raises(IntegrationConflict, match="records no delivery receipt"):
        store.create_integration(template.model_copy(update={"run_id": run_id}))
    assert store.integrations_for(run_id) == []


def test_create_is_refused_for_a_run_whose_receipt_has_no_git_candidate(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-nogit", git_commit="")
    with pytest.raises(IntegrationConflict, match="no Git candidate commit"):
        store.create_integration(_record(store, run_id))
    assert store.integrations_for(run_id) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_commit", "9" * 40),
        ("base_commit", "9" * 40),
        ("attempt_id", "A-other"),
        ("task_id", "T-other"),
        ("checks_digest", "sha256:other"),
    ],
)
def test_create_is_refused_when_the_record_does_not_describe_the_accepted_candidate(
    store: Store, project, task_spec: TaskSpec, field: str, value: str
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-bind")
    with pytest.raises(ValueError, match=field):
        store.create_integration(_record(store, run_id, **{field: value}))
    assert store.integrations_for(run_id) == []


def test_create_records_only_a_preparing_integration(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-prep")
    with pytest.raises(ValueError, match="preparing"):
        store.create_integration(_record(store, run_id, state=IntegrationState.READY))
    assert store.integrations_for(run_id) == []


@pytest.mark.parametrize("active", list(INTEGRATION_ACTIVE_STATES))
def test_a_second_active_integration_of_the_same_run_is_refused(
    store: Store, project, task_spec: TaskSpec, active: IntegrationState
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-two")
    first = store.create_integration(_record(store, run_id))
    if active is IntegrationState.CHECKING:
        store.update_integration(
            first.integration_id, expected=IntegrationState.PREPARING, state=active
        )
    elif active is IntegrationState.APPLYING:
        _to_applying(store, first.integration_id)
    before = store.integration(first.integration_id)
    notes_before = store.notes_for(run_id)
    second = _record(store, run_id)

    with pytest.raises(IntegrationConflict) as refused:
        store.create_integration(second)

    assert first.integration_id in str(refused.value)
    assert f"({active.value})" in str(refused.value)
    assert store.integration(second.integration_id) is None
    assert store.integration(first.integration_id) == before
    assert store.notes_for(run_id) == notes_before


def test_a_new_prepare_supersedes_a_ready_integration_of_the_same_run(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-sup")
    first = store.create_integration(_record(store, run_id))
    _to_ready(store, first.integration_id)
    other_run = _accepted_run(store, project, _spec(task_spec, "other"), "R-other")
    unrelated = store.create_integration(_record(store, other_run))
    _to_ready(store, unrelated.integration_id)

    second = store.create_integration(_record(store, run_id))

    old = store.integration(first.integration_id)
    assert old is not None
    assert old.state is IntegrationState.SUPERSEDED
    assert second.integration_id in old.detail
    assert second.state is IntegrationState.PREPARING
    assert [item.integration_id for item in store.integrations_for(run_id)] == [
        first.integration_id,
        second.integration_id,
    ]
    assert store.integration(unrelated.integration_id).state is IntegrationState.READY, (
        "only the same run's ready integration is superseded"
    )
    assert _integration_notes(store, run_id)[-1] == (
        f"integration: {second.integration_id} prepared against {TARGET} at {BASE}; "
        f"superseded ready integration(s) {first.integration_id}"
    )


# --------------------------------------------------------------------------
# update (compare-and-set)
# --------------------------------------------------------------------------


def test_update_is_a_compare_and_set_that_writes_the_allowed_fields(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-cas")
    created = store.create_integration(_record(store, run_id))

    updated = store.update_integration(
        created.integration_id,
        expected=[IntegrationState.PREPARING, IntegrationState.CHECKING],
        state=IntegrationState.CHECKING,
        mode="replayed",
        integration_commit=MERGED,
        paths=["src/parser.py", "tests/test_parser.py"],
        conflict_paths=[],
        worktree_path="C:/wt",
        worktree_state="PRESENT",
        detail="checks running",
    )

    assert updated.state is IntegrationState.CHECKING
    assert updated.mode == "replayed"
    assert updated.integration_commit == MERGED
    assert updated.paths == ["src/parser.py", "tests/test_parser.py"]
    assert updated.worktree_state == "PRESENT"
    assert updated.detail == "checks running"
    assert updated.target_tip == created.target_tip, "identity fields are untouched"
    assert store.integration(created.integration_id) == updated

    # Fields alone (no state change) are a CAS too.
    again = store.update_integration(
        created.integration_id, expected=IntegrationState.CHECKING, evidence_ids=["E-1", "E-2"]
    )
    assert again.state is IntegrationState.CHECKING
    assert again.evidence_ids == ["E-1", "E-2"]
    assert again.paths == updated.paths


def test_update_from_a_state_that_is_not_expected_is_refused_and_writes_nothing(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-wrong")
    created = store.create_integration(_record(store, run_id))

    with pytest.raises(IntegrationConflict, match="is preparing, expected ready or checking"):
        store.update_integration(
            created.integration_id,
            expected=[IntegrationState.READY, IntegrationState.CHECKING],
            state=IntegrationState.APPLYING,
            detail="should not land",
        )
    assert store.integration(created.integration_id) == created


@pytest.mark.parametrize(
    "field",
    ["target_tip", "target_ref", "run_id", "state_", "receipt_json", "integrated_at", "basis"],
)
def test_update_refuses_a_field_outside_the_allowlist(
    store: Store, project, task_spec: TaskSpec, field: str
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-field")
    created = store.create_integration(_record(store, run_id))
    with pytest.raises(ValueError, match=f"cannot set {field}"):
        store.update_integration(
            created.integration_id, expected=IntegrationState.PREPARING, **{field: "x"}
        )
    assert store.integration(created.integration_id) == created


@pytest.mark.parametrize(
    "fields",
    [{"worktree_state": "GONE"}, {"mode": "fast_forward"}, {"paths": "src/parser.py"}],
    ids=["worktree_state", "mode", "paths"],
)
def test_update_validates_values_through_the_record_model(
    store: Store, project, task_spec: TaskSpec, fields: dict
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-valid")
    created = store.create_integration(_record(store, run_id))
    with pytest.raises(ValidationError):
        store.update_integration(created.integration_id, expected=IntegrationState.PREPARING, **fields)
    assert store.integration(created.integration_id) == created


def test_update_never_sets_integrated(store: Store, project, task_spec: TaskSpec) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-noint")
    created = store.create_integration(_record(store, run_id))
    _to_applying(store, created.integration_id)
    before = store.integration(created.integration_id)
    with pytest.raises(ValueError, match="finalize_integration"):
        store.update_integration(
            created.integration_id,
            expected=IntegrationState.APPLYING,
            state=IntegrationState.INTEGRATED,
        )
    assert store.integration(created.integration_id) == before


def test_update_of_an_unknown_integration_is_not_found(store: Store) -> None:
    with pytest.raises(IntegrationNotFound):
        store.update_integration(
            "G-missing", expected=IntegrationState.READY, state=IntegrationState.APPLYING
        )


def test_two_applying_integrations_of_the_same_repository_target_are_refused(
    store: Store, project, task_spec: TaskSpec
) -> None:
    first_run = _accepted_run(store, project, _spec(task_spec, "one"), "R-one")
    second_run = _accepted_run(store, project, _spec(task_spec, "two"), "R-two")
    third_run = _accepted_run(store, project, _spec(task_spec, "three"), "R-three")
    first = store.create_integration(_record(store, first_run))
    second = store.create_integration(_record(store, second_run))
    elsewhere = store.create_integration(_record(store, third_run, target_ref="refs/heads/dev"))
    _to_applying(store, first.integration_id)
    _to_ready(store, second.integration_id)
    before = store.integration(second.integration_id)

    with pytest.raises(IntegrationConflict) as refused:
        store.update_integration(
            second.integration_id,
            expected=IntegrationState.READY,
            state=IntegrationState.APPLYING,
            apply_intent_at=utc_now(),
        )
    message = str(refused.value)
    assert first.integration_id in message and "R-one" in message
    assert "already applying to refs/heads/main" in message
    assert store.integration(second.integration_id) == before, "nothing of the refused CAS lands"

    # Another target of the same repository is independent.
    assert _to_applying(store, elsewhere.integration_id).state is IntegrationState.APPLYING

    # Once the first leaves applying, the second may take the target.
    store.update_integration(
        first.integration_id, expected=IntegrationState.APPLYING, state=IntegrationState.READY
    )
    applying = store.update_integration(
        second.integration_id, expected=IntegrationState.READY, state=IntegrationState.APPLYING
    )
    assert applying.state is IntegrationState.APPLYING


# --------------------------------------------------------------------------
# finalize
# --------------------------------------------------------------------------


def test_finalize_writes_the_receipt_and_a_note_and_leaves_the_run_row_alone(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-fin")
    created = store.create_integration(_record(store, run_id))
    applying = _to_applying(store, created.integration_id)
    run_before = dict(store.get_run(run_id))
    receipt = _receipt(applying, applied_by="")

    final = store.finalize_integration(
        created.integration_id, expected=IntegrationState.APPLYING, receipt=receipt
    )

    assert final.state is IntegrationState.INTEGRATED
    assert final.integrated_at == receipt.integrated_at
    assert final.basis == "hflow_ref_update"
    assert final.applied_by == "operator", "an empty receipt applied_by keeps the recorded one"
    assert final.integration_commit == MERGED and final.mode == "squash"
    assert store.integration_receipt(created.integration_id) == receipt
    assert _integration_notes(store, run_id)[-1] == (
        f"integration: {created.integration_id} integrated into {TARGET} at {MERGED} "
        "(hflow_ref_update)"
    )
    run_after = dict(store.get_run(run_id))
    assert run_after == run_before, "the run's receipt, task state and delivery state are untouched"
    assert run_after["delivery_state"] == DeliveryState.LOCAL_CANDIDATE.value
    assert ResultReceipt.model_validate_json(run_after["receipt_json"]).delivery_state is (
        DeliveryState.LOCAL_CANDIDATE
    )


def test_finalize_from_ready_as_an_observed_operator_merge(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-obs")
    created = store.create_integration(_record(store, run_id))
    ready = _to_ready(store, created.integration_id)
    receipt = _receipt(ready, basis="operator_merge_observed", applied_by="")

    final = store.finalize_integration(
        created.integration_id, expected=[IntegrationState.READY], receipt=receipt
    )
    assert final.basis == "operator_merge_observed"
    assert final.applied_by == ""


def test_finalize_is_refused_from_a_state_that_is_not_expected(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-finx")
    created = store.create_integration(_record(store, run_id))
    receipt = _receipt(created)
    notes_before = store.notes_for(run_id)

    with pytest.raises(IntegrationConflict, match="is preparing, expected applying"):
        store.finalize_integration(
            created.integration_id, expected=IntegrationState.APPLYING, receipt=receipt
        )
    assert store.integration(created.integration_id) == created
    assert store.integration_receipt(created.integration_id) is None
    assert store.notes_for(run_id) == notes_before

    with pytest.raises(IntegrationNotFound):
        store.finalize_integration(
            "G-missing", expected=IntegrationState.APPLYING,
            receipt=receipt.model_copy(update={"integration_id": "G-missing"}),
        )


def test_finalize_happens_once_and_an_integrated_record_keeps_its_state(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-once")
    created = store.create_integration(_record(store, run_id))
    applying = _to_applying(store, created.integration_id)
    receipt = _receipt(applying)
    final = store.finalize_integration(
        created.integration_id, expected=IntegrationState.APPLYING, receipt=receipt
    )

    with pytest.raises(IntegrationConflict, match="is integrated, expected applying"):
        store.finalize_integration(
            created.integration_id, expected=IntegrationState.APPLYING,
            receipt=_receipt(applying, integrated_at="2026-10-07T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="once"):
        store.finalize_integration(
            created.integration_id, expected=IntegrationState.INTEGRATED, receipt=receipt
        )
    with pytest.raises(IntegrationConflict, match="keeps its state and receipt"):
        store.update_integration(
            created.integration_id, expected=IntegrationState.INTEGRATED,
            state=IntegrationState.STALE,
        )
    assert store.integration(created.integration_id) == final
    assert store.integration_receipt(created.integration_id) == receipt


@pytest.mark.parametrize(
    "overrides",
    [
        {"integration_commit": "9" * 40},
        {"integration_tree": "9" * 40},
        {"mode": "replayed"},
        {"target_tip_before": MOVED_TIP},
        {"target_ref": "refs/heads/dev"},
        {"run_id": "R-other"},
        {"attempt_id": "A-other"},
    ],
    ids=lambda overrides: next(iter(overrides)),
)
def test_finalize_refuses_a_receipt_that_does_not_describe_the_record(
    store: Store, project, task_spec: TaskSpec, overrides: dict
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-mis")
    created = store.create_integration(_record(store, run_id))
    applying = _to_applying(store, created.integration_id)

    with pytest.raises(ValueError, match="does not describe"):
        store.finalize_integration(
            created.integration_id, expected=IntegrationState.APPLYING,
            receipt=_receipt(applying, **overrides),
        )
    assert store.integration(created.integration_id) == applying
    assert store.integration_receipt(created.integration_id) is None


def test_finalize_refuses_a_receipt_for_another_integration(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-other-id")
    created = store.create_integration(_record(store, run_id))
    applying = _to_applying(store, created.integration_id)
    with pytest.raises(ValueError, match="not " + created.integration_id):
        store.finalize_integration(
            created.integration_id, expected=IntegrationState.APPLYING,
            receipt=_receipt(applying, integration_id="G-elsewhere"),
        )


def test_the_receipt_round_trips_as_canonical_json(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-rt")
    created = store.create_integration(_record(store, run_id))
    applying = _to_applying(store, created.integration_id)
    receipt = _receipt(
        applying, mode="squash", limitations=["checks ran on the integration tree only"]
    )
    store.finalize_integration(
        created.integration_id, expected=IntegrationState.APPLYING, receipt=receipt
    )

    stored = store.conn.execute(
        "SELECT receipt_json FROM integrations WHERE integration_id = ?",
        (created.integration_id,),
    ).fetchone()[0]
    assert stored == canonical_json(receipt.model_dump(mode="json"))
    loaded = store.integration_receipt(created.integration_id)
    assert loaded == receipt
    assert IntegrationReceipt.model_validate_json(loaded.model_dump_json()) == receipt


# --------------------------------------------------------------------------
# schema backstops
# --------------------------------------------------------------------------


def test_the_schema_refuses_an_integrated_row_without_a_receipt_and_an_unknown_state(
    store: Store, project, task_spec: TaskSpec
) -> None:
    run_id = _accepted_run(store, project, task_spec, "R-ddl")
    created = store.create_integration(_record(store, run_id))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE integrations SET state = 'integrated', integrated_at = 'x', basis = 'y' "
            "WHERE integration_id = ?",
            (created.integration_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE integrations SET state = 'merged' WHERE integration_id = ?",
            (created.integration_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE integrations SET receipt_json = '{}' WHERE integration_id = ?",
            (created.integration_id,),
        )
    assert store.integration(created.integration_id) == created


# --------------------------------------------------------------------------
# storage v8
# --------------------------------------------------------------------------


_V8_INDEXES = {
    "ux_integrations_active_run",
    "ux_integrations_applying_target",
    "ix_integrations_run",
}


def _index_sql(path: Path) -> dict[str, str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(name): str(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'integrations' AND sql IS NOT NULL"
            )
        }
    finally:
        connection.close()


def test_a_fresh_ledger_is_storage_v8_with_the_integrations_table(tmp_path: Path) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    store = Store(path)
    try:
        assert store.storage_version == migrate.STORAGE_VERSION == 8
        assert migrate.SUPPORTED_STORAGE_VERSION == 8
        assert store.migrated_from is None and store.migration_backup is None
        assert migrate.recorded_version(store.conn) == 8
        assert "8->9" not in migrate.migration_steps()
        assert migrate.migration_steps()[-1] == "7->8"
    finally:
        store.close()
    assert "integrations" in migration_helpers._tables(path)  # noqa: SLF001
    indexes = _index_sql(path)
    assert set(indexes) == _V8_INDEXES
    assert "UNIQUE" in indexes["ux_integrations_active_run"]
    assert "WHERE state IN ('preparing', 'checking', 'applying')" in indexes[
        "ux_integrations_active_run"
    ]
    assert "WHERE state = 'applying'" in indexes["ux_integrations_applying_target"]


def _v7_database(path: Path, spec: TaskSpec) -> None:
    """A genuine v7 ledger: the v1 file the migration tests build, migrated exactly to v7."""
    migration_helpers._build_v1_database(path, spec, record_version=True)  # noqa: SLF001
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert migrate.migrate(connection, path, supported=7)[:2] == (1, 7)
    finally:
        connection.close()
    # The intermediate snapshot is not what this test is about; v8 writes its own.
    migrate.backup_path_for(path, 1).unlink()


def test_migration_v7_to_v8_keeps_every_row_and_adds_the_table_and_indexes(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    _v7_database(path, task_spec)
    tables_before = migration_helpers._tables(path)  # noqa: SLF001
    assert "integrations" not in tables_before
    content_before = migration_helpers._content_digest(path)  # noqa: SLF001

    store = Store(path)
    try:
        assert store.migrated_from == 7
        assert store.storage_version == 8
        assert store.migration_backup == migrate.backup_path_for(path, 7)
        assert store.migration_backup.exists()
        assert store.integrations_for(migration_helpers.RUN_ID) == []
        migration_helpers._assert_legacy_rows_are_readable(store, task_spec)  # noqa: SLF001
    finally:
        store.close()

    content_after = migration_helpers._content_digest(path)  # noqa: SLF001
    assert set(content_after) == tables_before | {"integrations"}
    # ``schema_meta`` is where the version is stamped (and the store seeds its own keys); every
    # table that holds a recorded fact is byte-for-byte the same content.
    for table in tables_before - {"schema_meta"}:
        assert content_after[table] == content_before[table], table
    assert migration_helpers._stored_version(path) == 8  # noqa: SLF001
    assert set(_index_sql(path)) == _V8_INDEXES
    backup = migrate.backup_path_for(path, 7)
    assert "integrations" not in migration_helpers._tables(backup)  # noqa: SLF001

    reopened = Store(path)
    try:
        assert reopened.migrated_from is None, "a v8 file is not migrated twice"
    finally:
        reopened.close()


def test_an_interrupted_v8_migration_rolls_back_and_a_later_open_succeeds(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    path = tmp_path / "data" / "hflow.sqlite"
    _v7_database(path, task_spec)
    content_before = migration_helpers._content_digest(path)  # noqa: SLF001

    def boom(step: str) -> None:
        if step.startswith("v8:"):
            raise migrate.MigrationError("test interruption in v8")

    with pytest.raises(StoreError, match="test interruption in v8"):
        Store(path, on_migration_step=boom)
    assert migration_helpers._stored_version(path) == 7  # noqa: SLF001
    assert "integrations" not in migration_helpers._tables(path)  # noqa: SLF001
    assert migration_helpers._content_digest(path) == content_before  # noqa: SLF001

    later = Store(path)
    try:
        assert later.migrated_from == 7 and later.storage_version == 8
    finally:
        later.close()


def test_a_version_less_file_is_read_by_shape_as_v7_or_v8(
    tmp_path: Path, task_spec: TaskSpec
) -> None:
    v7 = tmp_path / "v7" / "hflow.sqlite"
    _v7_database(v7, task_spec)
    v8 = tmp_path / "v8" / "hflow.sqlite"
    Store(v8).close()
    for path, expected in ((v7, 7), (v8, 8)):
        connection = sqlite3.connect(str(path), isolation_level=None)
        try:
            connection.execute("DELETE FROM schema_meta WHERE key = 'storage_version'")
            assert migrate.effective_version(connection) == expected
        finally:
            connection.close()
