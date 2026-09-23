# HFlow — architecture (current state)

HFlow is a thin deterministic controller in front of a native coding Harness. The
Harness does the reasoning; the controller does admission, budget, evidence, and
delivery accounting. This document describes **what exists today**, not the target
architecture — read the plan for that.

It was first written at M1 and has been corrected since; where a section describes a
capability that arrived later (the production Driver, Git worktrees, the review wire), the
module and its evidence are named so the claim can be checked. A statement is only as strong
as the artifact beside it: implemented code, an offline test, or a recorded live execution on
one binding — never a neighbour's success.

## Layer map

```text
CLI (cli.py: doctor/run/status/report/cancel/resume/clean/schema)
  |
Controller (controller.py)  — finite state machine, no LLM calls
  |-- admission.py    TaskSpec + project contract -> admit or refuse (nothing is created first)
  |-- authorization.py  one-shot, bound, user-attested approval artifact for a real run
  |-- store.py        SQLite: runs / attempts / evidence / authorizations, transactions, CAS
  |-- verify.py       approved checks over a frozen candidate -> evidence
  |-- workspace.py    scope containment, manifests, candidate fingerprints
  |-- gitworkspace.py Git worktrees, candidate freezing, candidate refs, status parsing
  |-- review.py       the one reviewer-answer parser, shared by the driver and replay
  |-- cleanup.py      guarded release of one run's managed worktree
  |-- report.py       status/report rendering from stored facts only
  |
HarnessDriver (contracts.HarnessDriver / LifecycleDriver)
  |-- drivers/selected.py   the single place a binding becomes a driver object
  |-- drivers/acpx_dsh.py   the production transport: acpx -> official DSH ACP
  |-- drivers/acp_events.py neutral projection of the client's event stream
  |-- drivers/winjob.py     Windows Job Object process boundary
  |-- drivers/fake.py       offline driver for tests and examples
```

## State

Task state: `DRAFT → READY → RUNNING → CHECKING → ACCEPTED`, plus `BLOCKED` and
`CANCELLED`. `CHECKING` carries its stage in `phase` (`verification`, `review`,
`integration-check`). Attempt state: `CREATED`, `ACTIVE`, `SUCCEEDED`, `FAILED`,
`CANCELLED`, `OUTCOME_UNKNOWN`, `SUPERSEDED`. Delivery is tracked separately:
`NONE → LOCAL_CANDIDATE`.

No run reaches `INTEGRATED` or `PUBLISHED`: a TaskSpec asking for one of those delivery levels
is refused at admission, before any run row exists, instead of being delivered as a local
candidate and reported as the requested level.

## Six properties the code enforces

1. **Admission refuses before anything is created.** `controller.run_task` validates the
   TaskSpec against the project contract as its first statement — before an authorization is
   registered, a run row inserted, a worktree attached or a turn reserved. A refusal is an
   `exit 2` with the issues listed, and leaves no run state at all; it is a different event
   from a run that dispatches and then blocks (`exit 3`).
2. **Budget before dispatch.** `store.dispatch_attempt` reserves the turn and records
   the attempt in one `BEGIN IMMEDIATE` transaction. A database `CHECK` constraint
   (`turns_reserved <= turn_limit`) backs up the logic. No reservation, no process.
3. **A worker cannot accept its own work.** The driver protocol returns an invocation
   outcome, an optional candidate description and an optional review verdict. It has no
   field for task state or verification. `ResultReceipt` is constructed only by
   `controller._accept` from stored rows.
4. **Evidence is bound to a candidate.** Every evidence row carries the candidate
   fingerprint, the checks digest, and the exact command. Acceptance re-hashes the
   candidate; if it moved, the run blocks as `evidence_stale` (A09/A10/A11).
5. **A late result cannot overwrite a newer attempt.** Result application is a
   compare-and-set on `(current_attempt_id, task_revision)`; a mismatch raises and
   changes nothing (A05).
6. **Unknown means stop.** An interrupted invocation becomes `OUTCOME_UNKNOWN` with its
   reservation intact, the run blocks, and `resume` only reconciles. It never
   re-dispatches (A04).

## What is deliberately absent

No DAG engine, no distributed queue, no plugin loader, no vector memory, no web UI, no
Task/Reviewer team topology, no second task database, no credential store, and no second
production transport. Each omission is a scoping decision from the plan, not an oversight;
the corresponding item is listed in the README as future work. The one production Driver
exists (`drivers/acpx_dsh.py`) and is bound through `drivers/selected.py`.

## Known boundaries (do not overstate)

- **No sandbox.** `command` checks and workers run as ordinary child processes with the
  current user's rights. Scope is enforced by *detection after the fact* plus refusal to
  accept, not by confinement, so a worker could write anywhere this user can write. No
  credential confinement either: a check inherits the process environment, which is why the
  approved-check runner is the only thing HFlow executes on a project's behalf.
- **A managed worktree is a workspace boundary, not a permission boundary.** When the
  TaskSpec sets `workspace.mode = worktree`, `gitworkspace.py` creates a detached worktree at
  a fixed base commit, the controller freezes the candidate as a real commit and keeps it
  reachable through `refs/hflow/candidates/<run-id>/<attempt-id>`. The user's HEAD, index,
  working files, stash and branches are not written to — but the worktree shares the source
  repository's Git objects and admin files, and a process running as this user can still read
  and write outside it. With `mode = in_place` a run edits the project root directly, exactly
  as M1 did.
- **The two candidate identities stay apart.** `candidate.git_commit`/`git_tree` are Git
  objects and identify what was verified; `candidate.fingerprint` is a content hash over the
  write scope and detects drift afterwards. `workspace.py` produces the fingerprint; a run
  without a worktree reports an empty `git_commit` and says so, rather than presenting one as
  the other.
- **Implementer and reviewer are separate invocations, but the reviewer is not sandboxed.**
  They are separate processes with separate reserved turns, separate sessions and separate
  allowance claims, and a reviewer never inherits the implementer's write permission. The
  review still runs against the same checkout with `isolation=prompt_only` recorded for
  exactly that reason; there is no enforced read-only boundary.
- **`review_isolation` is recorded from enforcement, not from claims.** A reviewer that
  says it was read-only does not change the recorded level (A08).
- **A reviewer's verdict travels as text, not as a protocol field.** ACP's terminal prompt
  response carries a stop reason, not HFlow's `ReviewOutput`; the verdict is an assistant
  message. The production driver therefore reassembles the reviewer's *final* message from
  eligible `agent_message_chunk` updates of that invocation's own session and decodes one
  canonical `ReviewOutput` from it (`src/hflow/review.py`). Missing, malformed, ambiguous or
  unbound output blocks as `review_protocol_error` and is recorded as failed review evidence -
  it is never described as the reviewer requesting changes, and a verdict can never be filled
  in on the model's behalf. Only the review invocation may produce it: an implementer whose
  output happens to contain a verdict-shaped object still reports `review=None`.
- **No repair cycle yet.** A rejected review or failed verification blocks the run; the
  bounded repair cycle in the plan is not implemented.
- **A stop ends local processes, and nothing more.** The production driver owns one
  invocation's process boundary (a Windows Job Object, `drivers/winjob.py`): the child is
  created suspended inside the boundary so ownership exists from its first instruction, and
  the boundary is closed after a bounded grace period. That is what `mechanism="forced"`
  means. It is not a cooperative protocol cancellation — `acpx cancel` targets a persisted
  session's queue owner and the selected one-shot `exec` path has none — and it is not a
  statement about a process that leaves the job, about another platform, about a remote model
  request, or about remote billing having stopped. A stop that cannot be confirmed stays
  `still_running`/`unknown`, and the run blocks instead of re-dispatching.
- **Billing is unknown.** `provider_billed_tokens`, `provider_cost` and
  `subscription_quota_remaining` are `null` because nothing observes them. Do not read `null`
  as `0`.
- **Authorization is trusted-local.** A real run needs a one-shot artifact bound to that exact
  execution and carrying the user's own text (`authorization.py`). What is enforced is the
  binding, the per-id consumption cap and the literal `provided_by: user`; what is *not*
  enforced is provenance — nothing distinguishes bytes a user typed from the same bytes written
  by the executing agent, and a fresh authorization id resets the allowance. Unattended
  execution is therefore disabled rather than merely discouraged.

## Recorded live evidence, and what it covers

| Execution | Recorded result | Covers |
|---|---|---|
| M0 live prompt turn (acpx -> real DSH) | one turn completed with a stop reason | the transport handshake and a single prompt turn |
| M2 trial A: forced stop | PASS — helper owned by the invocation's job, boundary empty after the stop | that machine, that acpx/DSH version, that binding, with the client in `approve-reads` mode (an invented `permissionPolicy` key was ignored, so the client default applied) |
| M2 attempt 2 (`3dbfeae`) | implementer completed, candidate frozen and checked, real reviewer returned `accepted` — the controller still recorded `BLOCKED` / `review_rejected` | that the pipeline ran end to end **and** that the then-current adapter discarded the verdict; it is not a success |
| later offline reprocessing | `ACCEPTED` / `LOCAL_CANDIDATE` recorded for that same candidate from its own evidence | that the repaired parser carries the recorded verdict through the acceptance predicates. No model was called, no check re-executed, no allowance consumed |

None of these rows certify cooperative cancellation, a filesystem sandbox, descendant control,
or remote billing termination. Details and provenance:
`docs/m2-live-acceptance-result.md`, `docs/m2-review-wire-repair.md`, `docs/m0-results.md`.

