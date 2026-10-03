"""Report rendering from stored facts only. No model is called (plan 16.7).

``status`` and ``report`` are pure projections of SQLite. If a value was not
observed, it prints as ``unknown`` rather than being estimated.
"""

from __future__ import annotations

from .contracts import (
    LaunchSurfaces,
    ModelApplied,
    ModelObservation,
    ResultReceipt,
    RunInspection,
    StreamOrder,
    SurfaceFile,
)


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
        f"  repairs     used {usage.used_repairs}/{limits.max_repairs} (the root's own counter: "
        "a repair dispatch is charged here; whether repair was armed for a run is in that run's "
        "repair decisions)",
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
        line = f"  {entry.role:<12} agent={entry.agent} driver={entry.driver} -> {entry.driver_id}"
        if entry.launch is not None:
            # The recorded config fact: which DSH home the launch bound, if any.
            line += (
                f" dsh_home=bound:{entry.launch.dsh_home}"
                if entry.launch.dsh_home
                else " dsh_home=unbound(per-invocation)"
            )
        lines.append(line)
    lines.append(
        f"  writes      implementer={effective.implementer_writes} "
        f"reviewer={effective.reviewer_writes}"
    )
    return lines


def _repair_lines(inspection: RunInspection) -> list[str]:
    """Every repair decision this run recorded, refusals included, or an explicit "none".

    A run with no decision gets a sentence rather than an empty table: "no repair decision was
    recorded" is a fact, and a blank section reads as a renderer that lost one. Each decision
    carries what an operator needs to audit it - the decision, its trigger, the round, the failed
    checks with the exit codes the policy matched, and the reason in the run's own words.
    """
    records = inspection.repair_records
    if not records:
        return ["repair        no repair decision recorded"]
    lines = ["repair decisions"]
    for index, record in enumerate(records, start=1):
        header = (
            f"  {index}  decision={record.decision.value} "
            f"trigger={record.trigger.value if record.trigger else 'none'} round={record.round}"
        )
        if record.decided_at:
            header += f" decided_at={record.decided_at}"
        lines.append(header)
        if record.policy_digest:
            lines.append(f"     policy    {record.policy_digest}")
        if record.failed_checks:
            lines.append(
                "     failed    "
                + ", ".join(
                    f"{check} exit={_unknown(record.exit_codes.get(check))}"
                    for check in record.failed_checks
                )
            )
        else:
            lines.append(
                "     failed    (none: this decision did not come from a failed check)"
            )
        lines.append(f"     reason    {record.reason or '(no reason recorded)'}")
    return lines


def _model_line(
    role: str, observation: ModelObservation | None, applied: ModelApplied | None
) -> str:
    """One invocation's model facts as its stream showed them, or "not recorded".

    The values are the agent's opaque option ids, printed verbatim. A run recorded before these
    facts existed (or by the offline driver) says so instead of reading as "no model".
    """
    if observation is None and applied is None:
        return (
            f"    model         {role} not recorded (this result carries no model observation)"
        )
    applied_text = _unknown(applied.value if applied else None)
    parts = [f"    model         {role} model_applied={applied_text}"]
    if observation is not None:
        parts.append(f"advertised={observation.advertised}")
        parts.append(f"requested={observation.requested or '-'}")
        parts.append(f"effective={_unknown(observation.effective_value)}")
        if observation.source:
            parts.append(f"(from {observation.source})")
        if observation.changes:
            parts.append(f"changes={len(observation.changes)}")
        if observation.thought_level is not None:
            parts.append(f"thought_level={observation.thought_level}")
    return " ".join(parts)


def _stream_line(role: str, order: StreamOrder | None) -> str:
    """Where one invocation's bound prompt response fell and what followed it, or "unknown".

    ``None`` is printed as unknown, never as 0: the offline driver, an unbound turn, a stream not
    read to its end and a result recorded before this field existed all record nothing.
    """
    if order is None:
        return (
            f"    stream        {role} updates_after_prompt_response=unknown (not recorded: no "
            "bound prompt response, no stream read to its end, or a result without this record)"
        )
    return (
        f"    stream        {role} prompt_response_line={order.prompt_response_line} "
        f"updates_after_prompt_response={order.updates_after_prompt_response} "
        f"(agent_message_chunk {order.message_chunks_after_prompt_response})"
    )


def _dsh_context_lines(inspection: RunInspection) -> list[str]:
    """The stored DSH context records, projected as recorded.

    Each record carries the list it was classified against, and that is what is printed: the
    current build's list may differ, and applying it to an old record would state a check that
    never ran.
    """
    records = inspection.dsh_context
    if not records:
        return [
            "dsh context   not recorded: no frozen Git candidate was classified for this run (an "
            "in-place run, a run that ended or was refused before its candidate was kept, or one "
            "recorded before this build)"
        ]
    lines = [
        "dsh context   files a DSH agent would load from the candidate worktree, per frozen "
        "candidate (git diff --no-renames <original base>..<candidate>, classified when it was "
        "frozen)"
    ]
    for record in records:
        count = len(record.paths)
        found = (
            f"CHANGED {count}: {', '.join(record.paths[:20])}"
            + (f" (+{count - 20} more)" if count > 20 else "")
            if record.paths
            else "none on the list"
        )
        lines.append(
            f"  {record.attempt_id}  round={record.round} "
            f"candidate={record.candidate_commit} {found}"
        )
    for source in dict.fromkeys(record.list_source for record in records):
        lines.append(f"  list        {source or '(not recorded)'}")
    lines.append(
        "  meaning     recorded only: nothing was refused and the reviewer packet is unchanged. "
        "Files already in the base commit load too and are not listed"
    )
    return lines


def surface_summary(entry: SurfaceFile) -> str:
    """One launch-surface path as a short phrase. A ``.env`` never has a digest to show."""
    if entry.kind == "absent":
        return f"{entry.name} absent"
    if entry.kind == "directory":
        return f"{entry.name} directory"
    if entry.kind == "unknown":
        return f"{entry.name} unknown ({entry.detail})"
    if entry.kind == "other":
        return f"{entry.name} other"
    if entry.name.rsplit("/", 1)[-1] == ".env":
        return f"{entry.name} {entry.size} bytes (not opened: may hold credentials)"
    if entry.sha256:
        return f"{entry.name} {entry.size} bytes {entry.sha256}"
    return f"{entry.name} {entry.size} bytes ({entry.detail})"


def launch_surfaces_lines(
    role: str, surfaces: LaunchSurfaces | None, *, indent: str = "    "
) -> list[str]:
    """One invocation's launch-surface record as stored, or "not recorded". Never re-observed.

    The record's notes are not repeated here; ``report --json`` carries them.
    """
    if surfaces is None:
        return [
            f"{indent}launch        {role} surfaces not recorded (this result carries no "
            "launch-surface record)"
        ]
    if surfaces.dsh_home_kind == "bound":
        home = f"{indent}dsh home      {role} bound {surfaces.dsh_home}"
    else:
        home = (
            f"{indent}dsh home      {role} per-invocation {surfaces.dsh_home} (created empty for "
            "each invocation; inferred)"
        )
    if surfaces.dsh_home_observed:
        present = [surface_summary(e) for e in surfaces.dsh_home_files if e.present is not False]
        home += ": " + ("; ".join(present) if present else "nothing present")
    else:
        home += " (not looked into before dispatch)"
    if not surfaces.workspace:
        workspace = (
            f"{indent}workspace     {role} not known before dispatch: observed at each "
            "invocation's spawn"
        )
    else:
        env_file = surfaces.workspace_env
        if env_file is None or env_file.kind == "absent":
            env_text = "absent"
        elif env_file.kind == "unknown":
            env_text = f"unknown ({env_file.detail})"
        elif env_file.kind == "file":
            env_text = f"present {env_file.size} bytes, not opened"
        else:
            env_text = f"present ({env_file.kind}), not opened"
        instructions = ", ".join(e.name for e in surfaces.instruction_files) or "none"
        skills = ", ".join(e.name for e in surfaces.skill_dirs if e.present) or "none"
        inherited = "yes" if surfaces.deepseek_api_key_inherited else "no"
        workspace = (
            f"{indent}workspace     {role} {surfaces.workspace}: .env {env_text}; "
            f"DEEPSEEK_API_KEY inherited={inherited}; "
            f"project root {surfaces.project_root or 'none (no .git marker)'}; "
            f"instructions {instructions}; skills {skills}"
        )
    client = surfaces.client
    dsh = client.dsh_carrier + (f" {client.dsh_version}" if client.dsh_version else "")
    client_line = (
        f"{indent}client        {role} acpx={client.acpx_version or 'unknown'} "
        f"sdk={client.sdk_version or 'unknown'} dsh={dsh}; DSH_* reaching the child: "
        f"{', '.join(surfaces.dsh_env_names) or 'none'} (names only)"
    )
    return [home, workspace, client_line]


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
    # Right under the block: the warning can be the one fact the block reason leaves out (a run
    # that ended without a receipt, or a stop that already decided it), and `hflow clean` reads
    # that metadata next.
    lines.extend(f"git metadata  {note}" for note in inspection.git_metadata_notes)
    lines.append("attempts")
    if not inspection.attempts:
        lines.append("  (none)")
    for attempt in inspection.attempts:
        lines.append(
            f"  {attempt.attempt_id}  revision={attempt.task_revision} role={attempt.role} "
            f"state={attempt.state.value} outcome={_unknown(attempt.outcome.value if attempt.outcome else None)} "
            f"repair={attempt.is_repair}"
            + (f" block={attempt.block_code}" if attempt.block_code else "")
        )
        lines.append(_model_line("implementer", attempt.model_observation, attempt.model_applied))
        lines.append(_stream_line("implementer", attempt.stream_order))
        lines.extend(launch_surfaces_lines("implementer", attempt.launch_surfaces))
        if attempt.review_invocation_id:
            lines.append(
                _model_line(
                    "reviewer", attempt.review_model_observation, attempt.review_model_applied
                )
            )
            lines.append(_stream_line("reviewer", attempt.review_stream_order))
            lines.extend(launch_surfaces_lines("reviewer", attempt.review_launch_surfaces))
    lines.extend(_dsh_context_lines(inspection))
    lines.extend(_repair_lines(inspection))
    lines.append("evidence")
    if not inspection.evidence:
        lines.append("  (none)")
    # Joined by attempt id: the reviewer attaches to the implementer's attempt, so the review row
    # of an attempt whose frozen candidate changed DSH context files is marked. It is not a field
    # of the evidence row.
    dsh_changed = {record.attempt_id for record in inspection.dsh_context if record.paths}
    for item in inspection.evidence:
        # The exit code is shown whenever one was recorded, not only for `command` checks: the
        # offline runner declares a verdict and a code without running a command, and a repair
        # decision is taken on exactly that code. Printing "-" for it would hide the fact the
        # decision was made from, while printing "-" for a genuinely absent code (a timeout, a
        # check that never launched) stays honest.
        lines.append(
            f"  {item.evidence_id}  kind={item.kind} check={item.check_id or '-'} "
            f"status={item.status.value} exit={_unknown(item.exit_code)}"
            + (
                " dsh_context=changed"
                if item.kind == "review" and item.attempt_id in dsh_changed
                else ""
            )
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
        # Batch E2 repair decisions, refusals included. An empty list means this run recorded
        # none, which the text form says in words rather than leaving a blank section.
        "repair_records": [r.model_dump(mode="json") for r in inspection.repair_records],
        # One per frozen Git candidate; empty means none was classified (see the text form).
        "dsh_context": [r.model_dump(mode="json") for r in inspection.dsh_context],
        # The shared-Git-metadata warnings, verbatim from the store; empty means none was found.
        "git_metadata_notes": list(inspection.git_metadata_notes),
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
