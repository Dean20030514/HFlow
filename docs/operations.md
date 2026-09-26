# Operations

Practical notes for running HFlow as it exists today. The commands below are the ones the
installed CLI actually accepts; when this file and `hflow <command> --help` disagree, `--help`
is right. `python -m pytest -q` and `python -m pytest -q --collect-only` are the two ways this
file's test numbers were checked.

## Daily commands

```sh
hflow doctor --json                        # what is on this machine; no model calls
hflow doctor --profile <id>                # resolve the profile per role; non-zero if unusable
hflow prepare --task task.json --profile <id> --project-root <repo>
hflow run --task task.json --project-root <repo> --driver fake --json
hflow run --task task.json --profile <id> --authorization-file auth.json --json
hflow status <run_id>
hflow report <run_id>
hflow resume <run_id>                      # only reconciles; never re-dispatches
hflow cancel <run_id>
```

`run` prints a JSON payload that includes the receipt. Use `--receipt-out <path>` to
write only the receipt, and check the exit code: `0` accepted, `2` refused at admission,
`3` blocked after dispatch.

## Configure once, then reuse it

A machine profile is the machine half of a run: which agent, transport and permission each
**role** uses. It lives beside the runtime data, never inside a project checkout:

```text
%LOCALAPPDATA%\HFlow\profiles\<id>.json          (or <data-dir>/profiles/<id>.json)
```

The shape is generated, not hand-written twice - `hflow schema` prints it under
`MachineProfile`. A two-role profile looks like this:

```json
{
  "schema_version": 1,
  "profile_id": "dsh-local",
  "role_bindings": { "implementer": "dsh-worker", "reviewer": "dsh-reviewer" },
  "agents": {
    "dsh-worker":   { "harness": "dsh", "driver": "acpx-dsh", "model_selection": "native_profile" },
    "dsh-reviewer": { "harness": "dsh", "driver": "acpx-dsh", "model_selection": "native_profile" }
  },
  "limits": { "max_parallel_workers": 1, "max_native_children": 0 },
  "security_mode": "trusted_local"
}
```

`model_selection` is a recorded label in this build, not a launcher flag: the DSH launcher
still starts with the profile its driver was built with, and `hflow doctor --profile dsh-local`
prints that exact argv so nothing about it has to be assumed.

**Both roles must be bound.** An unbound role is refused instead of inheriting the other one,
because a task revision can start requiring a review. Every one of these is a refusal, never a
fallback to a default:

| Input | What happens |
|---|---|
| `--profile ghost` (no such file) | refused, naming the path it looked in |
| a file whose `profile_id` disagrees with its name | refused |
| an unknown field, or a role bound to an undeclared agent | refused, naming the field |
| a role the profile does not bind | refused, naming the role |
| `driver: "some-other-harness"` | refused: one production transport plus the offline fake |
| `harness: "codex"` with `driver: "acpx-dsh"` | refused: that driver launches `dsh`, and every process the run started would be DSH while the record said Codex |
| one role `fake`, the other `acpx-dsh` | refused: half a run scripted, half a model |
| `--driver fake` together with a real profile | refused, naming the roles that disagree |

Precedence, defined once in `profiles.requested_profile_id`: `--profile` beats `HFLOW_PROFILE`;
with neither, `--driver` decides and defaults to `fake` (offline).

## Before you run anything: `hflow prepare`

`prepare` resolves the task, the project contract and the machine bindings through the *same*
function `run` uses, and prints what would happen. It calls no model, creates no run row, no
workspace, no SQLite database and no authorization:

```sh
hflow prepare --task task.json --profile dsh-local --project-root <repo>
hflow prepare --task task.json --profile dsh-local --project-root <repo> --json
hflow prepare --task task.json --profile dsh-local --root-budget-file root-budget.json
```

It reports:

| Section | What it answers |
|---|---|
| `effective_config` | which profile, which agent/driver per role, the **resolved launch**, which write permission, and a `digest` |
| `admission` | every problem with the task's definition that `run` would refuse on |
| `dispatch_preconditions` | every problem with running it *now* that `run`'s dispatch gate would refuse on |
| `write_allow` / `write_deny` | the scope the worker will be told |
| `checks` | the approved checks this task's acceptance needs, and which criterion needs each |
| `budget` | implementer + reviewer turns required, against the task budget and project ceiling |
| `root_budget` | with `--root-budget-file`: the root binding, its ceilings, the normal dispatch count, the repair switch and the deadline. `null` without the flag (a legacy-shaped run) |
| `packet_preview` | the implementer's input packet, byte length and digest |
| `authorization` | the binding an approval would have to cover, and nothing that is one |

Two things it deliberately does not do. It does not render the **reviewer's** packet - that
packet embeds the frozen candidate identity, the verification status and the evidence rows, none
of which exists before the implementer runs, so a preview now would be an invented input. And it
does not mint an approval: `creates_authorization` is pinned to `false`, no `user_text` and no
`provided_by` appear anywhere in its output, and feeding its output to `load_authorization`
fails. Approving is a user action.

A third: it does not promise **remaining allowance**. With `--root-budget-file` it prints the
root binding and the ledger path it would use, but it opens no database at all - no SQLite file
is created, no run row, no authorization - so the number that matters (does this root still have
allowance, has its deadline passed) is decided by the run's own dispatch transaction. A preview
that printed a remaining figure would be reading a file another process may already have spent.

For a worktree run the workspace path contains the run id, which is chosen at dispatch, so
`execution_root_is_final` is `false` and the packet size/digest are for the template path.

Exit code: `0` when the run would get past admission **and** its dispatch gate, `2` otherwise.
The full preview prints either way - that is the point of asking. What `prepare` cannot check is
stated rather than implied: remaining authorization allowance depends on a run's history, so it
is checked only once a run exists, and it says so in its notes.

### What stops a run before it dispatches, and what `prepare` therefore reports

| Condition | Where it is decided |
|---|---|
| the task declares write paths but `workspace.mode` is not `worktree` | shared precondition |
| the task declares write paths but `HFLOW_ALLOW_WRITES` is not enabled | shared precondition |
| a required review, but `budget.max_agent_turns` is 1 | shared precondition |
| a role's launch could not be resolved (no acpx client on this machine) | shared precondition |
| a real delivery whose approved checks are `kind=fake` | admission |
| unknown check, scope violation, risk below the project floor, unmet delivery level, reuse not decided, budget above the project ceiling | admission |
| not enough authorization allowance left for the whole fixed loop | controller, once a run exists |

The first four are the ones `prepare` reports under `dispatch_preconditions`, and they are the
same list the run's own gate raises - one function, two callers.

## What an approval now covers

The authorization artifact binds the *effective configuration*, not just the task: profile id,
per-role agents and drivers, model selections, limits, the write permission, and the **resolved
launch** - the client entry point, the interpreter that starts it, the launcher argv, and the DSH
home/profile. `prepare` prints exactly that binding, and `run` verifies against the same
resolution, so a preview and a run cannot disagree.

Consequences worth knowing:

- Approving a task on one profile does not approve it on another; switching profile after
  approval changes the digest and the run is refused, naming both sides.
- Changing where the client or the interpreter lives changes it too: `HFLOW_ACPX_NODE`,
  `HFLOW_ACPX_CLI`, `DSH_HOME`, or a different `dsh`/`node`/`python` earlier on `PATH` all change
  the approval digest, because all of them change which program would run.
- The launch is resolved **once**, before the approval, and then consumed by the driver. Nothing
  re-reads the environment after the check, so a variable changed mid-run cannot swap the client.
- `DSH_HOME` is part of the launch in both directions: a bound value is set on the child, and
  *absence is bound too* - when the resolution found no DSH home, the variable is removed from
  the child environment rather than inherited, including any value passed in through the
  driver's `extra_env`. A `DSH_HOME` that appears after the resolution therefore cannot reach
  the process.
- An artifact written before config binding existed still loads, still lists, and still keys its
  own single-use ledger row - but it cannot authorize a run that resolved a configuration,
  because nothing in it says which one. Re-issue it.
- The digest of an old artifact is unchanged by this build: the new fields are omitted while
  they are empty, so an already-consumed approval cannot look unused again.
- The launch is bound by *paths and argv*, not by program content: replacing a file at the same
  path does not change the approval.

## Root budgets: `--root-budget-file` (batch E1)

A **root** is one requirement in one repository, across revisions. One root budget file, passed
as `--root-budget-file` to `prepare` and `run`, declares what that requirement may spend in
total. It is a JSON document with exactly two members:

```json
{
  "limits": {
    "max_top_level_submissions": 4,
    "max_repairs": 1,
    "deadline_seconds": 86400
  },
  "note": "E2 repair ceiling for the parser task; approved 2026-01-01"
}
```

| Field | Meaning | Range |
|---|---|---|
| `max_top_level_submissions` | how many top-level dispatches (implementer + reviewer invocations) this root may buy in total | 1..64 |
| `max_repairs` | how many *additional* implementer attempts the root may buy. The first implementer attempt is not a repair. **Only recorded and enforced in E1** - no repair loop spends it yet | 0..8, default 0 |
| `deadline_seconds` | wall-clock ceiling measured from the root's **first successful reservation**, recorded as `deadline_at`; a later dispatch past it is refused | >= 60, default 86400 |
| `note` | free text the user keeps in the file. Never an approval: the approval is the authorization artifact's `user_text` | - |

Unknown members, a missing `limits`, or a value outside its range is refused rather than
defaulted - a ceiling nobody chose is not a ceiling.

What the root file does, and what it does not:

- The root identity is **derived mechanically** from `(project_id, canonical repo path, task_id)`
  (`root-<32 hex>`). No worker and no flag can choose one; a new `task_id` is a new root and needs
  its own approval.
- The **ledger path is part of the authorization binding**. `run` refuses an artifact whose
  `root_budget.ledger_path` is not the ledger this `--data-dir` resolves, so changing
  `--data-dir` gives a refusal naming the fields, not a second unused allowance. That guard is
  against ordinary path mistakes: copying the database, deleting it, or editing the artifact all
  stay inside the trusted-local boundary and are not defended against.
- A root charge is always recorded **with the authorization that bought it**. For a real driver
  that means `--root-budget-file` requires `--authorization-file`: the artifact must bind the same
  root, and its `root_limits` must equal the file's `limits`, or the run is refused before anything
  is dispatched. The offline fake driver reaches no model and has no approval to give, so an
  offline root run **without** `--authorization-file` mints a labelled in-memory record instead
  (`authorization_id` starts with `AUTH-offline`), whose `user_text` states that it is not a user
  approval and whose binding names `driver: fake`, so it can never authorize a real transport.
  Pass `--authorization-file` if you want the offline run charged to your own artifact: it is then
  loaded and verified exactly like a real one. No path charges a root with an empty authorization
  id.
- `prepare` prints the root binding, its ceilings, the normal dispatch count (implementer +
  reviewer), the repair switch (**OFF in E1**) and the deadline, and prints the same root inside
  the pending authorization binding, so the digest a user approves is the digest the run checks.
  It creates nothing.
- One transaction reserves a dispatch: ownership, role/phase, root ownership and allowance,
  authorization allowance, the attempt row, the invocation row and every counter commit together
  or not at all. The deadline and the counters are read and written inside it, so two controllers
  cannot both spend the last submission.
- **One root runs one task at a time.** A dispatch is refused while another run of the same root
  is not `ACCEPTED`/`BLOCKED`/`CANCELLED`; the refusal names the owning run. Unresolved
  invocations are not the whole rule - a run is busy while its approved checks run and while a
  verdict is rendered, and in both windows nothing is pending.
- **The whole remaining loop has to fit.** The reservation is checked against this dispatch plus
  every further dispatch the run needs, so a revision whose reviewer cannot be afforded is refused
  before the implementer is bought.
- **One review per candidate.** A non-implementer dispatch attaches to the attempt the implementer
  created and is refused if that attempt already recorded a reviewer invocation - whether or not
  the first review is still unresolved. A new invocation id is not a second review and not a
  second allowance.
- A pending invocation of a root - `reserved`, `requested`, `started`, `unknown` or
  `launch_unknown` - blocks every later dispatch of that root, **including under a new revision**.
  An unresolved invocation is never refunded, retried or re-dispatched; an operator reconciles it.
  The ledger separates four facts: `requested` (a driver was asked), `started` (the launch
  happened), the process count (a driver reported a pid) and the settlement. `not_started` is
  recorded only when no driver was ever asked; a launch that was requested and never reported back
  is `launch_unknown`, because an empty timestamp is not evidence that nothing ran - a forced stop
  of a real child is evidence of the opposite.
- `status`/`report` read the ledger back: root id, used/limit submissions, repairs used/limit,
  deadline, and per-invocation role, launch state and spawn kind. The `processes` line counts
  reported operating-system children, so an offline run shows zero however many times it settled.
  A run recorded before E1 has no root row and says `legacy / not recorded` - absent facts, never
  zeros.
- Automatic repair is **not implemented**. E1 is the ledger and the one dispatch transaction; the
  E2 loop that would spend `max_repairs` does not exist, and a budget field is not a feature.

## Where the data lives

| Data | Path |
|---|---|
| Runtime database | `%LOCALAPPDATA%\HFlow\hflow.sqlite` (Windows), `$XDG_DATA_HOME/hflow/` otherwise |
| Machine profiles | `<data-dir>/profiles/<id>.json` |
| Override | `--data-dir <dir>` or `HFLOW_DATA_DIR` |
| Profile selection | `--profile <id>` or `HFLOW_PROFILE` |
| Build id recorded into every run | `HFLOW_BUILD_ID`, else `hflow/<version>` plus the short git SHA |

`run`/`status`/`report` accept `--data-dir` before or after the subcommand. Runtime data
is never written inside a project checkout, so a run cannot dirty the tree it measures.

## Storage version and migrations (batch E1)

The database records its own **storage version**, separate from the public contract version.
Batch E1 adds `root_budgets` and `invocations` (plus a few columns), so the file goes from
version 1 to version 2 the first time a build that understands v2 opens it.

What happens on that first open:

1. The version is read **before** anything is written. A file that records a version newer than
   this build understands is refused with nothing touched - no snapshot, no write.
2. If the file is at version > 0, it is copied first with the SQLite backup API to
   `<db>.pre-v<version>.bak` (for example `hflow.sqlite.pre-v1.bak`), next to the database. A
   brand-new file gets no backup: there is nothing to protect.
3. The migration runs inside one `BEGIN IMMEDIATE` transaction of plain statements. Any failure
   rolls the whole thing back, and re-opening an already-migrated file does nothing (no second
   backup, no rewrite). The backup is never overwritten, so the earliest pre-migration state
   stays restorable.

**Do not open a migrated database with an older binary.** A build that predates the version
check has no idea what `root_budgets`/`invocations` mean and would read (and write) a layout it
does not understand. There is no downgrade path and no partial-version support.

To roll back, stop everything, keep the migrated file, and restore the snapshot:

```sh
# Windows PowerShell, with data-dir pointing at the directory that holds the database
Copy-Item "$data\hflow.sqlite" "$data\hflow.sqlite.migrated"
Copy-Item "$data\hflow.sqlite.pre-v1.bak" "$data\hflow.sqlite"
```

```sh
# POSIX
cp "$data/hflow.sqlite" "$data/hflow.sqlite.migrated"
cp "$data/hflow.sqlite.pre-v1.bak" "$data/hflow.sqlite"
```

The restored file is the pre-migration state: runs, authorizations and evidence as they were at
that moment, and **without** anything the newer build wrote afterwards (a root ledger row, an
invocation reservation, a delivery decision recorded later). Restoring is a deliberate data loss
of everything after the snapshot; it is the only supported rollback, and it is why the snapshot is
never overwritten.

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

For a run that spends against a **root ledger** (`--root-budget-file`, batch E1) the artifact
carries two more members: the derived `binding.root_budget` and the approved `root_limits`. Write
them from `hflow prepare --root-budget-file <file> --json`, which prints exactly that binding:

```json
{
  "binding": {
    "...": "the same fields as above, plus:",
    "root_budget": {
      "root_id": "root-<32 hex>",
      "project_id": "<project>",
      "repo_path": "<abs path>",
      "task_id": "<task id>",
      "ledger_path": "<abs path to hflow.sqlite>"
    }
  },
  "root_limits": {
    "max_top_level_submissions": 4,
    "max_repairs": 1,
    "deadline_seconds": 86400
  }
}
```

`root_limits` is **required** whenever the binding carries a root: an approval that named a root
but not its ceiling would otherwise get whatever default the build happens to use. Both
directions are checked - a root run whose artifact carries no root is refused, and a root artifact
used for a run that resolves no root is refused. `root_limits` must equal the `limits` in the
`--root-budget-file`, or the run is refused before anything is dispatched.

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

### What each role is actually told

Neither invocation receives a bare one-line goal any more. The controller renders one
**input packet** per role (`src/hflow/packet.py`) from recorded facts, and the driver transports
that text verbatim through the client's stdin path - it never re-renders, extends or explores the
repository to fill a gap.

| Role | The packet carries |
|---|---|
| implementer | goal, acceptance criteria with their check ids, allowed/forbidden write paths, the workspace, the formal check ids, external-side-effect limits, the deadline and the effective file permission |
| reviewer | the same task facts, the frozen candidate identity (base commit, candidate commit/tree, content fingerprint, diff reference, worktree), the recorded program evidence (verification status, per-check exit codes, evidence rows), the review rules, the canonical `ReviewOutput` contract, and the read-only constraint |

Three facts about this wiring:

- each packet is bounded (32 KiB by default). A packet that does not fit is refused **before**
  dispatch; a required field is never silently truncated. A full diff or a full log travels by
  reference, not inline;
- the digest of the exact text is recorded in the run notes (`role_input_packet`) and reported
  back by the driver (`prompt_digest`). A driver that hands the transport different text than the
  controller rendered blocks the run rather than having its result attributed to this task.
  That digest is a *local* record of what was sent - not a receipt from the ACP server or the
  model, which nothing in this build can observe. "The prompt arrived" is evidenced in tests by
  the receiving agent's own record of what it read;
- the implementer is never told the candidate identity or the check results (it produces them),
  and the reviewer is never handed the implementer's own summary of its work.

### Three kinds of transport evidence, kept apart

| Evidence | What it proves | Where |
|---|---|---|
| offline fake driver | the controller's state machine, budget, evidence and receipt rules | `tests/test_controller.py` and most of the suite |
| production driver + Python stand-in for acpx | the driver's launch, framing, event projection, stop and reconcile logic | `tests/test_packet_wire.py` (most cases), `tests/test_driver_acpx_dsh.py` |
| **installed pinned acpx + input-sensitive ACP stub** | the real Node client carries the rendered packet, and the agent checks the values it received | `tests/test_packet_wire.py`, the two `real_acpx` cases |
| real DSH with a model | nothing in this repository claims it: a live task needs its own explicit approval | `docs/m2-live-acceptance-result.md` (historical) |

A fake client result is never presented as a real-client proof, and a real-client result is never
presented as a live-model proof. The pinned client is never downloaded or upgraded by the tests: a
missing copy skips with the reason.

### Refusals that now happen before the first invocation

For a real driver (`--driver acpx-dsh`), admission refuses a run it already knows cannot finish,
instead of spending an implementation turn and failing afterwards:

| Refused up front | Why |
|---|---|
| a required check declared `kind=fake` | a fake check executes nothing: a receipt resting on it would claim a verification that never ran |
| a task with write paths but `workspace.mode` not `worktree` | an in-place run would write into your own checkout |
| a task with write paths but `HFLOW_ALLOW_WRITES` off | the invocation would be launched read-only and could not make the change |
| a task that needs a review but reserves fewer than 2 turns | the review is a separate invocation; one turn cannot reach an accepted run |
| a task whose fixed loop needs more submissions than the authorization has left | the predictable half of budget exhaustion: without this check the implementer would run and the run would then block with the implementation already paid for |
| a repository that cannot be discovered, or a base commit that does not exist | both are knowable without side effects, and the worktree would otherwise fail after the run existed |
| an implementer packet that does not fit the 32 KiB bound | the bound is checked against the packet this run would actually send, with its real worktree path - not against a placeholder |

`--driver fake` is the offline driver: it keeps its fake checks and needs none of the above. A
refusal through any of these gates creates no run, consumes no authorization allowance and starts
no process. The per-dispatch ledger is still the authoritative limit; these checks remove the
predictable failures in front of it, and they are not a substitute for its atomic claim.

A request for a TaskSpec that already has a run is a **history query**, not a new dispatch: it
returns the recorded outcome even when the authorization has since been used up, and it never
consumes allowance. Only a run that still needs turns is checked against the remaining allowance,
and only for the turns it still needs.

### Check output: what is kept, and what that word means

A command check's stdout and stderr are drained continuously through pipes into a bounded sink. The
default retention is **8 MiB per stream** (the run's whole invocation budget, including the
driver's own logs, is 32 MiB); both are operating values, not measured optima.

What the evidence row and the artifact say, precisely:

- `stdout`/`stderr` carry `total_bytes` (everything read), `retained_bytes` (what is on disk),
  a `sha256` digest **of the retained bytes**, and a `truncated` flag. The digest covers what a
  reader can actually obtain, so it can be recomputed; the discarded tail is a recorded count, not
  a silent loss;
- the files live under `<data-dir>/artifacts/<evidence-id>/<check-id>/` - inside the run's own
  record, never in the workspace under test - together with an `artifact.json` manifest naming the
  argv, sizes, digests, exit code and reason, elapsed time, and the environment summary;
- **truncated output cannot authorize acceptance on its own**: a stream cut at the retention limit
  is recorded as truncated in the evidence row. Debug output that is cut is not by itself a
  business failure, but a verdict or a control event read from a cut stream is never treated as
  complete;
- a check whose output pipes are still held open after it exits (a descendant inherited them) is
  reported as `output_capture_error` with `ERROR`, not as a clean pass. The wait for the readers is
  bounded, and what was read is still digested;
- `elapsed` covers launch, the check itself, the settle window and the reaping, so it is not a
  claim that the check's own timeout bounds the whole call.

### Worker log retention (what is bounded, and what is only measured)

Two different things live here, and the difference is stated rather than glossed:

**Bounded (HFlow's own retention).** Everything HFlow keeps is inside one declared budget per
invocation (32 MiB by default):

- the retained protocol log (`events.ndjson`) stops at the protocol share of that budget
  (`max_raw_log_bytes` minus the stderr share), and the in-memory event list stops growing with it
  (`events_capped`). Parsing continues, because that is how a stop reason is recognised, but
  nothing further is accumulated;
- the client's **stderr** is drained from a pipe this driver owns and retained up to the stderr
  share. What exceeds it is read, counted and digested but not written. (It used to be a file the
  child wrote into and a reader that had nothing to read: the record said "0 bytes, not truncated"
  while the child wrote mebibytes. It is a pipe now, so the bound is real and the record is true.);
- a single line that never ends is bounded at 1 MiB: it is counted as an unusable line, which also
  makes the turn unknown, instead of growing in memory as fast as the client writes;
- spending the budget is recorded **on the read that spends it**, not on the next line, so a stream
  whose final line crosses the budget is reported as `output_limit_exceeded` with an
  `OUTCOME_UNKNOWN` outcome. A cut protocol stream is never trusted.

**Measured, not bounded (the client's own protocol file).** The client writes `stdout.ndjson`
itself. HFlow does **not** truncate a file another process is writing - that removes bytes nobody
has read and can leave a hole - so it does not claim to cap that file. Its size is reported as the
measured number it is: `peak_raw_bytes` in the invocation record, and the retained copy's
`total_bytes` equals what was actually read. A client that writes 17 MiB therefore leaves a 17 MiB
file, and HFlow's record says exactly that instead of reporting the full stream as retained.

Is a cut stderr file a failure? No - stderr is diagnostics, and it is reported as truncated. A cut
*protocol* stream is: a verdict read from a stream that was cut is not a verdict.

### What a check's environment contains

The child environment is built from an **allowlist** of what a process needs to start and find its
runtime (PATH/PATHEXT, SystemRoot/TEMP, the usual Windows and POSIX shell and locale variables, the
Python/Node runtime variables), plus any variables the caller declares as approved test variables.
Unlisted names do not travel, and a name that looks like a credential (`*_API_KEY`, `*_TOKEN`,
`*_SECRET`, `*_PASSWORD`, `*_CREDENTIAL`, `*_AUTH*`, ...) is refused even when declared. The
refusal is reported as a fact in the evidence row's `withheld_secret_like=` list.

The environment summary records **names and counts only** - never a value, and never a hash of a
value, since a hash of a low-entropy secret is still a disclosure. This is not confinement: a check
still runs with the current user's rights and can read whatever that user can read.

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

`status` prints several things that are easy to confuse:

```text
turns         reserved 2/4, implementer self-reported 1 (a self-report, not a dispatched count and not a bill)
invocations   implementer=1 reviewer=1 (attempt rows; a deterministic dispatch count, not a model-request count)
root budget   root-1f0c... (a root run; a run with no root ledger row says "legacy / not recorded")
  submissions used 2/3 (remaining 1)
  repairs     used 0/1 (recorded counter; the repair loop that would spend it is not implemented in E1)
dispatch ledger
  reserved=0 started=1 not_started=0 settled=1 unknown=0 (total 2, ever started 2)
candidate     workspace still matches the accepted fingerprint
```

- `reserved 2/4` means two turns were *paid for* out of a ceiling of four: one for the
  implementer process and one for the reviewer process. Both were dispatched; a run that
  cannot afford the review turn blocks *before* the reviewer starts.
- `implementer self-reported 1` is what the worker claimed for its own turn. It is not the
  run total and it cannot return reserved budget. The controller's own **dispatch** count is
  the `invocations` line.
- The **dispatch ledger** keeps three different facts apart, and they are not one number:
  - `reserved` - the dispatch transaction committed. The allowance is spent and the intent is
    durable, but no process has been recorded as started. A reservation is neither a process
    nor a model request.
  - `started` - a process was handed to the driver (`ever started` counts the ones that also
    settled afterwards). It is still not a provider model request.
  - `not_started` - reserved, but provably never launched (a stop won the handoff). The
    allowance is kept, never refunded.
  - `settled` - a recorded result was applied; `unknown` - started and never settled, which
    blocks the root and is never re-dispatched.
  - **provider model requests: unknown.** No invocation row records one, and a reservation is
    never counted as one.
- Whether a Harness makes internal model requests per turn is not observable here, so billed
  usage stays `null` and "billed model requests" stays `unknown`. Do not read `null` as `0`.
  ACP `usage_update.used/size` is context-window usage, not a bill.
- A run with no root ledger row (any run recorded before E1, or one that never used a root
  budget file) prints `legacy / not recorded` for both the root budget and the dispatch ledger.
  That is **not** "0 used": there is no root, no invocation row and no counter to read, and
  nothing is back-filled from the new rules.
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

The stop goes to the **role that is running**, not always to the implementer: while the review
phase is live it names the reviewer's own invocation and asks the reviewer's driver (a profile
may bind the two roles to different drivers). Which role, driver and invocation were asked, and
what the stop reported, is recorded as a `cancel_target` line in the run's own note table
(`run_notes`, alongside the effective configuration and the role input packets); the run's notes
are not part of the `status`/`report` projection, so read them from the store.

A recorded stop is coordinated through the **write** and through the **spawn gate**, not
through a sequence of checks. Registering a role's invocation and every block of a run carry
"no cancellation intent" as a condition in the same statement, and the driver publishes an
invocation's handle and creates its child inside one gate that *both* stop entry points
(`cancel_handle` and `cancel(invocation_id)`) take to record their request. So a stop that
arrives while the reviewer packet is being built, while the review turn is being reserved, or
while the checks are still running cannot be followed by a reviewer process - and a stop that
arrives as the process is about to be created wins outright: no child is created, the invocation
is reported as cancelled, and nothing runs. A stop that arrives while the child is being created
waits for that critical section to end - normally by waiting to acquire the gate, which the spawn
releases only after publishing the handle - and then terminates the process it finds, reported as
`mechanism=forced`. "No handle for this invocation" is never an answer while a spawn is in
flight. That wait is bounded by the spawn, not by the invocation: the gate is released before the
model answer is awaited, so a stop may wait for a process to be created but never for the call it
is stopping to finish. A review turn already reserved for a stopped run stays spent - nothing is
refunded - but no process is started for it.

Once an intent is recorded the run's stop state is final: acceptance refuses, and a later
failure (a transport error, a `review_protocol_error`, a rejected review) is recorded against
the attempt without relabelling the run - an unconfirmed stop stays `outcome_unknown`, a
confirmed one stays `cancelled_by_operator`, and a result arriving after the attempt was
finalized is recorded as a `late_result` note instead of being applied.

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
