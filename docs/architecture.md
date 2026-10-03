# HFlow — architecture (current state)

HFlow is a thin deterministic controller in front of a native coding Harness. The
Harness does the reasoning; the controller does admission, budget, evidence, and
delivery accounting. This document describes **what exists today**, not the target
architecture — read the plan for that (`docs/batch-e-plan.md` for the root budget and the
bounded repair).

It was first written at M1 and has been corrected since; where a section describes a
capability that arrived later (the production Driver, Git worktrees, the review wire, the batch
E1 root ledger, the batch E2 bounded repair), the module and its evidence are named so the claim
can be checked. A statement is only as strong as the artifact beside it: implemented code, an
offline test, or a recorded live execution on one binding — never a neighbour's success.

## Layer map

```text
CLI (cli.py: doctor/prepare/run/status/report/cancel/resume/clean/schema)
  |
prepare.py  the one resolution `prepare` and `run` share: task overrides, project contract,
  |         per-role bindings (profiles.py), resolved launch, write permission, base commit
  |
Controller (controller.py)  — finite state machine, no LLM calls
  |-- admission.py    TaskSpec + project contract -> admit or refuse (nothing is created first);
  |                   the dispatch preconditions, repair-policy rules included
  |-- authorization.py  one-shot, bound, user-attested approval artifact for a real run
  |-- packet.py       the role input packets, rendered from stored facts, 32 KiB bound
  |-- store.py        SQLite transactions, reserve_dispatch, compare-and-set, stop-conditional writes
  |-- migrate.py      table DDL and numbered storage versions (v5); the tables are runs / attempts /
  |                   evidence / run_notes / authorizations / root_budgets / invocations /
  |                   run_repair_records
  |-- verify.py       approved checks over a frozen candidate -> evidence (with exit_reason)
  |-- artifacts.py    bounded output capture and the allowlisted check environment
  |-- workspace.py    scope containment, deny rules, manifests, candidate fingerprints
  |-- gitworkspace.py Git worktrees, base-commit resolution, candidate freezing, candidate refs
  |-- review.py       the one reviewer-answer parser, shared by the driver and replay
  |-- cleanup.py      guarded release of one run's managed worktree
  |-- report.py       status/report rendering from stored facts only
  |-- runtime.py      the controller build id recorded into every run
  |
HarnessDriver (contracts.HarnessDriver / LifecycleDriver)
  |-- drivers/selected.py   the single place a binding becomes a driver object
  |-- drivers/acpx_dsh.py   the production transport: acpx -> official DSH ACP
  |-- drivers/acp_events.py neutral projection of the client's event stream
  |-- drivers/dsh_surfaces.py what DSH reads at launch beyond the packet, observed per role
  |                         (files read, a .env never opened; never executed, enforced or bound)
  |-- drivers/winjob.py     Windows Job Object process boundary
  |-- drivers/fake.py       offline driver for tests and examples
```

Every structure is defined in `contracts.py`, except the authorization artifact
(`AuthorizationRecord` / `AuthorizationBinding`), which lives in `authorization.py`. `hflow
schema` generates JSON Schema for both, together with the other hand-written inputs
(`MachineProfile`, `RootBudgetPlan`, `RepairPolicy`).

`hflow cancel` and `hflow resume` do not build the driver a run was started with. They build an
observer per role from the run's recorded effective configuration: a role recorded as the offline
fake gets a fresh fake (which answers `unknown` for an invocation it did not start), and any
other role - or a run with no recorded configuration, labelled `unrecorded` - gets an observer
that holds no handle, answers a stop `unknown` and a reconcile `unknown`, and refuses to start
anything. A controller's scratch and check artifacts go under the data directory of its own
ledger (`<data-dir>/artifacts/...`, `<data-dir>/invocations/...`).

## State

Task state: `DRAFT → READY → RUNNING → CHECKING → ACCEPTED`, plus `BLOCKED` and
`CANCELLED`. `CHECKING` carries its stage in `phase` (`verification`, `review`,
`integration-check`). Attempt state: `CREATED`, `ACTIVE`, `SUCCEEDED`, `FAILED`,
`CANCELLED`, `OUTCOME_UNKNOWN`, `SUPERSEDED`. Delivery is tracked separately:
`NONE → LOCAL_CANDIDATE`.

No run reaches `INTEGRATED` or `PUBLISHED`: a TaskSpec asking for one of those delivery levels
is refused at admission, before any run row exists, instead of being delivered as a local
candidate and reported as the requested level.

Each top-level invocation also has a ledger state (`invocations.state`, `InvocationStartState`),
kept apart from the attempt state because a reservation, a launch request, a launch and a result
are four different facts:

| State | Meaning |
|---|---|
| `reserved` | the dispatch transaction committed: the allowance is spent and the intent is durable. Not a launch, not a process, not a model request |
| `requested` | the controller asked a driver to launch it, and no spawn report has arrived yet |
| `started` | the launch happened. Whether it created an operating-system child is a separate fact (`spawn_kind`, `process_pid`): the offline fake launches in-process and creates none |
| `not_started` | no launch happened (a stop won the handoff, or the driver refused before any process). The allowance stays consumed |
| `settled` | a result was observed and applied, with the controller's classification of it |
| `unknown` | a launch happened and was never settled. Blocks the root; no refund, no retry, no re-dispatch |
| `launch_unknown` | a launch was requested and the controller never learned whether it happened. Counts as no process, and blocks the root until an operator looks |

`reserved`, `requested` and `started` are the **open** states. Settling is a compare-and-set from
those three only (`store.settle_invocation`): a `not_started`, `settled`, `unknown` or
`launch_unknown` entry is never moved by a settlement or a stop, and a refused settlement is
recorded as a `dispatch:` run note naming the state that stands. A process count is never derived
from a result: `processes` (and its historical alias `ever_started`) counts invocations whose
driver reported a pid. A driver that never sends a spawn report is read from the row itself - an
entry still `requested`, with no `started_at` and `spawn_kind=unknown`, means no report arrived;
completed work then records a launch with `spawn_kind=unknown` and no process, and a cancelled
result with no work records `not_started` **only when the stop was already recorded when the
controller handed the invocation to the driver** (read after the launch request is recorded and
before `start`), because only then did the driver's gate provably refuse. A cancelled, workless
return after a stop recorded while the role was running is no such proof - a silent driver may
have worked and then honoured the stop - so the entry stays `requested`: a confirmed stop then
records `launch_unknown`, and an unconfirmed one leaves it open (blocking the root) until
`resume` records `launch_unknown`. A driver that *raises* without a spawn report leaves
the entry `requested` - a driver can raise after creating a process, so "no report and an
exception" is read as an unconfirmed launch that blocks the root, never as "never started". For
the same reason a confirmed stop records `not_started` only when no driver was ever asked; a
launch that was requested and never reported becomes `launch_unknown`. A spawn report that
arrives after an entry was closed as `launch_unknown` never reopens it
(`store.record_invocation_spawn`): a reported launch records its process facts and makes the entry
`unknown` - not `started`, which is open and could be settled into releasing the root - and a
reported non-launch records `not_started`, the same results the closure would have reached had
the report come first.

## Six properties the code enforces

1. **Admission refuses before anything is created.** `controller.run_task` validates the
   TaskSpec against the project contract as its first statement — before an authorization is
   registered, a run row inserted, a worktree attached or a turn reserved. A refusal is an
   `exit 2` with the issues listed, and leaves no run state at all; it is a different event
   from a run that dispatches and then blocks (`exit 3`).
2. **Budget before dispatch.** `store.reserve_dispatch` is the one dispatch transaction of
   batch E1, for both roles: in one `BEGIN IMMEDIATE` it checks ownership, phase, the absence of
   a stop, the root (unresolved invocations, deadline, room for the rest of the loop, repair
   count) and the authorization, then creates or attaches the attempt row, inserts the
   invocation row and increments the run, root and authorization counters - or rolls all of it
   back. Replaying the same invocation id returns the recorded intent and charges nothing.
   Database `CHECK` constraints (`turns_reserved <= turn_limit`,
   `used_top_level_submissions <= max_top_level_submissions`, `used_repairs <= max_repairs`)
   back up the logic. No reservation, no process. (`store.dispatch_attempt`, the older
   run-and-attempt-only transaction, is no longer on the production path.)
3. **A worker cannot accept its own work.** The driver protocol returns an invocation
   outcome, an optional candidate description and an optional review verdict. It has no
   field for task state or verification. `ResultReceipt` is constructed only by
   `controller._accept` from stored rows.
4. **Evidence is bound to a candidate.** Every evidence row carries the candidate
   fingerprint, the checks digest, and the exact command. Acceptance re-hashes the
   candidate; if it moved, the run blocks as `evidence_stale` (A09/A10/A11).
5. **A late result cannot overwrite a newer attempt or a stop.** Result application is a
   compare-and-set on `(current_attempt_id, task_revision)`; a mismatch raises and changes
   nothing (A05). Both roles follow one rule once a stop is recorded, confirmed or not: the
   result's first write is conditional on the stop in the same statement -
   `finish_attempt(unless_stopped=True)` for the implementer,
   `attach_review_result_unless_stopped` for the reviewer - and the ledger entry is settled only
   after that write succeeded. A result refused by it is a `late_result` note: the attempt and the
   ledger entry keep the state the stop left (after an unconfirmed stop the entry stays open and
   the root blocked until `resume` marks it `unknown`), the run keeps the stop's block, an
   implementer's result creates no candidate commit and no `refs/hflow/candidates/*` ref, and a
   reviewer's records no verdict, no review evidence and no process identity. A stop that commits
   after the write keeps the applied result; for the implementer, a stop that lands after the
   result was applied but before the freeze still prevents the freeze and the ref. When a stop
   commits while the acceptance is being written, `finalize_acceptance` refuses the late success
   in its own transaction, and the run is left in the stop's recorded state with no receipt.
6. **Unknown means stop.** An interrupted invocation becomes `OUTCOME_UNKNOWN` with its
   reservation intact, the run blocks, and `resume` only reconciles. It never
   re-dispatches (A04). The same block follows a completion that answers no observed prompt
   (`unbound_completion`, either role), a prompt answered with a JSON-RPC error
   (`prompt_error_response`) or with a stop reason outside ACP v1's set (`unknown_stop_reason`),
   a client whose boundary could not be confirmed empty (`boundary_not_empty`), and a controller
   interrupted (Ctrl+C, `SystemExit`) while a driver was starting or running an invocation.

## Bounded repair (batch E2)

One failed round may be followed by **one** second implementer attempt in the same run, on the
same revision, starting from the frozen candidate, followed by fresh checks and a fresh
independent review - only when the task carries an explicit `repair_policy`, and only for a
clean, declared business check failure or a substantive reviewer rejection with at least one
usable finding. The rules, the triggers and everything that stops a repair are in
`docs/operations.md`, "When a repair may happen". What the architecture adds:

- the repair round is its own `attempts` row (`is_repair = 1`); the schema key
  `UNIQUE (run_id, task_revision, role, is_repair)` makes a third implementer attempt impossible
  to insert;
- `store.reopen_for_repair` moves the run back to its implementation phase in one transaction,
  immediately before the repair reservation, and refuses a run another controller owns, a run
  with a stop recorded and a terminal run - so a repair never revives a stopped or ended run;
- three candidate identities stay apart (`CandidateIdentity`): the original base, the previous
  round's candidate and the new candidate. The receipt's base is the original base and its paths
  are the Git diff from that base to the final candidate, and the repair round's reviewer is
  shown the same cumulative diff plus a labelled "this round's change";
- every decision, refusals included, is one `RepairRecord` row in `run_repair_records`
  (`allowed`, `not_enabled`, `not_a_business_failure`, `no_findings`, `already_repaired`,
  `budget_exhausted`, `deadline_reached`, `stop_requested`, `no_content_change`,
  `workspace_drift`);
- the repair round reuses the first round's worktree only while it is exactly the previous
  candidate (HEAD, tree, a clean status and no index entry flagged assume-unchanged or
  skip-worktree), and only while the shared Git metadata still matches the pre-dispatch
  snapshot, refusing otherwise as `workspace_drift` before anything is bought
  (`_reconcile_repair_workspace`). Ignored files present then can only be the previous round's
  check or review byproducts (the first freeze refused any ignored path off its allowlist). Those
  outside what the scoped fingerprint hashes are carried: the repair's freeze accepts exactly
  those paths, as literal entries, and the round's manifest comparison still refuses a worker
  change to them as outside the scope; nothing is deleted, an ignored file is never staged, and
  new ignored paths still refuse. One a previous check left inside a `write_allow` directory,
  which the fingerprint would hash although no candidate commit holds it, would let the
  fingerprint and the frozen commit disagree, so it refuses the repair as `workspace_drift`
  instead;
- a repair on a run with a real transport requires a root binding; a fully offline fake run may
  repair rootless, charged to no root counter.

## What is deliberately absent

No DAG engine, no distributed queue, no plugin loader, no vector memory, no web UI, no
Task/Reviewer team topology, no second task database, no credential store, and no second
production transport. Each omission is a scoping decision from the plan, not an oversight;
the corresponding item is listed in the README as future work. The one production Driver
exists (`drivers/acpx_dsh.py`) and is bound through `drivers/selected.py`.

## Known boundaries (do not overstate)

- **DSH's own launch inputs are recorded, not controlled.** DSH reads a workspace `.env`, its
  home's `.env`, `cordis.patch.yml` layers, AGENTS.md/CLAUDE.md files and skills on its own
  (documented upstream at dsh-v0.2.0-rc.2, not observed). `drivers/dsh_surfaces.py` records,
  just before each spawn, what is at those fixed paths - presence, size and a SHA-256, a `.env`
  by presence and size only - plus the DSH_* variable names reaching the child and the client and
  carrier versions read from files. Nothing in that record is enforced or bound into an approval,
  and DSH reads the files after the look. With `DSH_HOME` unbound the child's DSH home is the
  per-invocation, empty `<data-dir>/invocations/<id>/home/.dsh` (inferred from upstream source).
- **No sandbox.** `command` checks and workers run as ordinary child processes with the
  current user's rights. Scope is enforced by *detection after the fact* plus refusal to
  accept, not by confinement, so a worker could write anywhere this user can write. The
  detection covers the task's `write_allow`, the task's and the project's `write_deny`, and a
  built-in deny list (`.git` and `.hflow` at the workspace root, `.acpxrc.json` at any depth):
  a denied path blocks `scope_violation` before the freeze, even inside an allowed directory.
  No credential confinement either: a check is given an allowlisted environment
  (`artifacts.py`), but it runs with this user's rights and can read whatever the user can, which
  is why the approved-check runner is the only thing HFlow executes on a project's behalf.
- **A managed worktree is a workspace boundary, not a permission boundary.** When the
  TaskSpec sets `workspace.mode = worktree`, `gitworkspace.py` creates a detached worktree at
  a fixed base commit, the controller freezes the candidate as a real commit and keeps it
  reachable through `refs/hflow/candidates/<run-id>/<attempt-id>`. The base is resolved once to
  a SHA (`prepare.resolve_run`, bound into the authorization) and used for the worktree, every
  round's candidate identity and the receipt, so a branch that moves during the run changes
  nothing. The freeze first requires the worktree's HEAD to be the commit the round started from
  (`freeze_candidate(expected_head=...)`: the original base in round one, the previous candidate
  in a repair). A worker that ran `git commit`, `--amend`, `reset` or `checkout` moved HEAD, and
  its commit never passed the scope and deny checks (they read the uncommitted status, which a
  commit leaves clean), so that freeze is refused `scope_violation` rather than built on or
  delivered. It refuses the same way, before reading the status, when an index entry is flagged
  assume-unchanged or skip-worktree (`GitRepo.index_flagged_paths`, from `git ls-files -v`):
  status, `git add` and the staged diff trust the index for such an entry, so the commit would
  hold bytes the checks did not read. The flags are never cleared. It then stages each literal
  `write_allow` entry (deletions included), refuses anything staged outside the scope or denied,
  and refuses as incomplete if the worktree still shows a change afterwards. After the freeze -
  before a candidate ref is written or a check runs - and again at acceptance, before a receipt
  names the paths, the whole diff from the original base to the candidate is held to
  `write_allow`, both `write_deny` lists and the built-in deny list. Every path list (staged paths, round and reviewer lists, `candidate_paths`) is taken with
  `--no-renames`, so a move names its deleted source as well as its new path. Every Git command
  that goes through `GitRepo` runs with forced configuration (`_base_env`: `GIT_CONFIG_COUNT`,
  applied after the global, repository and worktree files, and appended to an inherited
  `GIT_CONFIG_PARAMETERS`, which git reads after that list, so a caller's `git -c` cannot override
  it): `core.hooksPath` set to an empty, HFlow-owned temporary directory, `core.fsmonitor=false`,
  `commit.gpgsign=false`, `core.ignoreStat=false` and `core.sparseCheckout=false` (so HFlow's own
  `worktree add` never checks an entry out flagged assume-unchanged or skip-worktree), plus
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_NO_REPLACE_OBJECTS=1` and a fixed identity. Inherited
  repository-locating variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`)
  are dropped, because every call names its repository by its working directory. The freeze commit also passes
  `--no-verify`. No repository, user, worker or caller hook, monitor command or signer runs during
  `worktree add`, the freeze or a ref write; the user's global config is still read for everything
  else. Filters are not disabled (that would break LFS); instead `GitRepo.metadata_snapshot`
  digests the shared metadata HFlow's git reads from outside the tree - every non-`command`
  configuration key in the checkout and the worktree, the files they come from, both
  `config.worktree` files, `info/attributes` and the global attributes file - into a
  `GitMetadataSnapshot` held in controller memory for the run (never stored), taken right after
  `worktree add`. `Controller._git_metadata_refusal` compares it before the freeze, before the
  repair reconcile and at acceptance and refuses `scope_violation` on any difference;
  `Controller._note_git_metadata_at_exit` compares once more, note-only and never raising, when a
  worktree run ends without a receipt for another reason; the note is added to the run's outcome
  notes, and `inspect_run` projects every `git_metadata: changed` / `unreadable` note as
  `git_metadata_notes`, which `status` and `report` show. Git output that is not valid text makes
  the snapshot unreadable (a `GitError`), never a crash. An in-scope `.gitattributes` in the delivery is recorded
  as a receipt limitation, not refused. (`hflow clean`'s read-only `git worktree list` does not go through `GitRepo`.) The user's HEAD,
  index, working files, stash and branches are not written to — but the worktree shares the
  source repository's Git objects and admin files, and a process running as this user can still
  read and write outside it. A `write_allow` entry that is, or passes through, a symbolic link or
  junction in the checkout is refused at admission (a link can name a different place in the
  worktree), and an entry that only starts escaping the worktree during a run blocks the run
  `scope_violation` (the root is released) instead of raising. With `mode = in_place` a run edits
  the project root directly, exactly as M1 did.
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
- **A turn is bound to its own prompt, and a verdict travels as text.** ACP's terminal prompt
  response carries a stop reason, not HFlow's `ReviewOutput`. The production driver takes a
  turn's stop reason only from the response whose id equals the last observed `session/prompt`
  id: a terminal response that answers no observed prompt is `OUTCOME_UNKNOWN` /
  `unbound_completion` for implementer and reviewer alike (no freeze, no checks, no verdict); a
  prompt settled as `max_tokens`, `max_turn_requests` or `refusal` stays `FAILED
  stop_reason_<reason>` whatever a later stop-reason response says; a stop reason outside ACP v1's
  closed set is `OUTCOME_UNKNOWN` / `unknown_stop_reason`, and the event projection and the result
  share the set (`acp_events.V1_STOP_REASONS`); a JSON-RPC error answering the prompt is
  `OUTCOME_UNKNOWN` / `prompt_error_response`, with its code and bounded message recorded - and so
  is a prompt answered with both an error and a stop reason, in either order, since a request is
  answered once - while an error with another id, or with the prompt's id after a request from the
  agent reused it, is not attributed; a response with no `stopReason` settles nothing
  (`no_stop_reason`); and `stopReason=cancelled`
  with no stop requested for that invocation is `FAILED cancelled_unrequested`, because DSH also
  settles a prompt as cancelled when it disposes of a session. `end_turn` is turn settlement, not
  success - acceptance is decided by checks and review. Every bound result whose stream was read
  to its end records where the prompt response fell and how many updates for the prompt's session
  followed it (`stream_order`), from one reading of the reader's drained flag that the verdict
  decision uses too. The reviewer's verdict is reassembled from the `agent_message_chunk` updates
  the driver observed for the session the first `session/prompt` request named - a chunk for any
  other session, or with no `sessionId` (counted in a limitation,
  `agent_message_chunk_other_session=N`), a chunk observed before that request, or a request that
  names no session, leaves no verdict; the offline replay tool binds the same way and refuses
  any of these streams - and decoded into one canonical `ReviewOutput` (`src/hflow/review.py`).
  Chunks group into messages by ACP `messageId`, and only a different id starts a new message, so a
  same-id thought (DSH sends reasoning under the message's own id), a usage update or a tool call
  does not split one. The answer is the last message's message-chunk text in stream order, and
  thought text is never part of it. A `messageId` that returns after another message started
  rejects the transcript. Without `messageId`, a non-message update or a sequence gap ends a
  message. A message chunk for the prompt's session after the bound prompt response, or a stream
  not read to its end, means no verdict (`review_protocol_error` / `review_ambiguous`); the drain
  is checked before the transcript is read, so a line the reader adds while `collect` folds the
  result cannot leave a verdict behind. Missing, malformed or ambiguous output, or a reviewer turn
  that `FAILED`, blocks as `review_protocol_error` and is recorded as failed review evidence - it
  is never described as the reviewer requesting changes, and a verdict can never be filled in on
  the model's behalf. A reviewer turn whose outcome is unknown blocks as
  `outcome_unknown`, and its ledger entry is settled as unknown (the ledger records the
  controller's classification, not the driver's raw outcome), so `resume` can reconcile it.
  Only the review invocation may produce a verdict: an implementer whose output happens to
  contain a verdict-shaped object still reports `review=None`.
- **A stop ends local processes, and nothing more.** The production driver owns one
  invocation's process boundary (a Windows Job Object, `drivers/winjob.py`): the child is
  created suspended inside the boundary so ownership exists from its first instruction, and
  the boundary is closed after a bounded grace period. That is what `mechanism="forced"`
  means. It is not a cooperative protocol cancellation: the pinned acpx `exec` path sends
  `session/cancel` only when its own process receives SIGINT/SIGTERM/SIGHUP, and HFlow starts the
  client with `CREATE_NEW_PROCESS_GROUP`, which disables Ctrl+C for that group on Windows, while
  Ctrl+Break arrives as SIGBREAK, which acpx does not handle (M0 observed a CTRL_BREAK killing the
  client, exit `0xC000013A`, before any cancel reached the agent). That is reasoned and observed
  once, not a delivery that was tried and measured. A client that has exited is not an empty
  boundary: a stop asks the Job, and is `confirmed_stopped` only when the boundary is empty
  (`forced` if this stop had to terminate leftovers, `none` if it was already empty). A live stop
  also needs the client process itself shown to have exited: the driver's own `Popen` handle
  reporting its exit, its process object signalled, or `OpenProcess` answering
  `ERROR_INVALID_PARAMETER`. A process that exists but cannot be opened (`ERROR_ACCESS_DENIED`, or
  any other failure) is unanswered, never gone (`winjob.process_gone` returns `None`), and leaves
  the stop `unknown` unless `Popen` saw the exit. When the client exits, `collect` terminates
  whatever is still in the Job, keeps the outcome and adds
  `descendants_terminated_after_client_exit: N`; a Job that cannot be emptied makes the result
  `OUTCOME_UNKNOWN` / `boundary_not_empty`. Each invocation's Job and its `stdout.ndjson` handle
  are closed when its result is collected, and the controller calls the driver's optional
  `release(invocation_id)` once a result is applied, or recorded as a late-result note (a failed
  release is a `dispatch:` note and changes no decision). `release` never blocks on the client's
  pipes: its reader joins are bounded (`RELEASE_JOIN_SECONDS`), and a stderr pipe whose reader is
  still blocked - some holder of its write end outlived the teardown - is left to that daemon
  reader, which closes it at EOF, and recorded in `driver.release_notes(invocation_id)` ("stderr
  reader still blocked at release ..."). None of this is a statement about a process that leaves
  the job, about another platform (off Windows the boundary is `direct_child_only` and its
  terminate does nothing, so a forced stop or a deadline teardown does not kill even the direct
  child), about a remote model request, or about remote billing having stopped. A stop that
  cannot be confirmed stays `still_running`/`unknown`, and the run blocks instead of
  re-dispatching.
- **A stop goes to the role that is running, and both sides of a handoff are coordinated.**
  Implementer and reviewer are separate invocations (`attempts.invocation_id` /
  `attempts.review_invocation_id`) and may be separate driver objects. `Controller.cancel`
  records the intent first and then decides from the run's state as read *after* the intent: a
  live run gets its receipt and its terminal block (`cancelled_by_operator` for a confirmed stop,
  `outcome_unknown` otherwise) in **one** statement (`store.record_cancel_outcome`) and its
  attempt and ledger bookkeeping afterwards, so a failure in that bookkeeping is a `dispatch:`
  note on a run that is already terminal, and a failure of that one write leaves only the intent,
  so a retried cancel asks the driver again and ends the run. A receipt found on a run that is
  still live (a database written while the two were separate writes) has its block re-applied
  without asking the driver again. A run that had already ended (`BLOCKED`, `CANCELLED`) still
  has its last invocation asked to stop, and its receipt is recorded with
  `run_already_ended=true`, but its block, its attempt and its ledger entries are not touched, so
  an `outcome_unknown` run stays reconcilable (`ACCEPTED` is never reopened; nothing is asked to
  stop there). On a rootless run the implementer's registration is conditional on the stop as
  well, so a stop between the reservation and that registration ends the attempt `CANCELLED`
  with no driver asked and the reserved turn spent. It resolves the pair from the run's recorded
  `phase` - a review in progress means the reviewer's invocation through `reviewer_driver` -
  records which role, driver and
  invocation were asked as a run note, and reports the driver's facts. Coordination lives in
  the store and in the driver, never in a controller read: `register_attempt_invocation_unless_stopped`
  and `block_unless_stopped` write with `WHERE cancel_intent_at IS NULL`, and the driver's gate
  (`_gate`, a `Condition`) is held while it publishes an invocation's handle **and** creates the
  child, which *both* stop entry points - `cancel_handle` and `cancel(invocation_id)` - take to
  record their request. A missing handle is not an answer while a spawn is in flight: a stop that
  takes the gate waits for that critical section to end - normally by waiting to *acquire* the
  gate, which the spawn releases only after publishing - and then acts on what the spawn
  published. The wait is bounded by the spawn, never by the invocation: the gate is released
  before the result is awaited, so a stop can wait for a process to be created but not for the
  model call it is stopping to finish. The consequences that matter operationally: a stop that
  wins that gate means no process is created at all (`DriverHandle.start_cancelled`, reported as
  `cancelled` with `agent_turns=0`), and a stop that loses it finds a published handle and
  terminates the process. `InvocationRequest` carries `stop_requested` so the driver asks the
  run's state at the instant of the spawn. Reconciliation (`resume`) resolves the same way. A
  stop from another process (`hflow cancel` against a run a different controller is executing)
  holds no handle, so it reports `unknown` and blocks the run `outcome_unknown`.
- **The launch refuses a workspace client config.** acpx always loads `.acpxrc.json` from
  `--cwd` and lets it override HFlow's per-invocation config, including the agent argv
  (`workspace_client_config`). A file already in the workspace a real run starts from - the
  project root for an in-place run (a directory listing, `_workspace_client_config`), the base
  commit's root tree for a worktree run (`git ls-tree --name-only <base>`, no pathspec, since a
  pathspec matches case-sensitively) - is found by `prepare.start_workspace_client_config` and
  refused through `admission.predictable_dispatch_problems`, the one rule `prepare` and the
  controller's dispatch gate share, before a run row, an authorization record, a reservation or a
  process exists. One that appears later (the reviewer runs on the worktree the implementer
  wrote) is refused inside the driver's spawn gate, before any process exists but with the
  dispatch already reserved. Both checks match the name without regard to case among the
  workspace root's own entries (acpx's open finds `.ACPXRC.JSON` on a case-insensitive
  filesystem) and name the spelling they found.
- **A candidate's harness context files are recorded, not refused.** `workspace.dsh_context_paths`
  classifies the `--no-renames` list `_attempt_cycle` already takes for the cumulative scope check
  against DSH's documented context list (instruction files at any depth, the root skill
  directories, a root `.env`; upstream dsh-v0.2.0-rc.2 source). `store.record_dsh_context` keeps
  one `DshContextRecord` per frozen attempt in `run_notes` (prefix `dsh_context: `, carrying its
  `list_source`; no storage version change), and only `status`/`report` read it back. `_accept`
  re-derives the receipt limitation from its own Git delivery diff. `_review` and the reviewer
  packet are unchanged. The reviewer's DSH still loads the files; nothing here is enforcement.
- **Every launch program is an absolute file outside the workspace.** `resolve_launch_config`
  never falls back to a bare name: `dsh`, and `node`/`python` for a `.js`/`.py` client, are looked
  up only on the absolute entries of the resolving environment's `PATH` (`find_on_path`, never the
  current directory), an explicit program (`HFLOW_ACPX_NODE`, a driver argument, an argv
  override) must be absolute, and a launcher, `dsh`, client interpreter or acpx client entry
  inside the project root or the worktree parent directory (`prepare.launch_workspaces`) makes the
  launch not resolvable, as does a workspace inside the `node_modules` tree the entry loads its
  modules from (`_client_module_tree`). With `HFLOW_ACPX_CLI` unset and no copy under
  `<data-dir>/m0/acpx`, the entry falls back to the HFlow checkout's own `.probe/acpx`, so a run
  whose project root is the HFlow repository itself reports the launch not resolvable until
  `HFLOW_ACPX_CLI` names an acpx installed outside it. The DSH batch shim is wrapped in the
  absolute `%SystemRoot%\System32\cmd.exe`. The child environment (also used by the zero-model
  preflight) always carries `NoDefaultCurrentDirectoryInExePath=1`, with every other spelling
  removed, drops relative and empty `PATH` entries (absolute entries are passed on as they are),
  and has `DSH_PERMISSION_MODE` / `DSH_TOOLS_MODE` removed, so neither cmd.exe (which runs the
  npm shim's bare `node`; the Desktop shim runs an absolute `DeepSeek Harness.exe`) nor Node's
  spawn looks in the workspace first.
- **Billing is unknown.** `provider_billed_tokens`, `provider_cost` and
  `subscription_quota_remaining` are `null` because nothing observes them. Do not read `null`
  as `0`. An agent-reported `usage` on the prompt response (UNSTABLE in ACP) or the `cost` of a
  `usage_update` is never read into them.
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
model selection, or remote billing termination. Batch E1/E2 and the 2026-10-02 refinement
(root ledger, bounded repair, launch hardening, `--model`) are offline-tested only. Details and
provenance: `docs/m2-live-acceptance-result.md`, `docs/m2-review-wire-repair.md`,
`docs/m0-results.md`, `docs/adr/0001-transport.md`.
