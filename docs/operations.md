# Operations

Practical notes for running HFlow as it exists today. The commands below are the ones the
installed CLI actually accepts; when this file and `hflow <command> --help` disagree, `--help`
is right. `python -m pytest -q` and `python -m pytest -q --collect-only` are the two ways this
file's test numbers were checked.

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
| `not_implemented` | the requested driver does not exist in this build, or the TaskSpec asked for a delivery level this build cannot reach | use `--driver fake`, request `local_candidate`, or wait for the implementation |

Refusals that happen *before* a run exists (exit `2`) are listed in `issues`, not in a block
code. Two of them are easy to meet by accident:

| Admission issue | Meaning | Next action |
|---|---|---|
| `reuse_not_approved` | `choice` is `reuse`/`adapt` while `fit_test_status` is `pending` or `failed`, or `not_required` was claimed without a reason | answer the compatibility question (or record the choice as `build`/`defer`) and submit again |
| `not_implemented` at `delivery.mode` | the task asked for `integrated`/`published` | this build delivers `local_candidate` only; a lower level than requested is not delivered silently - change the request deliberately |

These are **refusals**, not blocks: no run row, no workspace, no reservation, no invocation and
no allowance exists afterwards, and `status` has nothing to show you. A block (`exit 3`) is the
opposite kind of event - the run exists, work happened, and its evidence is kept. Reading a
refusal as "the run failed" and a block as "nothing happened" are both wrong.

## Guarantees you can rely on

- `status`, `report` and `doctor` make zero model calls. They are pure SQLite reads plus
  static local probes (the optional `--project-root` drift check reads project files).
- Submitting the identical TaskSpec again returns the existing run; it does not buy a
  second worker turn. To genuinely re-run, change the task (a new revision produces a
  new run).
- A refused admission (`exit 2`) creates no run state at all.
- `cancel` on a run that never dispatched is local and instant; on a dispatched run it
  asks the driver and records `confirmed_stopped` / `still_running` / `unknown`
  honestly. What can be stopped is what the process boundary owns: a Windows Job Object, which
  covers the tree it was given. On other platforms the boundary degrades to
  `direct_child_only` and says so (`ProcessBoundary.kind`), so there is no descendant control
  to claim there, and nothing here reaches a remote model request or a remote bill.

## M2 runs through the CLI

An offline candidate needs three files: the target repository (a real Git repo), a project
contract, and a task. The change itself comes from a plan file, because the fake driver is a
scripted stand-in:

```sh
hflow run --task task.json --project .hflow/project.json --project-root <repo> \
          --driver fake --fake-write-plan plan.json --json
hflow status <run-id> --project-root <repo>
hflow report <run-id> --json --project-root <repo>
```

The TaskSpec selects isolation with `"workspace": {"mode": "worktree", "base_commit": "<sha>"}`.
`--base-commit` / `--workspace` override it *before* admission, so the stored spec and its
digest describe what actually ran.

What the receipt gives you, and how to read it:

| Field | Meaning |
|---|---|
| `candidate.base_commit` | the fixed commit the worktree started from |
| `candidate.git_commit` / `git_tree` | real Git objects for the frozen candidate |
| `candidate.fingerprint` | content hash over the write scope; **not** a Git id |
| `candidate.worktree` | where the candidate lived (until you clean it) |
| `candidate_paths` | the paths that are part of the candidate |
| `verification.status` / `evidence_ids` | the approved check's result on that candidate |

`--driver acpx-dsh` needs `--authorization-file <auth.json>` plus `--authorization-mode`, and
the artifact must cover exactly this run. It refuses before reading credentials, creating a
workspace or reserving budget, and it never falls back to the fake driver. `--live-authorized`
is not a flag in this build and never was one that worked: a bare flag could be typed by the
same process that runs the task, which is the thing the artifact exists to prevent.

## Releasing a workspace (`clean`)

`clean` deletes the run's **working directory**. It never deletes the delivery.

```sh
hflow clean <run-id>              # preview only; changes nothing, not even git metadata
hflow clean <run-id> --apply      # remove this run's worktree
hflow clean <run-id> --reconcile  # after an interruption, decide from recorded facts
```

The preview prints the resolved path, the Git common directory, the registration, HEAD, the
candidate ref and its target, the tracked/ignored/unsupported status, and every reason for
the decision. It creates no ref and removes no file. `--dry-run` is the same preview;
combining it with `--apply` is refused.

`--apply` re-checks everything (an earlier preview is not a standing permission), claims the
run's cleanup intent in a short transaction, and calls `git worktree remove` **without
`--force`**. It refuses when:

| Refusal | Why |
|---|---|
| `no_managed_workspace` | the run did not use a worktree |
| `not_a_worktree`, `not_registered` | the path is not the linked worktree git has for this run |
| `is_source_repository` | the path is the repository's main worktree |
| `execution_active`, `run_in_flight` | an attempt or the run is still live |
| `stop_unconfirmed` | a cancellation was requested but never confirmed |
| `unfrozen_changes`, `head_drift` | the worktree holds changes that were never frozen |
| `unknown_ignored_files` | ignored files that are not known build artifacts (an `.env`, local data) |
| `unsupported_status` | unmerged or submodule records that cannot be interpreted |

A refusal keeps everything and releases the cleanup claim, so the same command works once
you fix what blocked it. What survives a successful `clean`: the candidate commit (reachable
through `refs/hflow/candidates/<run-id>/<attempt-id>`), its tree, the receipt, the evidence,
and the run's history. Repeat `--apply` is idempotent; a path that vanished without a cleanup
record is reported `MISSING`, never as a success.

## Known limits of `clean`

- It protects against HFlow's own concurrent operations, not against another process running
  as the same user that deliberately holds files open.
- Windows can keep a directory undeletable for a moment after a check's child exits. HFlow
  retries once after a short settle; if git still refuses, it reports the failure and does
  not force anything.
- Worktrees are not garbage-collected automatically, and `clean` deliberately never runs
  `git gc`, `git clean`, `worktree prune`, or `rmtree`.

## Running a real Harness task (authorized only)

A real driver needs an **authorization artifact**: a JSON file holding the user's own approval
text, bound to one execution, with a hard cap on how many top-level submissions it covers.

```json
{
  "schema_version": 1,
  "authorization_id": "AUTH-m2-live-1",
  "provided_by": "user",
  "user_text": "<the user's approval, verbatim>",
  "authorized_at": "<when the user said it>",
  "max_top_level_submissions": 2,
  "binding": {
    "mode": "m2-live-change",
    "driver": "acpx-dsh",
    "project_id": "m2-live-reportkit",
    "repo_path": "<abs path>",
    "base_commit": "<40-hex>",
    "spec_digest": "sha256:<the task as admitted>",
    "spec_path": "<abs path to task.json>"
  }
}
```

```sh
hflow run --task task.json --project .hflow/project.json --project-root <repo> \
          --driver acpx-dsh --authorization-file auth.json \
          --authorization-mode m2-live-change --data-dir <data> --json
```

Why an artifact instead of a flag: a flag would be written by the same process that runs the
task, so an agent could authorize itself. The artifact is refused unless

- `provided_by` is exactly `user` (a model-written note is rejected by the schema);
- the binding matches the run about to happen - mode, driver, project, repository path, base
  commit, task digest and task path. Any mismatch lists the offending fields;
- allowance remains. Consumption is a single SQL UPDATE guarded by a CHECK constraint, so a
  restart, a new run id or a resubmitted identical spec cannot restore it.

`mode` keeps activities apart: a `stop-trial` approval does not cover a `m2-live-change` task.

Before any dispatch, a **zero-model preflight** runs: the driver launches the installed client
with a metadata argument (`--version`), so a broken launch binding is found without spending a
submission. Preflight failure refuses the run and consumes nothing. A duplicate submission
(which correctly dispatches none) also consumes nothing.

`implementer` and `reviewer` are separate invocations and separate top-level submissions; both
are claimed from the same artifact.

### Validate the client config offline (do this before a live run)

```sh
python tools/m0_probe/real_client_checks.py all
```

Four model-free checks: the installed client reports its version through the production
launcher; the client **accepts the config the driver writes** in both permission modes
(`config show` must parse it); and a full one-shot `exec` round trip runs against the project's
mock agent. The config check exists because an invented key makes the client exit during
startup, which is indistinguishable from "the agent did nothing" - it cost one live submission
once, and never will again: `defaultPermissions` takes `approve-all` / `approve-reads` /
`deny-all`, and `nonInteractivePermissions` accepts only `deny` or `fail`.

Writes are off unless `HFLOW_ALLOW_WRITES=1`, which the controller honors only for a run whose
workspace is a disposable worktree created from a fixed base commit; the effective mode is
recorded in the run's notes.

## Preparing a real M2 task

```sh
python tools/m2_live/prepare_m2_live.py --out .probe/m2-live
```

Builds a synthetic Git project with two genuine input-handling defects, the project contract,
the TaskSpec, and the authorization template (`user_text` deliberately empty). It asserts the
base commit really fails both defect tests and that nothing fails for import or collection
reasons - so a later live run is never the first time the package is exercised. It contains no
fixed patch and no fake-write plan.

### What the authorization does and does not prove

**Trust model: trusted-local, user-attested operation.** A human creates the artifact through the
manual procedure below; the artifact records that decision and bounds its consumption. The
executor is trusted not to forge approvals and not to modify the controller or its database.

What is actually enforced:

- the **binding** must match the run about to happen (mode, driver, project, repository path,
  base commit, task digest, task path), so an approval for one task cannot authorize another;
- **consumption is bounded per authorization id** by a single guarded UPDATE plus a CHECK
  constraint, so a restart, a new run id or a resubmitted identical spec cannot restore
  allowance for that id;
- `provided_by` must be the literal value `user` - a value constraint that rejects an artifact
  *labelling itself* agent-authored.

What is **not** enforced, and must not be claimed:

- provenance is **not authenticated**. Nothing distinguishes bytes a user typed from the same
  bytes written by the executing agent, because there is no approval issuer and no protected
  store outside the executor's reach. `tests/test_authorization.py` asserts this limit
  explicitly, so it is a recorded fact rather than an unstated assumption;
- a **fresh authorization id resets the allowance** - the cap bounds one id, not a person or a
  day. Raising this to real anti-forgery would need a separate issuer or store and is a
  deliberate design decision, not something to solve with another string field.

### Effective permissions per role

Before a live run, the controller records the effective modes in the run's notes, and they are
enforced per role:

| Role | `defaultPermissions` | Rationale |
|---|---|---|
| implementer (write-capable, disposable worktree, `HFLOW_ALLOW_WRITES=1`) | `approve-all` | the task must change a file; **all** tool permission requests are auto-approved, which is broader than file writes |
| implementer (default) | `approve-reads` | reads proceed, writes are refused |
| reviewer | `approve-reads` | never inherits the implementer's write approval |

`HFLOW_ALLOW_WRITES` is a local request, not authority: it is honored only for a run whose
workspace is a disposable worktree created from a fixed base, it is disclosed in the notes, and
it never applies to the review invocation. acpx permission mediation is also **not** a sandbox:
its filesystem checks do not confine arbitrary shell commands, and a disposable worktree does
not confine anything by itself.

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
the boundary. That teardown has been recorded as passing once, for one machine, one acpx/DSH
version and one binding (M2 trial A, with the client in `approve-reads` mode); it does not
extend to another platform, to a process that leaves the boundary, or to remote billing.
Unattended production execution therefore stays disabled.

An accepted cancellation cannot be overwritten by a late success: acceptance refuses while a
cancellation intent is recorded, in the same transaction that would have written the receipt.

## What is not safe yet

- **No sandbox.** A worker and `command` checks run as ordinary child processes with your
  user's rights. Scope is detected after the fact and the candidate is refused, but the write
  already happened. Nothing confines credentials either: an approved check inherits the
  process environment, so keep project checks free of anything that needs a secret.
- **The managed worktree is a workspace boundary, not a permission boundary.** With
  `"workspace": {"mode": "worktree", "base_commit": "<sha>"}` a run works in a detached
  worktree beside the repository, your HEAD/index/working files/stash/branches are left alone,
  and the frozen candidate is kept under `refs/hflow/candidates/...`. It still shares the
  source repository's Git objects and admin files, and a process running as this user can read
  and write outside it. With `mode: in_place` a run edits the project root directly.
- **The reviewer is not sandboxed.** It is a separate process, session and invocation and it
  never inherits the implementer's write permission, but it reviews the same checkout with
  `isolation=prompt_only` recorded. A prompt-level instruction is not an enforced boundary.
- **A stop is local.** Closing the managed process boundary ends the processes it owns; it is
  not a cooperative protocol cancellation (unsupported on the selected one-shot `exec` path),
  it does not follow a descendant that leaves that boundary, it exists on Windows only, and it
  says nothing about a remote model request or remote billing having stopped.
- **No repair cycle.** Failed verification or a rejected review stops the run.
- **No billing observation.** Cost and token fields are `null`; do not read `null` as 0.
- **Authorization is trusted-local.** The artifact records a human decision and bounds its
  consumption, but its provenance is not authenticated and a fresh authorization id resets the
  allowance; the executor is trusted not to forge approvals or edit the ledger. Until that
  changes, unattended execution stays disabled.

## Recovering from a controller crash

The durable facts are the `runs` and `attempts` rows. If the controller died between
"driver started" and "result stored", the attempt row still exists with its reservation
(`reserved_expires_at`, `process_identity`). `hflow resume <run_id>` calls the driver's
`reconcile` and records what it observed; the run stays `BLOCKED` with
`outcome_unknown`. Nothing re-dispatches automatically, because a lease expiry alone
does not prove the worker stopped.
