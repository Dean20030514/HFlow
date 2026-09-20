"""Report rendering from stored facts only. No model is called (plan 16.7).

``status`` and ``report`` are pure projections of SQLite. If a value was not
observed, it prints as ``unknown`` rather than being estimated.
"""

from __future__ import annotations

from .contracts import ResultReceipt, RunInspection


def _unknown(value: object) -> str:
    return "unknown" if value is None else str(value)


def _drift_line(inspection: RunInspection) -> str:
    """Never let a historical ACCEPTED read as verification of the current tree."""
    matches = inspection.run.workspace_matches_receipt
    if matches is None:
        return "candidate     no receipt to compare against the workspace"
    if matches:
        return "candidate     workspace still matches the accepted fingerprint"
    return (
        "candidate     DRIFTED: scoped files changed after acceptance; ACCEPTED describes "
        "the historical candidate, not the current working tree"
    )


def status_text(inspection: RunInspection) -> str:
    run = inspection.run
    lines = [
        f"run           {run.run_id}",
        f"task          {run.task_id} revision {run.task_revision}",
        f"state         {run.task_state.value}" + (f" ({run.phase.value})" if run.phase else ""),
        f"delivery      {run.delivery_state.value}",
        f"claimed_by    {_unknown(run.claimed_by)}",
        f"turns         reserved {run.agent_turns_reserved}/{run.agent_turns_limit}, "
        f"implementer self-reported {_unknown(run.agent_turns_observed)} "
        "(a self-report, not a dispatched count and not a bill)",
        f"spec_digest   {run.spec_digest}",
    ]
    implementer = sum(1 for a in inspection.attempts if a.invocation_id)
    reviewer = sum(1 for a in inspection.attempts if a.review_invocation_id)
    lines.append(
        f"invocations   implementer={implementer} reviewer={reviewer} "
        "(deterministic dispatch count; billed model requests: unknown)"
    )
    lines.append(_drift_line(inspection))
    if run.block_code:
        lines.append(f"blocked       {run.block_code}: {run.block_reason}")
    lines.append("attempts")
    if not inspection.attempts:
        lines.append("  (none)")
    for attempt in inspection.attempts:
        lines.append(
            f"  {attempt.attempt_id}  revision={attempt.task_revision} role={attempt.role} "
            f"state={attempt.state.value} outcome={_unknown(attempt.outcome.value if attempt.outcome else None)}"
            + (f" block={attempt.block_code}" if attempt.block_code else "")
        )
    lines.append("evidence")
    if not inspection.evidence:
        lines.append("  (none)")
    for item in inspection.evidence:
        lines.append(
            f"  {item.evidence_id}  kind={item.kind} check={item.check_id or '-'} "
            f"status={item.status.value} exit={_unknown(item.exit_code) if item.command else '-'}"
        )
    lines.append(f"model_calls   {inspection.model_calls_made}")
    return "\n".join(lines)


def receipt_text(receipt: ResultReceipt) -> str:
    usage = receipt.usage
    lines = [
        f"receipt       schema_version={receipt.schema_version}",
        f"run           {receipt.run_id}",
        f"task          {receipt.task_id} revision {receipt.task_revision}",
        f"attempt       {receipt.attempt_id}",
        f"runtime_build {receipt.runtime_build}",
        f"plan_digest   {receipt.plan_digest}",
        f"outcome       {receipt.harness_outcome.value}",
        f"candidate     base={receipt.candidate.base_commit}",
        *(
            [
                f"              git_commit={receipt.candidate.git_commit}",
                f"              git_tree={receipt.candidate.git_tree}",
                f"              worktree={receipt.candidate.worktree}",
                f"              paths={', '.join(receipt.candidate_paths) or '-'}",
            ]
            if receipt.candidate.git_commit
            else []
        ),
        f"              fingerprint={receipt.candidate.fingerprint}",
        f"verification  {receipt.verification.status} evidence={', '.join(receipt.verification.evidence_ids) or '-'}",
        f"review        {receipt.review.status} isolation={receipt.review.isolation.value}",
        f"task_state    {receipt.task_state.value}",
        f"delivery      {receipt.delivery_state.value}",
        "usage",
        f"  turns_reserved        {usage.controller_turns_reserved}",
        f"  turns_observed        {_unknown(usage.controller_turns_observed)} (implementer self-report)",
        f"  provider_billed_tokens {_unknown(usage.provider_billed_tokens)}",
        f"  provider_cost          {_unknown(usage.provider_cost)}",
        f"  quota_remaining        {_unknown(usage.subscription_quota_remaining)}",
    ]
    if receipt.limitations:
        lines.append("limitations")
        lines.extend(f"  - {item}" for item in receipt.limitations)
    lines.append(
        "scope note    candidate.fingerprint is a content fingerprint over the TaskSpec's write "
        "scope; candidate.git_commit/git_tree are real Git objects when the run used a worktree. "
        "They are different identities and neither is a whole-repository verification cache key"
    )
    return "\n".join(lines)


def report_text(inspection: RunInspection) -> str:
    parts = [status_text(inspection)]
    parts.append("")
    if inspection.receipt is None:
        parts.append("receipt       none (the run has not been accepted)")
    else:
        parts.append(receipt_text(inspection.receipt))
    return "\n".join(parts)


def report_json(inspection: RunInspection) -> dict[str, object]:
    """Machine-readable form. Same facts, no derived estimates."""
    payload: dict[str, object] = {
        "run": inspection.run.model_dump(mode="json"),
        "task_spec": inspection.task_spec.model_dump(mode="json"),
        "attempts": [a.model_dump(mode="json") for a in inspection.attempts],
        "evidence": [e.model_dump(mode="json") for e in inspection.evidence],
        "receipt": inspection.receipt.model_dump(mode="json") if inspection.receipt else None,
        "model_calls_made": inspection.model_calls_made,
    }
    return payload
