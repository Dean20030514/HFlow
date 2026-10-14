# HFlow — B-only approval and execution handoff

## Status

Prepared from the user's readiness report for `3dbfeae`. The author of this handoff has not inspected the local source, raw evidence, or test execution. The reported readiness result does not itself authorize a live task.

**This file is proposed approval wording, not an authorization artifact.** Only the human user's subsequent explicit confirmation authorizes execution. Do not register this file or the assistant's draft as `provided_by: user`.

The readiness-review development task is closed. Do not add a signing service, change transport, expand permissions, or rerun A as part of this handoff.

## Proposed text for the human to send to DSH

> I approve a new B-only M2 attempt using HFlow `3dbfeae` and the currently reviewed acpx 0.17.1 / DSH 0.1.5-rc.1 execution binding. Resolve and record the full runner commit without changing the code. Use the existing prepared task package under `.probe/m2-live`, the unchanged project base `5526040ee01672e321c10c4b98bff9d79c30cbe4`, and the already-reviewed task, project, and fixed-check inputs. Bind the approval to their actual paths and digests using the existing human-operated approval procedure; do not regenerate the task or alter its acceptance criteria.
>
> This approval permits at most two separately allocated top-level invocations: one DSH implementer, and one fresh-session DSH reviewer only after a valid candidate has been frozen and the fixed program checks pass. The two allocations are not interchangeable. A startup failure after an allowance is claimed, execution failure, timeout, unknown result, verification failure, or review rejection stops this attempt. There is no automatic refund, retry, repair, alternative-model run, or extra formatting-repair call. Do not dispatch the reviewer after an earlier step fails.
>
> I explicitly accept `defaultPermissions=approve-all` for this implementer and `approve-reads` for this reviewer, with the currently validated non-interactive policy. I understand that approve-all covers tool permission requests, not just file writes, and does not confine shell commands or native DSH tools to the worktree. Keep the task within its existing declared scope; this approval does not permit unrelated operations. The reviewer must not inherit the implementer's permissions. No permission escalation is authorized after a denial.
>
> I accept the disclosed trusted-local, user-attested authorization model and prompt-only/audit-only review for this supervised disposable test. Neither is enforced separation from a same-user process. Record only my actual confirmation; do not mint additional approval IDs, alter limits, or treat a fresh ID as new human permission. Top-level allowances are not a guarantee of exact provider request counts or billing.
>
> Create a new managed run/workspace at the unchanged base. Preserve failed run `R-0mh0gtrz9r`, its workspace, and the old B record, including its unusable reviewer remainder. Do not spend or repeat any A task. Retain A's original launch/stop evidence and its limited scope; do not relabel it as proof of approve-all, all tools, a sandbox, or stopped remote billing.
>
> Use only the previously approved credential reference through the existing controlled in-memory injection. Do not widen credential access or log/persist secret values. No Codex, planner, subagents, new dependencies, global changes, automatic merge/push/release/clean, or unattended execution. Preserve the existing time and output limits. End with a controller-generated local candidate and evidence, or with the recorded failure and preserved workspace. Do not modify HFlow during live execution or repair the candidate from the outer development session.

## After the human confirms

Use the existing prepared commands and artifact procedure. This handoff does not invent new CLI flags, an authorization schema, or a budget store.

1. Check the actual runner and relevant binding against this reviewed combination, and verify that task/project/check inputs are unchanged. Reuse the six existing zero-model check results when the relevant inputs are identical. A mismatch pauses dispatch; it is not permission to repair code and continue under this approval. No repeated full regression or A trial is required for an unchanged combination.
2. Record the human's actual confirmation and the new B-specific approval via the existing procedure. Keep previous approvals and their accounting intact. Registering a new ID without a new human decision is not authorized, even though the current trust model does not technically prevent it.
3. Execute the real CLI/controller/Driver implementation path in a new managed worktree. No Fake write plan, supplied final patch, manually fixed candidate, or bypass around the controller.
4. After implementation, confirm execution has ended, freeze the candidate, enforce scope and unchanged acceptance inputs, and run the fixed program checks. Only then dispatch the independent real reviewer with its own role permissions. Candidate/check-input drift invalidates acceptance. Do not repeat valid formal checks merely to prepare review materials.
5. Let the controller issue `ACCEPTED / LOCAL_CANDIDATE` only if the existing acceptance conditions hold. Otherwise retain the failure, partial changes, and evidence. Do not recover or retry automatically, consume the reviewer allocation to rerun implementation, or create another approval ID.

## Final report

Use the existing result document. Report the actual runner and task base, new B authorization consumption, per-role invocation/session identities, top-level prompt dispatch evidence, effective permission modes, candidate commit/tree/ref, fixed-check result, independent review result, receipt, and limitations. Distinguish allowance claims from actual Harness prompt dispatch and provider billing. Billing remains unknown without an appropriate source.

No new code commit is required solely to finish a successful run; a factual evidence/documentation update is sufficient. No forced cleanup of other projects or unrelated processes.

**Finish with the first genuine M2 candidate or one accurately recorded failure—not another readiness-review cycle.**

## Source boundary

Scope is carried forward from `HFlow_B_Only_Readiness_Review.md`, especially its B-only role allocations, unchanged task base, preservation of failed evidence, and trusted-local approval model, together with the user's `3dbfeae` readiness report. These establish the intended execution limits, not independent verification of local code or a grant of live authority.
