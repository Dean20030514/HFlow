# HFlow — architecture (M1 state)

HFlow is a thin deterministic controller in front of a native coding Harness. The
Harness does the reasoning; the controller does admission, budget, evidence, and
delivery accounting. This document describes **what exists today**, not the target
architecture — read the plan for that.

## Layer map

```text
CLI (cli.py: doctor/run/status/report/cancel/resume/schema)
  |
Controller (controller.py)  — finite state machine, no LLM calls
  |-- admission.py   TaskSpec + project contract -> admit or refuse
  |-- store.py       SQLite: runs / attempts / evidence, transactions, CAS
  |-- verify.py      approved checks over a frozen candidate -> evidence
  |-- workspace.py   scope containment, manifests, candidate fingerprints
  |-- report.py      status/report rendering from stored facts only
  |
HarnessDriver (contracts.HarnessDriver)
  |-- drivers/fake.py       offline driver (the only implemented one)
  |-- drivers/selected.py   M0 placeholder that refuses to run
```

## State

Task state: `DRAFT → READY → RUNNING → CHECKING → ACCEPTED`, plus `BLOCKED` and
`CANCELLED`. `CHECKING` carries its stage in `phase` (`verification`, `review`,
`integration-check`). Attempt state: `CREATED`, `ACTIVE`, `SUCCEEDED`, `FAILED`,
`CANCELLED`, `OUTCOME_UNKNOWN`, `SUPERSEDED`. Delivery is tracked separately:
`NONE → LOCAL_CANDIDATE` (nothing else is implemented).

## Five properties the code enforces

1. **Budget before dispatch.** `store.dispatch_attempt` reserves the turn and records
   the attempt in one `BEGIN IMMEDIATE` transaction. A database `CHECK` constraint
   (`turns_reserved <= turn_limit`) backs up the logic. No reservation, no process.
2. **A worker cannot accept its own work.** The driver protocol returns an invocation
   outcome, an optional candidate description and an optional review verdict. It has no
   field for task state or verification. `ResultReceipt` is constructed only by
   `controller._accept` from stored rows.
3. **Evidence is bound to a candidate.** Every evidence row carries the candidate
   fingerprint, the checks digest, and the exact command. Acceptance re-hashes the
   candidate; if it moved, the run blocks as `evidence_stale` (A09/A10/A11).
4. **A late result cannot overwrite a newer attempt.** Result application is a
   compare-and-set on `(current_attempt_id, task_revision)`; a mismatch raises and
   changes nothing (A05).
5. **Unknown means stop.** An interrupted invocation becomes `OUTCOME_UNKNOWN` with its
   reservation intact, the run blocks, and `resume` only reconciles. It never
   re-dispatches (A04).

## What is deliberately absent

No DAG engine, no distributed queue, no plugin loader, no vector memory, no web UI, no
Task/Reviewer team topology, no second task database, no credential store, and no
production Harness driver. Each omission is a scoping decision from the plan, not an
oversight; the corresponding plan item is listed in the README as future work.

## Known boundaries of M1 (do not overstate)

- **No sandbox.** `command` checks and (later) workers run as ordinary child processes.
  Scope is enforced by *detection after the fact* plus refusal to accept, not by
  confinement. A malicious worker could write anywhere this user can write.
- **The workspace is the target project.** M1 has no managed worktree or snapshot, so a
  run edits the project directly. `candidate.tree_hash` is a content fingerprint of
  supported files in scope, not a Git tree object.
- **One invocation carries both roles.** Verification and review are separate
  invocations in budget terms, but the review still reads the same working directory;
  `isolation=prompt_only` is recorded for exactly that reason.
- **`review_isolation` is recorded from enforcement, not from claims.** A reviewer that
  says it was read-only does not change the recorded level (A08).
- **No repair cycle yet.** A rejected review or failed verification blocks the run; the
  bounded repair cycle in the plan is not implemented.
- **Billing is unknown.** `provider_billed_tokens`, `provider_cost` and
  `subscription_quota_remaining` are `null` because nothing observes them.
