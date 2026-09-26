"""Report rendering from stored facts only. No model is called (plan 16.7).

``status`` and ``report`` are pure projections of SQLite. If a value was not
observed, it prints as ``unknown`` rather than being estimated.
"""

from __future__ import annotations

from .contracts import ResultReceipt, RunInspection


def _unknown(value: object) -> str:
    return "unknown" if value is None else str(value)


def _root_budget_lines(inspection: RunInspection) -> list[str]:
    """The root ledger's recorded facts, or an explicit statement that there is none.

    A legacy run has no root row. Printing zeros for it would read as "a ledger exists and
    nothing was spent", which is a different and false statement: absent facts are not zeros.
    """
    usage = inspection.root_budget
    if usage is None:
        return [
            "root budget   legacy / not recorded: this run has no root ledger row, so it has no "
            "root ceilings, no repair counter and no root deadline. Absent, not zero"
        ]
    binding, limits = usage.binding, usage.limits
    lines = [
        f"root budget   {binding.root_id}",
        f"  covers      project {binding.project_id}, task {binding.task_id}",
        f"  repo        {binding.repo_path}",
        f"  ledger      {binding.ledger_path}",
        f"  submissions used {usage.used_top_level_submissions}/"
        f"{limits.max_top_level_submissions} (remaining {usage.remaining})",
        f"  repairs     used {usage.used_repairs}/{limits.max_repairs} (recorded counter; the "
        "repair loop that would spend it is not implemented in E1)",
    ]
    if usage.deadline_at:
        lines.append(
            f"  deadline    {usage.deadline_at} (root limit {limits.deadline_seconds}s, measured "
            f"from the first reservation at {_unknown(usage.first_dispatch_at)})"
        )
    else:
        lines.append(
            f"  deadline    none yet: the {limits.deadline_seconds}s root limit is measured from "
            "the first reservation, and no reservation is recorded"
        )
    if usage.run_ids:
        lines.append(f"  runs        {', '.join(usage.run_ids)}")
    if usage.authorization_ids:
        lines.append(f"  approvals   {', '.join(usage.authorization_ids)}")
    return lines


def _dispatch_ledger_lines(inspection: RunInspection) -> list[str]:
    """Per-invocation role and launch state, never merged into one "invocations" number.

    Four facts, deliberately kept apart: a reserved allowance is not a launch *requested*, a
    requested launch is not a process *created*, and a created process is not an observed provider
    model request. The ledger records the first three - the last stays ``unknown`` unless a driver
    reported it, and is never derived from a reservation.
    """
    counts = inspection.invocation_counts
    if inspection.root_budget is None and not inspection.invocations:
        return [
            "dispatch ledger",
            "  legacy / not recorded: this run has no root, so no top-level dispatch record "
            "exists and no reserved/requested/started state can be reported",
        ]
    lines = [
        "dispatch ledger",
        f"  reserved={counts.reserved} requested={counts.requested} started={counts.started} "
        f"not_started={counts.not_started} settled={counts.settled} unknown={counts.unknown} "
        f"launch_unknown={counts.launch_unknown}",
        f"  processes   {counts.processes} operating-system child(ren) reported by a driver "
        f"(ever started {counts.ever_started}); {counts.childless_launches} launch(es) ran without "
        "one, which is what the offline driver does",
        "  meaning     reserved = the dispatch transaction committed: allowance spent, intent "
        "durable. Not a launch, not a process, not a model request",
        "  meaning     requested = the controller asked a driver to launch it; nothing reported "
        "yet. A crash here leaves launch_unknown, which blocks the root",
        "  meaning     started = the launch happened. Whether it created a process is the separate "
        "count above; a childless launch is still a launch",
        "  meaning     not_started = no launch happened (a stop won the handoff, or the driver "
        "reported none); the allowance is kept, never refunded",
        "  meaning     settled = a recorded result was applied; unknown = a launch was never "
        "settled, which blocks the root and is never re-dispatched",
        "  provider model requests: unknown - no invocation row records one, and neither a "
        "reservation nor a process is counted as one",
    ]
    if inspection.invocations:
        lines.append("  invocations")
        for invocation in inspection.invocations:
            lines.append(
                f"    {invocation.invocation_id}  role={invocation.role} "
                f"state={invocation.state.value} spawn={invocation.spawn_kind.value} "
                f"repair={invocation.is_repair} attempt={invocation.attempt_id}"
                + (
                    f" approval={invocation.authorization_id}"
                    if invocation.authorization_id
                    else ""
                )
            )
            facts: list[str] = []
            if invocation.launch_requested_at:
                facts.append(f"launch_requested_at={invocation.launch_requested_at}")
            if invocation.started_at:
                facts.append(f"launched_at={invocation.started_at}")
            if invocation.process_started_at:
                facts.append(
                    f"process_started_at={invocation.process_started_at} "
                    f"pid={invocation.process_pid}"
                )
            if invocation.settled_at:
                facts.append(f"settled_at={invocation.settled_at}")
            if invocation.outcome is not None:
                facts.append(f"outcome={invocation.outcome.value}")
            if invocation.detail:
                facts.append(f"detail={invocation.detail}")
            if facts:
                lines.append("      " + " ".join(facts))
    return lines


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


def _config_lines(inspection: RunInspection) -> list[str]:
    """Which configuration the run used. A historical run says so instead of being guessed at."""
    effective = inspection.effective_config
    if effective is None:
        return [
            "configuration not recorded: this run predates config binding, so its profile and "
            "role bindings are unknown rather than assumed"
        ]
    lines = [
        f"configuration {effective.source}"
        + (f" profile={effective.profile_id}" if effective.profile_id else " (no profile)")
        + f" digest={effective.digest()}"
    ]
    for entry in effective.roles:
        lines.append(
            f"  {entry.role:<12} agent={entry.agent} driver={entry.driver} -> {entry.driver_id}"
        )
    lines.append(
        f"  writes      implementer={effective.implementer_writes} "
        f"reviewer={effective.reviewer_writes}"
    )
    return lines


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
    lines.extend(_config_lines(inspection))
    implementer = sum(1 for a in inspection.attempts if a.invocation_id)
    reviewer = sum(1 for a in inspection.attempts if a.review_invocation_id)
    lines.append(
        f"invocations   implementer={implementer} reviewer={reviewer} "
        "(attempt rows; a deterministic dispatch count, not a model-request count)"
    )
    lines.extend(_root_budget_lines(inspection))
    lines.extend(_dispatch_ledger_lines(inspection))
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
    lines.append(
        f"model_calls   {inspection.model_calls_made} (this command; provider-side requests are "
        "reported by the receipt and stay unknown when not observed)"
    )
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
    ]
    # A later decision must show what the execution itself ended as, right next to the delivery.
    if receipt.provenance.get("kind"):
        lines.extend(
            [
                "provenance    this receipt is a later decision, not the execution's own outcome",
                f"  kind                  {receipt.provenance.get('kind')}",
                f"  original_decision     {receipt.provenance.get('original_decision', '-')}"
                f" / {receipt.provenance.get('original_block_code', '-')}",
                f"  original_runtime      {receipt.provenance.get('original_runtime_build', '-')}",
                f"  original_reason       {receipt.provenance.get('original_block_reason', '-')}",
                f"  source_evidence       {receipt.provenance.get('source_evidence_id', '-')}",
                f"  model_calls           {receipt.provenance.get('model_calls', '-')}"
                f" (authorization consumed: {receipt.provenance.get('authorization_consumed', '-')})",
            ]
        )
    lines.extend(
        [
            "usage",
            f"  turns_reserved        {usage.controller_turns_reserved}",
            f"  turns_observed        {_unknown(usage.controller_turns_observed)} (implementer self-report)",
            f"  provider_billed_tokens {_unknown(usage.provider_billed_tokens)}",
            f"  provider_cost          {_unknown(usage.provider_cost)}",
            f"  quota_remaining        {_unknown(usage.subscription_quota_remaining)}",
        ]
    )
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
        "effective_config": inspection.effective_config.model_dump(mode="json")
        if inspection.effective_config
        else None,
        "effective_config_digest": inspection.effective_config.digest()
        if inspection.effective_config
        else None,
        # Batch E1 ledger facts. ``None`` / empty means "not recorded for this run", which is
        # what a legacy run has: it is not an empty ledger with zero consumption.
        "root_budget": inspection.root_budget.model_dump(mode="json")
        if inspection.root_budget
        else None,
        "invocations": [i.model_dump(mode="json") for i in inspection.invocations],
        "invocation_counts": inspection.invocation_counts.model_dump(mode="json"),
        "model_calls_made": inspection.model_calls_made,
    }
    return payload
