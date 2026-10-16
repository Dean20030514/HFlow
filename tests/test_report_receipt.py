"""How a receipt reads in ``report``: a later decision is never shown as the execution's own.

A ledger can hold a receipt whose ``provenance`` says it was decided after the execution (the
one-off M2 recovery of 2026-09 wrote such a receipt). The tool that wrote it is retired; reading
it must keep working, with the original failure printed next to the delivery.
"""

from __future__ import annotations

from hflow.contracts import (
    CandidateSnapshot,
    DeliveryState,
    InvocationOutcome,
    ResultReceipt,
    ReviewResult,
    TaskSpec,
    TaskState,
    UsageFacts,
    VerificationResult,
)
from hflow.report import receipt_text


def _receipt(spec: TaskSpec, **provenance: object) -> ResultReceipt:
    return ResultReceipt(
        run_id="R-1",
        task_id=spec.task_id,
        attempt_id="A-1",
        task_revision=1,
        runtime_build="report-test",
        plan_digest=spec.spec_digest(),
        harness_outcome=InvocationOutcome.COMPLETED,
        candidate=CandidateSnapshot(base_commit="base", fingerprint="sha256:fp"),
        verification=VerificationResult(status="passed", evidence_ids=["E-1"]),
        review=ReviewResult(status="not_required"),
        task_state=TaskState.ACCEPTED,
        delivery_state=DeliveryState.LOCAL_CANDIDATE,
        usage=UsageFacts(),
        provenance=dict(provenance),
    )


def test_a_receipt_without_provenance_prints_no_provenance_block(task_spec: TaskSpec) -> None:
    assert "provenance" not in receipt_text(_receipt(task_spec))


def test_a_later_decision_prints_the_original_outcome_next_to_the_delivery(
    task_spec: TaskSpec,
) -> None:
    text = receipt_text(
        _receipt(
            task_spec,
            kind="offline_reprocessing",
            original_decision="BLOCKED",
            original_block_code="review_invalid",
            original_runtime_build="old-build",
            original_block_reason="the reviewer answer could not be read",
            source_evidence_id="E-9",
            model_calls=0,
            authorization_consumed=False,
        )
    )
    lines = text.splitlines()
    delivery = next(i for i, line in enumerate(lines) if line.startswith("delivery"))
    assert lines[delivery + 1] == (
        "provenance    this receipt is a later decision, not the execution's own outcome"
    )
    assert "  kind                  offline_reprocessing" in lines
    assert "  original_decision     BLOCKED / review_invalid" in lines
    assert "  original_reason       the reviewer answer could not be read" in lines
    assert "  model_calls           0 (authorization consumed: False)" in lines
