# Operations

Practical notes for running HFlow as it exists today (M1, offline).

## Daily commands

```sh
hflow doctor --json                        # what is on this machine; no model calls
hflow run --task task.json --project-root <repo> --driver fake --json
hflow status <run_id>
hflow report <run_id>
hflow resume <run_id>                      # only reconciles; never re-dispatches
hflow cancel <run_id>
```

`run` prints a JSON payload that includes the receipt. Use `--receipt-out <path>` to
write only the receipt, and check the exit code: `0` accepted, `2` refused at admission,
`3` blocked after dispatch.

## Where the data lives

| Data | Path |
|---|---|
| Runtime database | `%LOCALAPPDATA%\HFlow\hflow.sqlite` (Windows), `$XDG_DATA_HOME/hflow/` otherwise |
| Override | `--data-dir <dir>` or `HFLOW_DATA_DIR` |
| Build id recorded into every run | `HFLOW_BUILD_ID`, else `hflow/<version>` plus the short git SHA |

`run`/`status`/`report` accept `--data-dir` before or after the subcommand. Runtime data
is never written inside a project checkout, so a run cannot dirty the tree it measures.

## Reading a blocked run

`hflow status <run_id>` prints the block code and the attempts. The codes you will
actually meet today:

| Code | Meaning | Next action |
|---|---|---|
| `budget_exhausted` | the turn ceiling could not cover the next dispatch | split the task, or raise `limits` in `project.json` deliberately |
| `verification_failed` | an approved check failed; the worker's exit code is irrelevant | read the evidence detail, fix the code or the check, submit a new revision |
| `scope_violation` | files changed that the TaskSpec did not authorize | inspect the reported paths; nothing was accepted |
| `evidence_stale` | the candidate changed after verification | re-run; do not reuse the old evidence |
| `review_rejected` | the reviewer returned `changes_requested` | read the finding, then submit a new revision |
| `outcome_unknown` | the invocation was interrupted and its result is unknown | `hflow resume` to reconcile; submit a new revision to proceed |
| `driver_failed` | the driver reported a failure before/without a result | read `block_reason`; fix the environment, do not blindly retry |
| `not_implemented` | the requested driver does not exist in this build | use `--driver fake`, or wait for M0/M2 |

## Guarantees you can rely on

- `status`, `report` and `doctor` make zero model calls. They are pure SQLite reads plus
  static local probes (the optional `--project-root` drift check reads project files).
- Submitting the identical TaskSpec again returns the existing run; it does not buy a
  second worker turn. To genuinely re-run, change the task (a new revision produces a
  new run).
- A refused admission (`exit 2`) creates no run state at all.
- `cancel` on a run that never dispatched is local and instant; on a dispatched run it
  asks the driver and records `confirmed_stopped` / `still_running` / `unknown`
  honestly. Cross-process child-tree termination is **not** implemented.

## Reading the counters honestly

`status` prints three things that are easy to confuse:

```text
turns         reserved 2/4, implementer self-reported 1 (a self-report, not a dispatched count and not a bill)
invocations   implementer=1 reviewer=1 (deterministic dispatch count; billed model requests: unknown)
candidate     workspace still matches the accepted fingerprint
```

- `reserved 2/4` means two turns were *paid for* out of a ceiling of four: one for the
  implementer process and one for the reviewer process. Both were dispatched; a run that
  cannot afford the review turn blocks *before* the reviewer starts.
- `implementer self-reported 1` is what the worker claimed for its own turn. It is not the
  run total and it cannot return reserved budget. The controller's own **dispatch** count is
  the `invocations` line.
- Whether a Harness makes internal model requests per turn is not observable here, so billed
  usage stays `null` and "billed model requests" stays `unknown`. Do not read `null` as `0`.
  ACP `usage_update.used/size` is context-window usage, not a bill.
- `candidate` compares the workspace against the fingerprint the run was accepted at. If it
  says `DRIFTED`, the stored `ACCEPTED` describes the candidate as it was, not the files on
  disk now; nothing has been re-verified.

## Stopping a run

`hflow cancel <run_id>` records the intent **first**, then asks the driver, then reports what
actually happened:

| Receipt | Meaning |
|---|---|
| `confirmed_stopped` + `mechanism=forced` | the managed process boundary was terminated; *local execution stopped* |
| `confirmed_stopped` + `mechanism=none` | there was nothing left to stop (already exited, or never dispatched) |
| `still_running` / `unknown` | the stop could not be confirmed: the run stays blocked, the workspace and evidence are kept, and nothing is re-dispatched |

A confirmed stop is **not** a rollback, **not** a successful protocol cancellation, and
**not** a known business result. Cooperative cancellation (`session/cancel`) is unsupported
on the one-shot `exec` launch path, so the only mechanism available is forced teardown of
the boundary. Until that is verified against the real Harness, unattended production
execution stays disabled.

An accepted cancellation cannot be overwritten by a late success: acceptance refuses while a
cancellation intent is recorded, in the same transaction that would have written the receipt.

## What is not safe yet

- **No sandbox.** A worker (once a real driver exists) and `command` checks run as
  ordinary child processes with your user's rights. Scope is detected after the fact and
  the candidate is refused, but the write already happened.
- **No isolated workspace.** M1 edits the project root directly. Keep a clean checkout
  or a copy until M2 adds managed worktrees.
- **No repair cycle.** Failed verification or a rejected review stops the run.
- **No billing observation.** Cost and token fields are `null`; do not read `null` as 0.

## Recovering from a controller crash

The durable facts are the `runs` and `attempts` rows. If the controller died between
"driver started" and "result stored", the attempt row still exists with its reservation
(`reserved_expires_at`, `process_identity`). `hflow resume <run_id>` calls the driver's
`reconcile` and records what it observed; the run stays `BLOCKED` with
`outcome_unknown`. Nothing re-dispatches automatically, because a lease expiry alone
does not prove the worker stopped.
