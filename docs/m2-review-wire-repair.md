# Reviewer result wire — repair and offline replay of the recorded B attempt

The live implementation and review both completed; delivery was blocked by **adapter result
loss**, not by the candidate and not by the reviewer's judgment. This note records the repair,
the regression that proves it, and the replay of the recorded reviewer bytes through the
production decision path.

**No new live authorization was used.** New real model submissions: **0**. `AUTH-m2-live-2`
still reads `used 2/2`.

## 1. The missing wire, confirmed in source

| Location | Before |
|---|---|
| `src/hflow/drivers/acpx_dsh.py` `collect()` | built `InvocationResult(..., review=None, ...)` unconditionally |
| `src/hflow/controller.py` `_review()` | `if review_output is None: return ReviewResult(status="changes_requested")` |
| `controller._drive` | `changes_requested` ⇒ `BLOCKED` / `review_rejected` |

So a real reviewer could never reach acceptance, and the run's recorded reason described a
*substantive* rejection that never happened. The recorded payload proves the loss:
the saved `review_json` for invocation `I-vc5pcccfog` reads `"review": null` with
`"outcome": "completed"`.

The verdict itself was never missing from the wire. acpx `--format json` emits raw ACP
JSON-RPC: the terminal prompt response answers request id `2` with `stopReason=end_turn`, and
the reviewer's answer travels as `session/update` → `agent_message_chunk` updates. In the
recorded stream the answer is the **last** of 17 assistant messages
(`messageId eb6ade90-750b-4bf3-8733-e9ccd164d89b`, line 200 of 203).

## 2. The fix

*New* `src/hflow/review.py` — the one parser, used by production and replay:

* `AnswerTranscript` reassembles the invocation's **final** assistant message from
  `agent_message_chunk` updates carrying that invocation's own `sessionId`. Chunks group by
  the optional `messageId` when present (as the pinned runtime sends it) and by contiguity
  when it is absent - never "the last NDJSON line", never the whole transcript. User text,
  thoughts, tool results and other sessions cannot supply the answer.
* `decode_review` accepts exactly one Review object and validates it against the canonical
  `contracts.ReviewOutput`. Supported forms: a bare JSON object, one fenced block (plain or
  `json`-labelled - the form the recorded reviewer used), or prose ending in one JSON object.
  A labelled `python` sample is decoration, not a result block.
* Refused, not repaired: malformed JSON, repeated keys (Python's decoder keeps the last
  value), `NaN`/`Infinity`, wrong types/enums, missing required fields, unknown fields, two
  candidate result blocks, or any non-finite constant - and no model is ever asked to fix a
  format.

`AcpxDshDriver.collect()` now decodes the verdict **only** for a `reviewer` invocation and
**only** when the turn actually completed *and* its terminal response answers the observed
`session/prompt` request. Transport validity stays separate from content: cancelled, unknown,
truncated, overflowed or unbound turns yield no verdict regardless of how accepted the text
looks.

`Controller._review()` now distinguishes the two failures that used to be identical:

| Situation | Before | Now |
|---|---|---|
| validated `changes_requested` | `review_rejected` | `review_rejected` (findings preserved) |
| missing / invalid / ambiguous verdict | `review_rejected` (false statement) | `review_protocol_error` + failed review evidence |
| reviewer turn did not complete | `review_rejected` | `review_protocol_error` with the transport reason |
| completion not bound to the prompt | `review_rejected` | `OUTCOME_UNKNOWN` (reconcile; never re-dispatch) |

No state machine was added, the original `review_rejected` history was not rewritten, and the
receipt transaction, CAS, cancellation precedence, scope checks and candidate/check
fingerprints are untouched.

## 3. Regression: fails before, passes after

Both tests drive the real client stand-in, the real `collect()` and the real `_review()`; no
fake driver and no pre-filled `InvocationResult.review`.

```text
tests/test_review_wire.py::test_a_reviewer_verdict_is_decoded_from_the_final_message
tests/test_review_wire.py::test_structured_review_reaches_the_controller
```

With `src/hflow` reverted to the committed state both fail with
`the reviewer's structured verdict was discarded` / `assert None is not None` and a
`BLOCKED` run; with the patch they pass and the run reaches
`ACCEPTED / LOCAL_CANDIDATE` with review evidence `status=passed`.

The whole suite: **183 passed, 1 skipped** (the pre-existing directory-link skip), including
`test_real_client_review.py`, which runs the *installed* acpx under Node against the project's
mock ACP agent - once with a fenced verdict (⇒ receipt) and once with prose only (⇒
`review_protocol_error`, no receipt).

## 4. The recorded reviewer bytes, replayed offline

```sh
python tools/m2_live/replay_review.py R-gkb3ld97x8 --out <diagnostic>.json
```

Read-only by construction: it copies the ledger before reading it, never imports the
controller dispatch path, launches **no** child process at all (the test traps `subprocess.*`
and `os.system`/`os.popen` during the replay), and re-hashes the store afterwards to show it
is unchanged.

| Check | Result |
|---|---|
| source run / state | `R-gkb3ld97x8`, `BLOCKED` / `review_rejected`, `delivery NONE`, no receipt, `controller_build hflow/0.0.1+3dbfeae` |
| parser build | `hflow/0.0.1+e6a303e` |
| reviewer stream | `…/invocations/I-vc5pcccfog/events.ndjson`, 403397 bytes, `sha256:041201c1…49df6`, 203 lines, 0 unparseable |
| prompt binding | request id `2` = terminal response id `2`; single session `c8fa7994…b178` |
| answer | final message `eb6ade90…d89b`, 6723 chars, `sha256:6c7cb3c9…7fa2c` |
| **extracted verdict** | **`accepted`**, 5 findings (`AC-1`,`AC-2`,`AC-3`,`EVIDENCE-zgamyka8s3`,`F-1`), digest `sha256:bbe60014…025e9` |
| reviewer's own claim vs record | the answer names candidate commit `499ece7…` - the recorded ref and worktree HEAD |
| task / project | `task.json` digest == recorded `spec_digest`; revision 2; both files hashed |
| candidate | `refs/hflow/candidates/R-gkb3ld97x8/A-7f2pbp4teu` = `499ece7043fe3267b4ff89f9a5b5bc1d70c42481` = worktree HEAD; recomputed fingerprint `sha256:5c47b12d…d405` == recorded evidence fingerprint |
| verification evidence | `E-zgamyka8s3`, `unit`, passed, exit 0, `checks_digest sha256:42216b79…ba80` == run's digest - **current**, and not re-run (stated as a limit, not repaired) |
| reviewer permissions | recorded config `defaultPermissions=approve-reads`; run note: implementer `approve-all` \| reviewer `approve-reads` |
| **isolated acceptance** | predicates pass; the real `finalize_acceptance` transaction writes a receipt in a temporary store (`isolated_task_state ACCEPTED`), explicitly a **test artifact** |
| original store | `sha256:94d8f5ba…e129` before and after - `store_mutated: false` |
| authorization | `AUTH-m2-live-2` used 2/2, `provided_by user` |

Two limits are recorded in the diagnostic rather than hidden: a hash computed now does not
prove the bytes were not edited earlier, and the approved check was re-associated (candidate
fingerprint + checks digest + command) but **not** re-executed to manufacture a fresh
timestamp.

## 5. What this does and does not establish

* Established: the saved reviewer verdict is a valid canonical `ReviewOutput`; the production
  path now carries such a verdict through `collect()` and `_review()` to the unchanged
  acceptance checks; the recorded candidate and its verification evidence still bind; and the
  acceptance predicates pass on the recorded material in an isolated evaluation.
* **Not** established: an uninterrupted successful M2 run. The original run is still
  historically `BLOCKED` / `review_rejected` with no receipt; nothing was rewritten, and the
  runner commit `3dbfeae` is not stamped as passed.
* Not done: no new live model call, no A retest, no format-repair call, no new dependency, no
  automatic historical finalization, no new readiness platform.

Post-hoc finalization of the original run, if it is ever wanted, is a separate explicit local
action through the controller checks and CAS with the original failure and a new processing
version/time recorded - not a hand-edited SQLite state and not a forged verdict.
