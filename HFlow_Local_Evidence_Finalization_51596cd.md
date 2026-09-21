# HFlow: finalize the preserved candidate from existing evidence

## Status and authority

Prepared from the user's report of fix `51596cd` and note commit `9a2a3c4`. The report confirms the missing reviewer-result wire, positive production-path regressions, and a successful saved-evidence replay in a temporary Store. The author of this handoff has not inspected the local source or raw evidence and has not reproduced these results.

**The reviewer-wiring repair is closed on the reported evidence. New live allowance: 0.** Do not repeat A, B, implementation, review, or formatting work. Do not build another readiness, authorization, or replay platform.

This document proposes a distinct local finalization action. It is not the user's approval and must not be imported as `provided_by: user`. Until the human explicitly requests that action, leave the production Store and delivery state unchanged.

## 1. Fixed identities

| Item | Value |
|---|---|
| Original run | `R-gkb3ld97x8` |
| Original attempt | `A-7f2pbp4teu` |
| Original implementer | `I-xf3sez1lho` |
| Original reviewer | `I-vc5pcccfog` |
| Original runtime | `3dbfeaeec2b50ea18b7d72f50eadc979988bc2b6` |
| Parser/adapter fix | `51596cd` — resolve the full SHA locally |
| Documentation note | `9a2a3c4` — record the actual current HEAD separately |
| Task | `.probe/m2-live/attempt-2/task.json`, revision 2 |
| Base | `5526040ee01672e321c10c4b98bff9d79c30cbe4` |
| Candidate | `499ece7043fe3267b4ff89f9a5b5bc1d70c42481` |
| Candidate reference | `refs/hflow/candidates/R-gkb3ld97x8/A-7f2pbp4teu` |
| Formal verification evidence | `E-zgamyka8s3` |
| Closed live allowance | `AUTH-m2-live-2`, consumed 2/2 |
| Original decision | `BLOCKED / review_rejected`, no delivery receipt |

Resolve full task, answer, stream, check, and configuration digests from existing local records. Never expand the abbreviated hashes in the conversation by guessing. Preserve the unrelated failed run `R-0mh0gtrz9r` and all other project processes.

## 2. Proposed wording for the human to send

> I authorize one local, zero-model finalization of candidate `499ece7043fe3267b4ff89f9a5b5bc1d70c42481` for original run `R-gkb3ld97x8`, using the recorded implementation, verification, and review evidence and the validated reviewer fix `51596cd`. Record the actual processing build and time. Recheck the existing evidence bindings and acceptance safeguards; if anything is missing, changed, cancelled, or contradictory, stop without modifying the candidate or dispatching any task. Preserve the original blocked decision and its original runtime. Record any successful finalization as a new, explicitly linked offline-reprocessing decision and LOCAL_CANDIDATE receipt, not as an uninterrupted success of the old run. Do not copy the temporary test receipt into production or hand-edit the Store. Reuse the existing controller/Store path; only a narrow local finalization entry point is permitted if one is missing. No new live approval ID, allowance claim, credential access, Agent invocation, test rerun, candidate edit, automatic merge/push/release/clean, dependency or global configuration change. `AUTH-m2-live-2` remains consumed 2/2, and unattended execution stays disabled.

This wording permits only the described local action when the human actually sends it. It does not authorize a new B attempt or automatic continuation after an error.

## 3. Execution after explicit human confirmation

Reuse `tools/m2_live/replay_review.py`, the production parser, canonical Review contract, existing evidence checks, and controller/Store acceptance machinery. Do not introduce a second parser or receipt generator. If a local persistence entry point is needed, keep it a narrow operation for the recorded evidence; do not create a general recovery subsystem or fabricate a new model execution.

Before a production write, check the same predicates already used by the successful isolated replay, including original identities and task/check digests, completed reviewer prompt binding, source output integrity, frozen candidate identity, recorded review/no-write observations, verification evidence, cancellation precedence, and compatible current state. Verify actual current associations instead of trusting only the earlier replay's PASS summary.

This is evidence validation, not a new code review. Do not add retrospective contract requirements or rerun valid formal tests just to obtain a new timestamp. Read-only local Git inspection is allowed if needed; it is not model execution. If the preserved evidence no longer supports acceptance, produce the specific blocker and stop.

Use the existing short transaction/CAS pattern. Maintain the original execution decision and add a distinct linked finalization decision with a newly generated production receipt and actual processing time/version. Do not directly patch SQL state, force the original attempt through a fake RUNNING/CHECKING transition, backdate acceptance, or copy the temporary receipt/temporary database.

The storage representation may reuse existing notes, evidence, and decision metadata. The necessary semantic distinction is:

```
Original execution: BLOCKED / review_rejected on the original runtime
Later decision:     accepted by offline reprocessing, on the recorded fixed build
Delivery:           LOCAL_CANDIDATE, linked to the unchanged candidate
New submissions:    0
```

These labels are conceptual, not a required new enum or schema. A current delivery projection may point to the new decision only if it still exposes the original failure separately. If the existing Store cannot represent that distinction, add only the minimal persistence support rather than overwrite history.

The operation must be idempotent: the same source evidence and candidate return the same finalization result rather than issuing duplicate deliveries. Conflicting state or different evidence for the same operation must fail closed. Do not reset authorization consumption or treat a new local processing record as a new model allowance.

## 4. Bounded validation and stopping rule

Existing parser and production-wire tests stand. Do not reopen fenced-output grammar, optional message identity support, tool permissions, or A certification without new evidence of a defect.

Only if a finalization write path is added, exercise it in an isolated Store for successful linked receipt creation, duplicate/idempotent use, and rejection on changed evidence or cancellation/conflicting state. Confirm no model dispatch, credential access, or allowance mutation. Reuse current tests and transaction guards; no new framework or live test is required.

After the approved action, report the new receipt/decision identity, original and processing SHAs, retained candidate reference, evidence references, original blocked decision preserved, unchanged authorization consumption, and new submissions 0. Reuse existing documentation. No code commit is required solely to report a completed action.

Call the result **M2 candidate delivered after evidence-based offline recovery**. Do not call it an uninterrupted successful run, stamp `3dbfeae` as passed, or enable unattended execution.

Once a valid local receipt exists, this particular repair/recovery is finished. A future clean run can demonstrate the fixed path during a useful separately authorized task; do not purchase another implementation/review of this already completed candidate merely for presentation.

## Prior instruction boundary

`HFlow_Reviewer_Result_Offline_Repair.md`, section 5, explicitly distinguishes the temporary evaluation receipt from formal local finalization and permits later requested finalization through controller checks/CAS with the original failure and new processing version/time retained. Its section 7 prohibits automatic historical finalization and a new B trial. This handoff preserves those limits.
