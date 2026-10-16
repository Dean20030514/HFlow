# HFlow — architecture (current state)

HFlow is a thin deterministic controller in front of a native coding Harness. The
Harness does the reasoning; the controller does admission, budget, evidence, and
delivery accounting. This document describes **what exists today**, not the target
architecture — read the plan for that (`docs/batch-e-plan.md` for the root budget and the
bounded repair).

It was first written at M1 and has been corrected since; where a section describes a
capability that arrived later (the production driver, Git worktrees, the review wire, the root
ledger, the bounded repair, integration), the module and its evidence are named so the claim can
be checked. A statement is only as strong as the artifact beside it: implemented code, an offline
test, or a recorded live execution on one binding — never a neighbour's success. The operator's
view of the same behaviour is `docs/operations.md`.

## How to read a claim

| Kind | Means | Where the backing is |
|---|---|---|
| implemented | the code path exists in this tree | the module named next to the claim |
| offline-tested | a test or an offline tool covers it, with no model call | `tests/`, `tools/m0_probe/real_client_checks.py` |
| live evidence, one binding | a recorded real execution on a specific machine, client and build | `docs/m2-live-acceptance-result.md`, `docs/m0-results.md` |
| unsupported / unverified | not implemented, or never exercised - never inferred from a neighbouring row | the README's "Not implemented", a driver capability record, a run's own notes |

A recorded live result is scoped to the artifacts named beside it. It does not become a property
of "the controller", and upgrading a dependency invalidates it until re-checked.

## Layer map

```text
CLI (cli.py: prepare/approve/run/status/report/runs/diff/cancel/resume/clean/ledger/integrate/
  |          init/schema; doctor.py: the read-only environment probe)
  |-- plan.py         saved plans, staleness, approve's terminal-only approval and its code
  |-- hints.py        the `next:` commands from stored facts; pasteable quoting and --data-dir
  |-- listing.py      `runs`, the `status` inbox, `diff`
  |-- scaffold.py     `init project/profile/task` (exclusive create)
  |-- integrate.py    controlled integration of an accepted candidate (prepare/apply/reconcile)
  |-- cleanup.py      guarded release of one run's managed worktree
  |-- report.py       status/report rendering from stored facts only
  |
prepare.py  the one resolution `prepare`, `approve` and `run` share: project contract, per-role
  |         bindings (profiles.py), resolved launch, write permission, base commit
  |
Controller (controller.py)  — finite state machine, no LLM calls
  |-- admission.py    TaskSpec + project contract -> admit or refuse (nothing is created first);
  |                   the dispatch preconditions, repair-policy rules included
  |-- authorization.py  one-shot, bound, user-attested approval artifact for a real run
  |-- ownership.py    the owner identity of a run: token, pid, creation time, host, OS file lock
  |-- packet.py       the role input packets, rendered from stored facts, 32 KiB bound
  |-- store.py        SQLite transactions, reserve_dispatch, compare-and-set, stop-conditional writes
  |-- migrate.py      table DDL and numbered storage versions (v8); the tables are runs / attempts /
  |                   evidence / run_notes / authorizations / root_budgets / invocations /
  |                   run_repair_records / invocation_settlements / integrations (+ schema_meta)
  |-- verify.py       approved checks over a frozen candidate -> evidence (with exit_reason)
  |-- artifacts.py    bounded output capture and the allowlisted check environment
  |-- workspace.py    scope containment, deny rules, manifests, candidate fingerprints
  |-- gitworkspace.py Git worktrees, base-commit resolution, candidate freezing, candidate refs,
  |                   the shared-Git-metadata snapshot
  |-- review.py       the one reviewer-answer parser, shared by the driver and replay
  |-- runtime.py      the controller build id recorded into every run
  |
HarnessDriver (contracts.HarnessDriver; a driver's optional cancel_handle is looked up, not declared)
  |-- drivers/selected.py   the single place a binding becomes a driver object
  |-- drivers/acpx_dsh.py   the production transport: acpx -> official DSH ACP (spawn, stream
  |                         reader, stop, reconcile)
  |-- drivers/launch.py     launch resolution, the agent child environment, the spawn-gate refusals
  |-- drivers/launch_content.py  launch entry files bound by content
  |-- drivers/primitives.py leaf helpers: PATH lookup without the cwd, env keys, bounded reads
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

Run creation writes its internal `RunAdmissionBinding` and effective configuration in that same
transaction. `RunAdmissionBinding`
fixes the admitted project/repository identity, workspace path, resolved base, project contract,
effective configuration and launch content, drivers, deadline, and root binding and limits.
An explicitly absent offline configuration is not a missing historical record. A never-dispatched
run may be continued or adopted only under that same binding; authorization ids may change, but
execution inputs may not. Missing, unreadable or different bindings return the existing run unchanged (exit
`5`), with instructions to restore the original configuration or cancel and submit a new revision.
`WorkspaceProvenance` fixes the project root, Git common directory and managed worktree path in
the transaction that attaches the worktree path. Neither record is reconstructed from mutable
notes or receipt paths, and neither
authenticates the database against another process running as the same user.

`cleanup.py` checks that provenance against the observed workspace and Git registration before
repository operations. A run that carries a `git_metadata: changed` or `unreadable` note is refused
`git_metadata_changed` - before any status read, which would run a filter that metadata names -
until the shared Git metadata digests to the run's recorded `snapshot before dispatch` again. Its
cleanup claim checks terminal run state and absence of active attempts inside the claim
transaction. Each removal attempt rechecks the guards and claimed workspace identity, including
index flags that can hide modified files. Removal succeeds only when both the directory and its
registration are gone; reconciliation uses the same repository source. An already recorded
success is an idempotent historical result, not a new removal or observation; a later directory
at that path is not deleted. A historical root ledger may identify the repository when
provenance is absent; missing paths without a reliable source stay unknown. Invalid stored context
or repair records raise `StoredRecordUnreadable`: `status`/`report` exit `6`, naming the record
instead of replacing it or exposing a traceback. Continuation and cleanup retain their
conservative refusal paths.

## State

Task state: `DRAFT → READY → RUNNING → CHECKING → ACCEPTED`, plus `BLOCKED` and
`CANCELLED`. `CHECKING` carries its stage in `phase` (`verification`, `review`,
`integration-check`). Attempt state: `CREATED`, `ACTIVE`, `SUCCEEDED`, `FAILED`,
`CANCELLED`, `OUTCOME_UNKNOWN`, `SUPERSEDED`. Delivery is tracked separately:
`NONE → LOCAL_CANDIDATE`.

A run itself never reaches `INTEGRATED` or `PUBLISHED`: a TaskSpec asking for one of those delivery
levels is still refused at admission, before any run row exists. Integration is a separate,
operator-approved step after acceptance (`hflow integrate`, `src/hflow/integrate.py`): its own
`integrations` record (storage v8) and its own `IntegrationReceipt` with
`delivery_state = INTEGRATED`, while the run's receipt stays `LOCAL_CANDIDATE` and is never
rewritten. Integration states: `preparing → checking → ready → applying → integrated`, terminal
`conflict` / `checks_failed` / `stale` / `interrupted` / `superseded` / `failed`. Its checks are
evidence of kind `integration-check` over the integration commit, run in a worktree of their own
and bracketed by a shared-Git-metadata snapshot (a change ends `checks_failed`, leaves that
worktree in place and notes the run). The ref update is Git's own compare-and-set: a locked ref
fails it with Git's message and the record returns to `ready`. `hflow integrate <run-id>` drives
the same operations from whatever state the run's integration is in, and an integration that
reaches `integrated` in the process releases the run's worktree through `cleanup.apply_cleanup`
(unless `--keep-worktree`), never changing the exit code.
`PUBLISHED` is not reachable in this build.

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
the report come first. `hflow ledger settle` moves an `unknown` or `launch_unknown` entry of an
ended run to `operator_settled` by attestation (an `invocation_settlements` row); it is not a
result, a process or a provider request.

## Reading the counters honestly

`hflow status <run-id>` prints several numbers that are easy to confuse. An excerpt of its real
output for an offline fake run with a root budget file (one implementer, one reviewer, one
`command` check, no model), paths and digests shortened:

```text
state         ACCEPTED
delivery      LOCAL_CANDIDATE
claimed_by    local-controller
owner         pid=1776 host=<host> created=2026-10-16T13:33:24.951149Z generation=1 token=862c07f9...
  liveness    not_probed (lock not_probed; not probed: the run is terminal): the run is terminal; its owner is not probed
turns         reserved 2/4, implementer self-reported 1 (a self-report, not a dispatched count and not a bill)
invocations   implementer=1 reviewer=1 (attempt rows; a deterministic dispatch count, not a model-request count)
root budget   root-0f383c4e...
  submissions used 2/3 (remaining 1)
  repairs     used 0/0 (the root's own counter: a repair dispatch is charged here; whether repair was armed for a run is in that run's repair decisions)
  deadline    2026-10-17T13:33:31Z (root limit 86400s, measured from the first reservation at 2026-10-16T13:33:31Z)
  approvals   AUTH-offline-60lwgk9kpy
dispatch ledger
  reserved=0 requested=0 started=0 not_started=0 settled=2 unknown=0 launch_unknown=0
  processes   0 operating-system child(ren) reported by a driver (ever started 0); 2 launch(es) ran without one, which is what the offline driver does
  provider model requests: unknown - no invocation row records one, and neither a reservation nor a process is counted as one
candidate     workspace still matches the accepted fingerprint
model_calls   0 (this command; provider-side requests are reported by the receipt and stay unknown when not observed)
```

- `claimed_by` is a human label. `owner` is the recorded controller process (storage v6; "not
  recorded" before). `liveness` (`matching`, `gone`, `unknown`, `not_recorded`, `not_probed`)
  says what its word rests on - observed by this command, or decided by rule with nothing probed -
  and none of it is stored.
- `reserved 2/4` is turns *paid for* out of the ceiling; a run that cannot afford the review blocks
  before the reviewer starts. `implementer self-reported` is the implementer's own count summed
  over rounds (the receipt's `controller_turns_observed`); it is not a dispatch count and returns no
  budget. The dispatch count is `invocations`.
- `root budget` reads the ledger row: `submissions` span every run and revision of the root,
  `repairs` is the root's own counter, `deadline` is fixed at the first reservation, `approvals`
  names every authorization charged (an offline run's CLI-minted `AUTH-offline-...` included).
- The **dispatch ledger** keeps the four facts of "State" apart. `processes` (and its alias `ever
  started`) counts invocations whose driver reported a pid and is never derived from a result
  state, so this offline run shows two settled launches and zero processes.
  `operator_settled=N (by attestation, not observed)` appears once `ledger settle` closed an entry.
- **Billing is unknown.** No invocation row records a provider request, so billed tokens, cost and
  quota stay `null` - never read as `0`. ACP `usage_update` is context-window usage and an
  agent-reported `usage`/`cost` is not a bill; neither fills those fields.
- A run with no root row prints `legacy / not recorded` for the root and the ledger: absent, not
  "0 used".
- Per invocation, `model`, `stream` and `launch` lines show the model observation, where the
  prompt response fell in the stream, and the launch-surface record; they read `not recorded` /
  `unknown` (never `0`) for the offline driver, older rows and streams not read to their end.
- `dsh context` lists, per frozen candidate, the files a DSH agent would load from the candidate
  worktree (`none on the list` or `CHANGED n:`), marked `declaration checked` when each was matched
  against `write_allow`. `repair` lists every repair decision, refusals included.
- `candidate` compares the workspace with the accepted fingerprint; `DRIFTED` means the stored
  `ACCEPTED` describes the candidate as it was, not the files on disk now.
- When they apply, `open entries`, `git metadata` lines (the shared-Git-metadata warnings),
  integration records and `next:` lines follow.

## Six properties the code enforces

1. **Admission refuses before anything is created.** `controller.run_task` validates the
   TaskSpec against the project contract as its first statement — before an authorization is
   registered, a run row inserted, a worktree attached or a turn reserved. A refusal is an
   `exit 2` with the issues listed, and leaves no run state at all; it is a different event
   from a run that dispatches and then blocks (`exit 3`), from an argument-parsing error
   (`exit 4`), from a run that exists but is not finished or cannot continue under its recorded
   admission binding (`exit 5`), and from an unreadable stored record (`exit 6`).
2. **Budget before dispatch.** `store.reserve_dispatch` is the one dispatch transaction, for
   both roles (`implementer` and `reviewer` are the only roles it accepts): in one
   `BEGIN IMMEDIATE` it checks ownership, phase, the absence of a stop, the root (unresolved
   invocations, deadline, room for the rest of the loop, repair count) and the authorization,
   then creates or attaches the attempt row, inserts the
   invocation row and increments the run, root and authorization counters - or rolls all of it
   back. Replaying the same invocation id returns the recorded intent and charges nothing.
   Database `CHECK` constraints (`turns_reserved <= turn_limit`,
   `used_top_level_submissions <= max_top_level_submissions`, `used_repairs <= max_repairs`)
   back up the logic. No reservation, no process. A run without a root takes the same
   transaction with no root counter and no invocation row.
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
   in its own transaction, and the run is left in the stop's recorded state with no receipt. It
   also requires the receipt to name this run, its current attempt and its task revision, and
   writes receipt and state in one guarded UPDATE. After a takeover (`store.take_over_run`,
   `owner_lost`) the superseded owner's launch registration
   (`register_attempt_invocation_unless_stopped`) and review attachment
   (`attach_review_result_unless_stopped`) require a live run without a receipt, so they write
   nothing.
6. **Unknown means stop.** An interrupted invocation becomes `OUTCOME_UNKNOWN` with its
   reservation intact, the run blocks, and `resume` only reconciles. It never
   re-dispatches (A04). The same block follows a completion that answers no observed prompt
   (`unbound_completion`, either role), a prompt answered with a JSON-RPC error
   (`prompt_error_response`) or with a stop reason outside ACP v1's set (`unknown_stop_reason`),
   a client whose boundary could not be confirmed empty (`boundary_not_empty`), and a controller
   interrupted (Ctrl+C, `SystemExit`) while a driver was starting or running an invocation.

Every store transaction (`Store.transaction`, and the migration's) runs its `COMMIT` inside the
guarded block: a commit that fails (`database is locked`, for one) is rolled back while it is still
open, so the connection releases the write lock and its own uncommitted rows. Opening a ledger
already at this build's storage version writes nothing; a new file is created and an older one
migrated ("Storage version and migrations").

## Storage version and migrations

The ledger records its own storage version, separate from the contract schema version. A file is
migrated to `migrate.STORAGE_VERSION` (8) the first time a build that understands it opens it; all
DDL lives in `migrate.py`, and `store.py` only decides when to migrate.

| Version | Adds (and what an older row gets) |
|---|---|
| v1 | `runs`, `attempts`, `evidence`, `run_notes`, `authorizations` |
| v2 | the root ledger `root_budgets` and the per-dispatch `invocations` |
| v3 | `invocations.launch_requested_at` (a request apart from a launch) and `authorizations.origin`; a v2 row past `reserved` becomes a launch request |
| v4 | the process facts a driver reports (`process_started_at`, `process_pid`, `spawn_kind`); older rows keep `spawn_kind = unknown`. Also repairs rows the first v3 step left with the request time in `started_at` |
| v5 | `evidence.exit_reason`, `run_repair_records`, `attempts.is_repair`; older rows keep an empty reason and `is_repair = 0` |
| v6 | the owner identity (`owner_token`, `owner_pid`, `owner_created`, `owner_host`, `claim_generation`); an older row keeps a NULL owner, read as "owner unknown" |
| v7 | `invocation_settlements`, the append-only record of `ledger settle` |
| v8 | `integrations`, with partial unique indexes (one `preparing`/`checking`/`applying` record per run, one `applying` per repository and target branch); a run accepted before v8 has none |

A file whose bootstrap never stamped a version is read by its shape, newest first. The v5 step
rebuilds `attempts` (v1's `UNIQUE (run_id, task_revision, role)` becomes `UNIQUE (run_id,
task_revision, role, is_repair)`, so a third implementer attempt cannot be inserted) by SQLite's
own procedure, with foreign keys suspended before the transaction and `PRAGMA foreign_key_check`
verified before it commits; an `attempts` table with unexpected columns is refused.

On the first open by a newer build:

1. The version is read before anything is written; a version newer than this build is refused
   with nothing touched.
2. A file at version > 0 is first copied with SQLite's backup API to `<db>.pre-v<version>.bak`,
   through a `.partial` copy that is checked (`integrity_check` and its version) before the rename.
   An existing `.bak` that is empty, damaged or of another version refuses the open, ledger
   unchanged: move it aside and open again. A `.partial` another process is still writing (its open
   handle blocks the cleanup) refuses with "open it again once that process has finished".
3. The migration runs in one `BEGIN IMMEDIATE` transaction; any failure, a failed `COMMIT`
   included, rolls it back whole. Re-opening a migrated file does nothing, and the snapshot is
   never overwritten.

Nothing is back-filled: an old evidence row keeps an empty `exit_reason`, which is exactly what
keeps it ineligible to trigger a repair. There is no downgrade: do not open a migrated ledger with
an older build. The only rollback is to stop everything, keep the migrated file, and copy the
snapshot over `hflow.sqlite` (for example `Copy-Item hflow.sqlite.pre-v7.bak hflow.sqlite`) -
losing everything written after the snapshot.

## Bounded repair

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
  `budget_exhausted`, `deadline_reached`, `stop_requested`, `reopen_refused`,
  `no_content_change`, `workspace_drift`);
- the repair round reuses the first round's worktree only while it is exactly the previous
  candidate (HEAD, tree, a clean status and no index entry flagged assume-unchanged or
  skip-worktree), and only while the shared Git metadata still matches the pre-dispatch
  snapshot, refusing otherwise as `workspace_drift` before anything is bought
  (`_reconcile_repair_workspace`). Ignored files present then are the previous round's check or
  review byproducts, or allowlisted caches the worker itself left (`__pycache__`, `.pytest_cache`):
  the first freeze refused any ignored path off its allowlist and, as every freeze does, any
  ignored file the scoped fingerprint hashes and any sourceless `.pyc` outside `__pycache__`. Those
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
none is promised as future work here; the README's "Not implemented" section lists what a
reader must not assume. The one production Driver
exists (`drivers/acpx_dsh.py`) and is bound through `drivers/selected.py`.

## Known boundaries (do not overstate)

- **DSH's own launch inputs are mostly recorded, not controlled.** DSH reads a workspace `.env`, its
  home's `.env`, `cordis.patch.yml` layers, AGENTS.md/CLAUDE.md files and skills on its own
  (documented upstream at dsh-v0.2.0-rc.2, not observed). `drivers/dsh_surfaces.py` records,
  just before each spawn, what is at those fixed paths - presence, size and a SHA-256, a `.env`
  by presence and size only - plus the DSH_* variable names reaching the child and the client and
  carrier versions read from files. Two of these inputs are refused on a real driver, at
  admission and again at the driver's spawn gate: a root `.env` in the workspace DSH starts in
  (`workspace_env_file`; never parsed or opened) and a bound `DSH_HOME` inside or around the
  workspace (`dsh_home_in_workspace`). A launch with no credential source visible - no
  `DEEPSEEK_API_KEY` name in the child's environment and neither `.credentials.yaml` nor `.env` in
  its DSH home (stat only) - is refused the same way (`no_credential_source`, user ruling
  2026-10-07); a presence check, not a validity check. The rest of the record is not enforced or
  bound into an approval, and DSH reads the files after the look; a `$DSH_HOME/.env` outside the
  workspace is not refused. With `DSH_HOME` unbound the child's DSH home is the
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
  it): `core.hooksPath` set to a path below an existing system file (no worker can create a hook
  there), `maintenance.auto=false`, `gc.auto=0`, `core.fsmonitor=false`,
  `commit.gpgsign=false`, `core.ignoreStat=false` and `core.sparseCheckout=false` (so HFlow's own
  `worktree add` never checks an entry out flagged assume-unchanged or skip-worktree) and
  `safe.bareRepository=explicit` (a directory git would only implicitly take for a bare
  repository, such as a worktree root a worker turned into one, is refused rather than read),
  plus `GIT_CONFIG_NOSYSTEM=1`, `GIT_NO_REPLACE_OBJECTS=1`, `GIT_NO_LAZY_FETCH=1`, a
  `GIT_GRAFT_FILE` below the same system file, which can never exist
  (no grafts, inherited or written into `.git/info/grafts`) and a fixed identity. Inherited
  repository-locating variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_IMPLICIT_WORK_TREE`,
  `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`,
  `GIT_NAMESPACE`, `GIT_PREFIX`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`) are dropped, because every
  call names its repository by its working directory, and so are variables that redirect what git
  reads there (`GIT_CONFIG`, `GIT_SHALLOW_FILE`, `GIT_REPLACE_REF_BASE`, `GIT_ATTR_SOURCE`,
  `GIT_EXTERNAL_DIFF`, `GIT_DIFF_OPTS`, the `GIT_*_PATHSPECS` modes); `GIT_CEILING_DIRECTORIES`
  only narrows discovery and is kept. The freeze commit also passes
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
  Manifests and scoped fingerprints hash file contents in 64 KiB chunks and count the bytes
  actually read. This bounds file-content working memory without changing sorted path entries,
  content digests, candidate identity or missing/unreadable semantics; it does not bound the
  number of paths in a snapshot.
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
  to its end without a wire-state truncation records where the prompt response fell and how many updates for the prompt's session
  followed it (`stream_order`), from one reading of the reader's drained flag that the verdict
  decision uses too. The reviewer's verdict is reassembled from the `agent_message_chunk` updates
  the driver observed for the session the first `session/prompt` request named - a chunk for any
  other session, or with no `sessionId` (counted in a limitation,
  `agent_message_chunk_other_session=N`), a chunk observed before that request, or a request that
  names no session, leaves no verdict - and decoded into one canonical `ReviewOutput`
  (`src/hflow/review.py`).
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
- **Raw output and parsed state have separate bounds.** Each stdout read reaches the bounded
  sink before decoding: `total_bytes` counts the complete read stream, the file retains its raw
  prefix, and `StreamCapture.digest` hashes only the retained bytes. Blank lines, invalid UTF-8
  and a final line without a newline are preserved. A 1 MiB line parser discards an overlong
  line through its next newline rather than parsing its tail as a new record. The protocol byte
  share and 20,000 nonempty wire records bound all accumulated state, including raw-line windows,
  prompt/terminal/error records, model state and transcripts; eventless messages count too.
  After either bound, draining and byte accounting continue but metadata cannot authorize an
  outcome: `OUTCOME_UNKNOWN` / `output_limit_exceeded`, no review, model observation or
  `stream_order`, no refund and no re-dispatch. A passed model flag reads `UNKNOWN`, an absent
  flag `NOT_PASSED`; observed dispatch yields one turn, otherwise an incomplete stream yields
  `agent_turns=None`. `prompt_response_line` remains a zero-based nonempty-line ordinal; raw
  blank lines mean it need not equal the physical file line number. Historical ordinals and
  replay indices are unchanged.
- **A failed protocol read is incomplete evidence.** The stdout file exists before its reader
  starts; an open, seek or read error propagates to the reader guard rather than masquerading
  as EOF. Even a prefix containing a completed prompt response cannot authorize success or a
  review verdict. Final model observations and stream ordering are absent on `reader_failed`.
  Model/reasoning updates and model-set responses are scoped to the first valid created session
  and checked against the first prompt; mismatches or ambiguous request ids cannot upgrade a
  requested model to `passed` or `accepted`.
- **Check artifact errors settle as verification errors.** Factory/directory failures produce
  `ERROR/no_artifact_dir` before launch; manifest errors produce `ERROR/output_capture_error`
  after execution, retaining actual exit and stream facts and naming the existing directory.
  These errors block verification and cannot buy a repair or leave the run in `CHECKING`.
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
  (`_gate`, a lock) is held while it publishes an invocation's handle **and** creates the
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
- **A candidate's harness context files are refused unless declared, and recorded when declared.**
  `workspace.dsh_context_paths`
  classifies the `--no-renames` list `_attempt_cycle` already takes for the cumulative scope check
  against DSH's documented context list (instruction files at any depth, the root skill
  directories, a root `.env`; upstream dsh-v0.2.0-rc.2 source). Right after that scope check,
  `workspace.dsh_root_env_paths` and `workspace.undeclared_dsh_context_paths` (the rule is
  `dsh_context_declared`: a `write_allow` entry equal to the path, or a skill entry at or under a
  skill directory that contains it) block the run `RefusalCode.CONTEXT_FILE_CHANGE` before the
  candidate ref (user ruling, 2026-10-03). `store.record_dsh_context` keeps
  one `DshContextRecord` per frozen attempt in `run_notes` (prefix `dsh_context: `, carrying its
  `list_source`; no storage version change), and only `status`/`report` read it back. `_accept`
  re-derives the receipt limitation from its own Git delivery diff. The cycle passes the declared
  list to `_review`, which reads bounded diffs (`_context_file_changes` through
  `GitRepo.diff_text_bounded`, 8 KiB in all) into the reviewer packet's untrusted-data section,
  and to `_repairable_failure`, which carries it in `RepairContext.context_files` for the repair
  packet. The reviewer's DSH still loads the files; nothing here is enforcement.
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
  absolute `%SystemRoot%\System32\cmd.exe`. The child environment (`launch.child_environment`,
  also used by the zero-model preflight) always carries `NoDefaultCurrentDirectoryInExePath=1`,
  with every other spelling removed, drops relative and empty `PATH` entries (absolute entries are
  passed on as they are), and has `DSH_PERMISSION_MODE` / `DSH_TOOLS_MODE` removed, so neither
  cmd.exe (which runs the npm shim's bare `node`; the Desktop shim runs an absolute `DeepSeek
  Harness.exe`) nor Node's spawn looks in the workspace first. The `node` the npm shim runs (the
  `node.exe` beside the shim, else the first on the child's `PATH`) is bound by content as
  `carrier_interpreter` and looked up again in the child's environment at the spawn gate. Every
  variable `artifacts.is_secret_like` matches is withheld from the agent child except
  `DEEPSEEK_API_KEY` (user ruling 2026-10-16), and the launch record notes the withheld names -
  a denylist, not a credential sandbox.
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
model selection, or remote billing termination. The root ledger, the bounded repair, the launch
hardening, `--model`, the plan flow and integration are offline-tested only. Details and
provenance: `docs/m2-live-acceptance-result.md`, `docs/m2-review-wire-repair.md`,
`docs/m0-results.md`, `docs/adr/0001-transport.md`.
