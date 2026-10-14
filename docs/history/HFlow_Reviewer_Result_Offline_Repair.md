# HFlow: wire reviewer results and replay existing evidence offline

## Scope and starting point

The user reports a real B execution on runner `3dbfeaeec2b50ea18b7d72f50eadc979988bc2b6`, followed by evidence/documentation commit `e6a303e`. Both authorized invocations completed, but the controller blocked delivery because the real Driver returned `review=None` instead of carrying the reviewer's structured verdict. This brief's author has not inspected these local commits, the candidate, or the original transcript. Confirm the diagnosis against the actual source before changing it.

**This task is an offline adapter/contract repair. New live authorization: 0.** It does not authorize another implementer, reviewer, A test, formatting call, approval artifact, candidate modification, historical state change, or production receipt issuance. The no-retry live attempt stays closed; this is separate maintenance after that attempt.

Preserve:

| Item | Reported identity |
|---|---|
| Run | `R-gkb3ld97x8` |
| Attempt | `A-7f2pbp4teu` |
| Implementer | `I-xf3sez1lho` |
| Reviewer | `I-vc5pcccfog` |
| Authorization | `AUTH-m2-live-2`, consumed 2/2 |
| Original project base | `5526040ee01672e321c10c4b98bff9d79c30cbe4` |
| Frozen candidate | `499ece7043fe3267b4ff89f9a5b5bc1d70c42481` |
| Candidate reference | `refs/hflow/candidates/R-gkb3ld97x8/A-7f2pbp4teu` |
| Task | `.probe/m2-live/attempt-2/task.json`, revision 2; retrieve the full recorded digest rather than expanding the abbreviated report |
| Historical result | `BLOCKED / review_rejected`, no receipt |

Keep the previous failed workspace `R-0mh0gtrz9r` and its history too. Do not reset, amend, merge, push, release, clean, or stop unrelated processes. Keep current permissions, the trusted-local model, and unattended execution disabled. No transport or authorization redesign.

## 1. Reproduce the missing wire before fixing it

Read only the relevant current contract, reviewer prompt/output specification, `AcpxDshDriver.collect()`, event aggregation, and controller `_review()`/acceptance path. Confirm whether the production path discards the verdict as reported. A model's diagnosis is not a substitute for checking these functions.

Identify the canonical review model already defined in `contracts.py` and the output contract in force for this recorded invocation. Do not invent a second Review schema, add mandatory fields the original reviewer was never asked to emit, or weaken the existing schema to make one recorded output pass.

Add a regression that traverses production collection and controller review handling: a valid structured accepted verdict must reach the existing acceptance checks. Show it fails on the unpatched code. Do not replace `collect()`, `_review()`, or the normalized review field with a prefilled accepted object in this regression.

## 2. Recover review content from the right source

Use the existing JSON event path. acpx v0.17.1 documents `--format json` as raw ACP JSON-RPC NDJSON; its terminal prompt response is not HFlow's Review object. Model text is delivered separately in assistant message updates. [S1, S2]

Scope extraction to the recorded reviewer invocation, role, session, and its own prompt interval. Obtain those identities from the controller/Driver records, not from claims inside the model's JSON. Associate the terminal response with the original prompt request ID. Do not treat every JSON-RPC response as task completion.

Reassemble eligible `agent_message_chunk` text in order, preserving content across chunk and UTF-8 read boundaries. Exclude user text, thoughts, tool results, echoed source files, stderr, the implementation transcript, and other sessions. Do not parse individual chunks as independent complete verdicts.

Use message identities/boundaries when the selected installed combination actually provides them. ACP describes messageId as optional: do not require a newer optional field absent from this pinned runtime. When boundaries are unavailable, use the existing, demonstrable turn/answer extraction rule and reject ambiguity; do not guess that the last NDJSON line contains the final text. [S2]

Keep transport validity separate from content: a valid-looking accepted JSON object in an incomplete stream, unsuccessful invocation, timeout, cancelled operation, or capped/truncated output cannot authorize acceptance. Reuse the existing completion, output-limit, stop, and stream-drain checks; do not increase limits to force this example through.

## 3. Parse one contract object, not an acceptance keyword

Implement a deterministic, bounded parser using existing dependencies and the canonical review model. Inspect the actual final answer before choosing the small supported syntax rule; do not change the raw answer.

The normal case is one complete JSON object. A single JSON code fence or a bounded terminal result block may be supported if it is consistent with the established reviewer-output contract and unambiguously identified within the reviewer answer. State the supported forms and test them. Any wrapper removal must preserve all object content and original evidence.

Do not use a greedy first-brace/last-brace regex, search the whole transcript for `accepted`, choose whichever object passes the schema, discard conflicting review objects, fill a missing verdict, or ask a model to repair the format. A prose-only approval is not a structured verdict.

Reject malformed JSON, multiple ambiguous/conflicting result objects, wrong types/enums, missing contract-required fields, duplicate object keys, and non-finite JSON constants. Python's default JSON decoder accepts repeated keys with last-value-wins and accepts some non-standard constants; a JSON decode alone is therefore not this validation policy. Use its existing hooks plus the canonical model rather than writing a JSON parser. [S3]

Preserve a valid `changes_requested` verdict and its findings. Preserve accepted verdict details required by the existing contract; do not reduce the verdict to a Boolean. Leave unknown/optional field handling consistent with the existing schema. Do not silently invent acceptance-check records or findings.

A valid Review is input to controller policy, not permission for the Driver to issue a receipt. Never let model fields override candidate identity, task scope, permissions, budgets, verification results, or the controller state.

## 4. Complete the wire and distinguish failures

The production path must become:

```
reviewer ACP messages
-> existing event/answer aggregation
-> deterministic decoding + canonical Review validation
-> Driver result.review + evidence reference
-> controller review decision
-> unchanged acceptance checks
```

Use one parser for production and offline replay. Keep the general transport layer neutral; any role-specific selection belongs at the existing result-adaptation boundary. Non-review invocations must not acquire review authority merely because their output contains a verdict-shaped object.

Refusing acceptance for a missing review is correct; describing that as the reviewer's substantive rejection is misleading. Retain the existing BLOCKED state and add/reuse a precise reason/detail for missing, invalid, ambiguous, or unbound review evidence. `review_protocol_error` is a suggested description, not a requirement for a new state machine. Genuine validated `changes_requested` remains a review rejection.

Do not rewrite the original `review_rejected` history. A separate diagnostic result can explain that it was an adapter/ingestion failure. Preserve cancellation precedence, scope checks, candidate/check fingerprints, independent reviewer identity and no-write audit, and the existing receipt transaction/CAS.

## 5. Reuse evidence rather than buy another review

Read the original run's saved reviewer stream/final text and completion metadata. Do not reconstruct the accepted object from the user's summary, the reviewer's later diagnosis, or a newly written fixture.

In a disposable evaluation copy, verify the association between:

- original run, attempt, reviewer invocation/session and prompt;
- task revision and full task/project/check digests;
- frozen candidate commit/tree and retained reference;
- successful original verification evidence for that candidate and unchanged checks;
- original reviewer completion, recorded role permissions, review-input binding and before/after no-write observations;
- complete raw output and the extracted object's source location/hash.

Use the existing evidence model. Compute and record a source hash before parsing, while acknowledging that a hash computed now does not prove past authenticity. Do not invent missing timestamps, identities, fingerprints, or session data. The existing trusted-local evidence limitations remain.

Run the patched parser and the existing decision checks on this saved material. Prefer a pure evaluator already present; otherwise use an isolated Store copy/temporary test database with stable references to unchanged evidence. Source records must be readable without starting the original controller execution. This is replay of bytes and decisions, not resuming a Harness session.

Make any attempted model launch, credential read, allowance claim, live resume, or production-ledger mutation fail in this replay. Local read-only Git checks and isolated test subprocesses are permitted where needed. Do not use a global subprocess ban that hides required deterministic checks.

Do not rerun already-valid formal tests just to create a new timestamp. First verify their input associations and integrity. If the historical verification or reviewer binding is insufficient, report that specific gap; do not claim it was repaired by rerunning code checks now.

Output one derived diagnostic: source run, original runner SHA, parser-fix SHA, evidence references/hashes, extracted verdict, acceptance-predicate result, and blockers. A receipt created solely in a temporary test Store is a test artifact, not the historical run's delivery receipt.

Original run state, raw logs, authorizations and budgets remain unchanged. Do not stamp the old runner as passed. Formal post-hoc finalization, if later requested, is a separate explicit local action through controller checks/CAS with recorded original failure and new processing version/time—not a hand-edited SQLite state or forged verdict. It would not inherently require another model review when existing evidence is sufficient.

## 6. Focused zero-model tests

Reuse current mocks and CLI fixtures; do not build a replay service or new protocol server.

| Group | Required outcome |
|---|---|
| Positive production wire | Accepted review travels through actual collect/controller code; valid candidate and fixed checks yield a receipt in an isolated test run. The test fails while collect always returns review=None. |
| Negative review | Valid changes_requested retains findings and blocks without a receipt. |
| Framing and identity | Split JSON text/UTF-8 reads reconstruct correctly; unrelated sessions, user/tool/thought text cannot supply the verdict; use actual message identity behavior. |
| Invalid contract | Malformed, absent, wrong-shaped, duplicate-key or ambiguous verdict output blocks with a protocol reason, without a fake substantive rejection. |
| Completion and safeguards | Accepted-looking content plus failed/missing completion, overflow, cancellation, review writes or candidate/check drift does not produce acceptance. Reuse existing checks/tests. |
| Real client and mock | Real pinned Node/acpx plus the existing mock delivers the structured reviewer result through the actual production collector and controller. Do not replace the real client with a Python fake or inject result.review directly. |
| Saved real evidence | Original review bytes validate and the isolated acceptance evaluation either passes or identifies a concrete missing binding; original state and 2/2 consumption remain unchanged. |

The real-client mock test is still zero model work: explicit mock agent, isolated homes, no provider credentials, no real DSH prompt. Keep any sanitizer separate from authoritative replay: a committed fixture may be redacted only without changing verdict semantics/framing; unredacted local evidence remains private.

Run affected tests and one necessary offline regression after code settles. No unchanged full-matrix repetition, A retest, live format-repair call, new dependencies, or extra agents. Do not touch unrelated processes.

## 7. Completion and handoff

Suggested commit, only for actual implementation changes:

```
fix(review): propagate validated reviewer verdicts
```

Report: actual fix SHA; where the production wire was missing; regression that failed before/passes after; supported output grammar; saved-evidence extraction result and isolated acceptance result; original candidate unchanged; original run still historically BLOCKED; AUTH-m2-live-2 still 2/2; new real model submissions 0.

Do not call this an uninterrupted successful M2 run. A truthful result could be: "Original live implementation and review completed; delivery was blocked by adapter result loss; patched offline reprocessing validates the saved verdict and satisfies the acceptance checks in an isolated evaluation." Use that wording only if the checks actually pass.

**End condition: existing real review evidence reaches the canonical Review and controller decision without another model call. No automatic historical finalization, no new B trial, and no new readiness platform.**

## Sources and limits

The user's latest report supplies the HFlow identities and local observations; this brief is not independent verification. Earlier HFlow instructions already required production reviewer wiring before live review and deterministic handling without model formatting repairs.

[S1] acpx v0.17.1, docs/output-formats.md: raw ACP NDJSON, output channels, and identity distinctions. Read through the GitHub connector.

`https://github.com/openclaw/acpx/blob/v0.17.1/docs/output-formats.md`

[S2] ACP v1 Prompt Turn: assistant updates, optional message identities, matching prompt completion, stop reasons. General protocol documentation does not prove which optional fields the pinned DSH emits.

`https://agentclientprotocol.com/protocol/v1/prompt-turn`

[S3] Python JSON documentation: resource bounds, repeated keys, decoder hooks, and non-standard constants.

`https://docs.python.org/3/library/json.html`

Existing conversation artifacts: original greenfield plan sections 16.3–16.5; HFlow_M2_Controlled_Live_Acceptance.md section 6.3; HFlow_B_Only_Human_Approval_3dbfeae.md (closed two-invocation allowance and preserved evidence).
