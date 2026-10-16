# Operations

The operator guide for HFlow as this tree builds it. When this file and `hflow <command> --help`
disagree, `--help` is right. Internals are in `docs/architecture.md`; what is absent from this
build is listed once, in the README's "Not implemented" section.

## Daily commands

```sh
hflow doctor                                    # what this machine can run; no model calls
hflow init project                              # .hflow/project.json with kind=command checks
hflow init profile dsh-local                    # <data-dir>/profiles/dsh-local.json
hflow init task T-1 --goal "..." --allow src    # ./T-1.task.json, and the prepare command for it
hflow approve --task T-1.task.json --profile dsh-local --allow-writes   # writes T-1.plan.json, asks you
hflow run --plan T-1.plan.json                  # runs the plan with the approval approve wrote
hflow status                                    # the inbox: runs that need you, each with a next command
hflow integrate <run-id>                        # at a terminal: put the accepted candidate on its branch
```

`approve --task` is `prepare --plan-out` and `approve --plan` in one process. The two-step form
lets you read the preview before anything asks for an approval:

```sh
hflow prepare --task T-1.task.json --profile dsh-local --allow-writes --plan-out T-1.plan.json
hflow approve --plan T-1.plan.json          # add --run to continue straight into run --plan
hflow run --plan T-1.plan.json
```

Everything else is reading and recovery: `status <run-id>`, `report <run-id>` (receipt and
evidence), `runs` (recent runs, one next command each), `diff <run-id>`, `cancel`, `resume`,
`ledger settle` and `clean`. `report` and `diff` take `--last` for the newest run instead of an id;
`status` does not (`status --last` exits `4` naming `report --last`), and `runs --needs-you` is
gone too (exit `4`, naming `hflow status`).

The offline fake driver (the default without a profile) runs the same loop with no model and no
approval: `hflow run --task task.json --project-root <repo> --driver fake --fake-write-plan
writes.json`, where the write plan is a JSON object of `{relative path: file text}`.

## Exit codes, output and `next:` lines

| Code | Meaning |
|---|---|
| `0` | done: accepted, or the command did what it was asked |
| `2` | refused: nothing was created (an admission refusal, a stale plan, a wrong code, a Git error in `integrate`) |
| `3` | blocked: the run exists, work happened and its evidence is kept; for `integrate`, a decision is yours (hand-off, conflict, stale, failed checks) |
| `4` | usage: an argument error, a removed flag, an unknown run/integration/invocation id, a bad `--attest` |
| `5` | in progress: the run is `DRAFT`/`READY`/`RUNNING`/`CHECKING`, another owner may be alive, or another process holds the ledger locked (`database is locked`: run it again) |
| `6` | a stored record no longer validates, or the ledger cannot be used by this build (a newer storage version, a file that is not a ledger, a write the ledger refused) |
| `130` | Ctrl+C: prints `interrupted: ...`, retries nothing; what was recorded before is authoritative (`hflow status <run-id>`) |

- A refusal (`2`) and a block (`3`) are different events. A refusal leaves no run row, workspace,
  reservation or allowance behind; a block is a run that dispatched and stopped.
- A command that looks something up (`status`, `report`, `diff`, `cancel`, `resume`, `clean`,
  `integrate`, `ledger settle`) never creates a ledger. An id it cannot find exits `4` with one
  line naming the ledger file it looked in and suggesting `--data-dir`.
- `resume` exits `5` whenever it refused on the owner rule and wrote nothing, or a store write
  failed (rolled back whole; run it again) - never a `0` or `3` that would read as done.
- `--data-dir` is accepted before or after the verb. `--controller-id`, `--receipt-out`,
  `--authorization-mode`, `--base-commit` and `--workspace` do not exist (exit `4`): the task file
  carries its workspace mode and base, and `run --json` carries the receipt.
- Without `--json`, a refusal or a `run`/`resume` outcome prints `key: value` lines; `--json`
  prints one JSON document (`approve --run --json` prints two lines: the approval, then the run).

**`next:` lines.** `run`, `status`, `report`, `cancel`, `resume`, `ledger settle` and every
`integrate` command end their text output with `next: <command>` lines (`prepare --plan-out` and
`approve` end with the `approve` / `run --plan` command), worked out from stored facts only
(`hflow.hints`: no model, no process probe, no Git command; it reads only ref and worktree `HEAD`
files of the run's repository). The order is: `resume` after an interruption, `ledger settle` for
each unresolved entry, the integration of an accepted Git candidate, then `clean` (preview) and
`clean --apply`. A trailing `# ...` says when the command depends on something only it can
observe. No hint re-runs a task, reuses an authorization or proposes a new revision.

- An `--attest "<what you know and why>"` placeholder must be replaced by your own words. A
  `<...>` placeholder or those words pasted as printed are refused with exit `4`, before anything
  is opened, by `resume`, `ledger settle`, `integrate reconcile` and `integrate <run-id>`.
- Every printed command carries `--data-dir <absolute path>` when the ledger is not the default
  one, and a path is quoted for PowerShell on Windows (7 or 5.1) and for `sh` elsewhere. Do not
  paste into cmd.exe: it expands `%VAR%` inside double quotes and passes single quotes on. A drive
  or share root is printed as `C:\.` so no backslash precedes a closing quote.
- An accepted Git candidate's integration step is always `hflow integrate <run-id>` (it prepares,
  asks for the confirmation code, applies or hands the merge over, and reconciles). It names
  `--project` only when the command's default would not resolve the run's contract, and
  `--target <branch>` only when neither an earlier integration's branch nor the task's base
  branch (still present in the run's repository) gives one. When a ready integration's target is
  checked out (read from `HEAD` files, any letter case) the comment says where the merge will be
  handed over.

## Setting up

### `hflow doctor`

Exit `0`, or `2` when a named profile (`--profile`, else `HFLOW_PROFILE`) cannot be used. It finds
programs on the absolute `PATH` entries only, runs `git --version` and `node --version` and
nothing else - never `dsh` (the one on `PATH` may be the DSH Desktop app's shim) and never the acpx
client - and opens the ledger read-only. Its readiness lines are facts and never change the exit
code:

| Line | What it says |
|---|---|
| `ledger health` | storage version and `PRAGMA quick_check(1)` over a read-only connection: `ok`, `problem` (the first finding; nothing repaired), `incomplete` (a lock or the 10-second bound stopped it - not a finding), `unreadable`, or no ledger |
| `ledger settle` | how many entries were settled by operator attestation, `consumed` and `void` |
| `integrate` | `hflow integrate` usable, not usable or unknown, from the PATH Git's version (2.40 needed) |
| `acpx client` | the acpx entry a launch would use and its `package.json` version, else `NOT FOUND` with the order: `HFLOW_ACPX_CLI`, `<data-dir>/m0/acpx/node_modules/acpx/dist/cli.js`, the HFlow checkout's `.probe/acpx`. `PATH` is never consulted for acpx and a profile cannot name it |
| `dsh launcher` | where a launch finds `dsh` (path only): `PATH` (`dsh=PATH`), else the npm CLI at `<data-dir>/dsh/0.2.0-rc.2/node_modules/.bin/dsh.cmd` (`dsh=npm (data dir)`). With neither it prints the install, `npm install --prefix <data-dir>\dsh\0.2.0-rc.2 --ignore-scripts @deepseek-ai/dsh@0.2.0-rc.2`. A Desktop shim under `%LOCALAPPDATA%\Programs\DeepSeek Harness` is named but not recommended (DSH #9626, unconfirmed: its workspace-write sandbox fails every shell call) |
| `credentials` | whether `DEEPSEEK_API_KEY` is in the environment (name only) and, with a bound `DSH_HOME`, whether `.credentials.yaml` / `.env` exist there (stat only), then whether a real launch from this shell would be refused `no_credential_source`. A presence check, not a validity check |
| `writes` | how a task with `scope.write_allow` gets its write opt-in, and whether `HFLOW_ALLOW_WRITES` is enabled in this shell |

With `--profile` it prints each role's resolved launch (agent argv, `--model` flag or "no --model
flag", the launch entry files with their SHA-256) and names `child_dsh_home`, the DSH home an
HFlow child uses - not your own shell's unless `DSH_HOME` is set.

### Machine profile

A machine profile binds each role to an agent and a driver. It lives at
`<data-dir>/profiles/<id>.json`, never in a checkout; `hflow init profile <id>` writes the usual
one and `hflow schema` prints its shape (`MachineProfile`):

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

Precedence: `--profile` beats `HFLOW_PROFILE`; with neither, `--driver` decides and defaults to
`fake`. Every one of these is refused, never defaulted:

| Input | Refusal |
|---|---|
| no such profile file; `profile_id` disagreeing with the file name | refused, naming the path |
| an unknown field, a role bound to an undeclared agent, a role left unbound | refused, naming it (both roles must be bound) |
| a driver other than `acpx-dsh` or `fake`; `harness` other than `dsh` with `acpx-dsh` | refused |
| one role `fake` and the other real; `--driver fake` with a real profile | refused, naming the roles |
| an invalid `model_selection` (see "Model selection") | `invalid_spec` at profile load |

Every hand-written JSON input (task, contract, profile, plan, authorization, root budget, repair
policy, fake write plan) is read as UTF-8, a byte-order mark accepted. A file that cannot be read
that way - UTF-16 from Windows PowerShell 5.1's `>` included - is a one-line `invalid_spec`
refusal naming the file (exit `2`).

### Where the data lives

| Data | Path |
|---|---|
| Ledger | `<data-dir>/hflow.sqlite`; the data dir is `--data-dir`, else `HFLOW_DATA_DIR`, else `%LOCALAPPDATA%\HFlow` on Windows, `$XDG_DATA_HOME/hflow` (default `~/.local/share/hflow`) elsewhere |
| Profiles | `<data-dir>/profiles/<id>.json` |
| Check artifacts, invocation scratch | `<data-dir>/artifacts/...`, `<data-dir>/invocations/...` |
| Worktrees | `<repo>.hflow-worktrees/<run-id>` beside the repository |
| Build id recorded into each run | `HFLOW_BUILD_ID`, else `hflow/<version>`, plus `+<sha>` only when run from HFlow's own source checkout |

Nothing runtime is written inside a project checkout. Storage versions and migration backups:
`docs/architecture.md`, "Storage version and migrations".

## Before you run anything: `hflow prepare`

### What it reports

`prepare` resolves the task, the project contract and the machine bindings through the same
function `run` uses. It calls no model and creates nothing: no run row, workspace, database or
authorization. Exit `0` when the run would pass admission and its dispatch gate, `2` otherwise; the
full preview prints either way.

| Section | What it answers |
|---|---|
| `effective_config` | profile, agent and driver per role, the resolved launch (client entry, interpreter, agent argv, `--model` when set, DSH home), the write permission, and its digest |
| `admission` | every problem with the task that `run` would refuse on |
| `dispatch_preconditions` | every problem with running it now (next section); look here as well as at `admission` |
| `write_allow` / `write_deny` | the scope the worker is told (the project's and the built-in deny lists are enforced but not listed) |
| `checks` | the approved checks acceptance needs |
| `budget` | turns required against the task budget and project ceiling; with a repair policy, the worst case |
| `root_budget`, `repair_plan` | the root binding and ceilings with `--root-budget-file`; whether a repair is armed |
| `packet_preview` | the implementer's packet, its size and digest (for a worktree run, rendered for the template path; the reviewer's packet does not exist before a candidate does) |
| `launch_surfaces` | what DSH would read on its own (see "Launch-surface record") |
| `authorization` | the binding an approval must cover, with `base_commit` as the resolved SHA |

It never mints an approval (`creates_authorization` is `false`) and never states a remaining
allowance: that depends on the ledger, which only the run's dispatch transaction reads.

### The plan flow

| Command | What it does | Refused (nothing written) |
|---|---|---|
| `prepare --plan-out P` | writes a plan (`kind: "hflow-plan"`): the inputs with every path absolute (the data dir too), the profile it selected, the write opt-in, the full report and the binding digest. Atomic; an existing plan at `P` is replaced. `--json` keeps stdout for the report and puts the plan lines on stderr | exit `2` when `P` exists and is not a plan, or when the run would be refused (`plan not written: ...`) |
| `approve --task T [resolution flags] [--allow-writes] [--plan-out P] [--out A] [--run]` | resolves `T` as `prepare` does, writes the plan to `P` (default `<task dir>/<stem>.plan.json`, a trailing `.task` dropped: `T-1.task.json` -> `T-1.plan.json`), then exactly the `approve --plan` path below. An identical plan already at the default path is kept | exit `2`: a task the run would refuse; a different plan at the default path unless `--plan-out` names it; a `--plan-out` that exists and is not a plan; non-interactive stdin when an approval is needed (checked before any file is written) |
| `approve --plan P [--out A] [--run]` | calls no model and opens no database. Resolves the plan again; at an interactive terminal it prints the summary, the binding digest and an 8-hex **approval code** (the start of the binding digest) on stderr, reads the code (trimmed, any case), then your approval in your own words (one line). Writes the authorization file as an exclusive create: by default `<plan stem>.<12 hex of the digest>.authorization.json` beside the plan. Stdout carries only the result and `next: hflow run --plan P` | exit `2`: stale plan; task the run would refuse; stdin not a console (piped, redirected or the NUL device) - refused unread, before anything is shown; wrong code (the second question is never asked); empty approval; target file exists (the refusal names the `run --plan` that would use it). Exit `4`: a resolution flag or `--plan-out` with `--plan` |
| `approve ... --run` | the same approval, then - only once the file is written - `running hflow run --plan P --authorization-file <that file>` in the same process; the exit code is the run's | nothing runs when the approval fails |
| `run --plan P [--authorization-file A]` | takes every resolution input from the plan and uses the plan's data dir. Without `--authorization-file` it uses approve's default file when it exists (stderr says so). Every check a run makes still runs: the binding, `root_limits`, single use, the credential and launch refusals | exit `4`: any resolution flag (`--task`, `--project`, `--project-root`, `--profile`, `--driver`, `--allow-writes`, `--root-budget-file`, `--repair-policy-file`), or a `--data-dir` naming another directory. Exit `2`: a stale plan (before anything is claimed); a real-driver plan with no approval file ("approve the plan first") |

A plan that needs no authorization (offline fake driver, no root budget) is said to need none
(exit `0`, nothing written). An offline plan with a root budget can run directly: `run --plan`
charges the root to a CLI-minted record labelled as not a user approval, and `approve --plan` is
the optional alternative.

**Stale plans.** `approve` and `run --plan` refuse a plan whose inputs no longer resolve to its
binding, root ceilings or submission count - an edited task or contract, a moved base branch,
another profile or launch, a changed root file: "plan ... is stale", listing each differing field
(for the effective configuration, each changed field and, where it comes from the environment of
the resolving process, the variable: `DSH_HOME`, `HFLOW_ACPX_CLI`, `HFLOW_ACPX_NODE`, `PATH`,
`SystemRoot`, the data dir's npm DSH install, and `HFLOW_PROFILE` for a plan that names no
profile). The refusal ends with the `hflow prepare ... --plan-out P` command rebuilt from the
plan. A plan written by an older build that set `--base-commit` or `--workspace` is refused (exit
`2`) with that re-prepare command: the task file now carries both.

**The write opt-in.** A task that declares `scope.write_allow` needs `--allow-writes` on `prepare`
or `approve --task` (or `HFLOW_ALLOW_WRITES=1`, `true` or `yes` in that shell); the plan records
the result and the binding covers it, so `approve --plan` and `run --plan` never read the variable.
`run --task` accepts `--allow-writes` or the variable. A plan from before the field keeps the write
permission its own report recorded. Without an opt-in a real run is refused `scope_violation`
(location `scope.write_allow`), naming `--allow-writes`. `hflow init task` prints the `prepare`
command with `--allow-writes` and says why.

The approval code makes approving a deliberate act; it does not identify you: a process that
drives a pseudo-terminal can read the summary and type both lines.

### What stops a run before it dispatches

`prepare` reports the "dispatch precondition" rows under `dispatch_preconditions` - they are the
list the run's own gate raises (`admission.predictable_dispatch_problems`, two callers). The
repair rows apply to offline runs too; the others only to a real driver. Each is refused before a
run row, an authorization record, a reservation or a process exists.

| Condition | Code (location) | Decided by |
|---|---|---|
| a `repair_policy` on a task that is not `worktree`, or not reviewed, or whose worst case (4 dispatches) exceeds `budget.max_agent_turns` | `scope_violation` / `not_implemented` / `budget_exceeded` | dispatch precondition |
| a `repair_policy` with a real transport and no `--root-budget-file` | `budget_exceeded` (`root_budget`) | dispatch precondition |
| write paths with `workspace.mode` not `worktree`; write paths without the write opt-in; a required review with `max_agent_turns` 1 | `scope_violation` / `budget_exceeded` | dispatch precondition |
| a launch that cannot be resolved: no acpx client; no `dsh` on an absolute `PATH` entry or in the data dir's npm install; no `node`/`python` the client needs; a relative explicit program; a launcher, `dsh`, interpreter, acpx entry or npm carrier `node` inside the project root or `<repo>.hflow-worktrees` (or a workspace inside the client's `node_modules` tree); no `SystemRoot`; a shim path holding `& \| < > ^ % ! " ( ) @ ; , =` | `not_implemented` | dispatch precondition |
| `.acpxrc.json` or `.env`, any letter case, at the root of the starting workspace (the project root in place; the base commit's tree for a worktree, via `git ls-tree`). Listed, never opened | `workspace_client_config` / `workspace_env_file` (`workspace`) | dispatch precondition |
| a bound `DSH_HOME` that is relative, unresolvable, or equal to, inside or around the project root or worktree directory | `dsh_home_in_workspace` (`launch.dsh_home`) | dispatch precondition |
| no credential source: `DEEPSEEK_API_KEY` not among the child's variable names and the child's DSH home holding neither `.credentials.yaml` nor `.env` as a regular file (stat only; an unbound home is the empty per-invocation one, so only the environment counts) | `no_credential_source` (`launch.credentials`) | dispatch precondition |
| a real delivery whose approved checks are `kind=fake` | `not_implemented` (`checks.<id>`) | admission |
| a `write_allow` entry with `*`, `?` or `[`; under the built-in deny list (`.git`, `.hflow`, `.acpxrc.json`); with a segment ending in a dot or space or holding `:`; resolving (8.3 short name) to a denied path; or passing through a link or junction | `scope_violation` (`scope`) | admission |
| a `write_deny` entry that is empty after normalization, absolute, drive-qualified, holds `..`, or starts with `/` or `\` | `scope_violation` | admission |
| unknown check, scope violation, risk below the project floor, `integrated`/`published` delivery requested, reuse not decided (`reuse_not_approved`), budget above the project ceiling, a non-empty `dependencies` list | admission codes | admission |
| not enough authorization allowance for the whole fixed loop | `budget_exhausted` | controller, before the run row |
| a root whose `max_repairs` cannot cover the repair the run may buy (`needed` is 2 when an earlier run already dispatched an implementer on the root) | `budget_exhausted` | controller, before the run row (`prepare` reports only the `max_repairs: 0` case) |
| a run without `--root-budget-file` for a task that already has a root in this ledger | `budget_exhausted` ("pass --root-budget-file") | controller; the dispatch transaction repeats it as a race backstop |

A run that already exists for an identical TaskSpec is a history query: it returns the recorded
outcome and consumes nothing. To run again, submit a new revision (a changed task).

## What an approval covers

The authorization binds the **effective configuration**, not only the task: mode, driver,
project, repository path, base commit (as a SHA), task digest and path, the roles that will be
dispatched, the effective configuration digest (profile, per-role agents, drivers, model
selections, limits, write permission, resolved launch), the project contract digest, the launch
content digest, the ledger path and, for a root run, the root binding and `root_limits`.
`prepare` prints it, the plan saves it, `approve` writes it after resolving again, and `run`
verifies against the same resolution. Any mismatch refuses the run, naming the fields.

- **Profile and launch.** Another profile, `HFLOW_ACPX_NODE`, `HFLOW_ACPX_CLI`, `DSH_HOME`, or a
  different `dsh`/`node`/`python` earlier on `PATH` changes the digest. The launch is resolved once
  and consumed by the driver; nothing re-reads the environment afterwards. A bound `DSH_HOME` is set
  on the child and an unbound one is removed from it, `extra_env` included.
- **Contract.** Editing `.hflow/project.json` after approval refuses the artifact ("the project
  contract changed since approval").
- **Ledger.** Single use is per ledger, so a real-driver approval names the absolute ledger path
  and `run` against another data dir refuses it ("issued for another ledger").
- **Base commit.** A ref such as `main` is resolved once to a 40-hex SHA and that SHA is bound; an
  approval stops applying once `main` moves. The run records `base 'main' -> <sha>` and uses that
  commit for the worktree, every round and the receipt.
- **Launch entry files are bound by content; transitive modules remain bound by path.** The fixed
  list: the client interpreter (`node.exe`), the acpx entry and its `package.json`, the
  `@agentclientprotocol/sdk` `package.json` Node resolves from that entry, the dsh launcher, and
  for a Desktop or npm carrier the carrier entry its shim runs and that file's `package.json` (for
  the Desktop app, whose entry lives inside `resources\app.asar`, the archive file is bound
  instead, `carrier_archive`; an entry also under `app.asar.unpacked` is refused). For the **npm
  carrier** the `node` its shim runs - the `node.exe` beside the shim, else the first `node` on the
  child's `PATH` - is bound too (`carrier_interpreter`), refused inside a workspace and looked up
  again in the child's environment at the spawn gate. Plans and approvals for the npm route
  prepared before this build must be prepared again. `prepare` and `doctor --profile` print each
  file's kind, SHA-256, size and path; a changed file refuses the artifact
  (`launch_content_changed`).
- **The spawn gate hashes the same list again** just before `CreateProcess` and refuses a changed
  byte, size or resolved path (`launch_content_changed`, no process created, the run blocked).
- **Not covered:** `node_modules` beyond those `package.json` files and anything Node loads at
  runtime, DSH's own code loading, the Desktop carrier's `DeepSeek Harness.exe` and
  `app.asar.unpacked`, `cmd.exe`, and what an unclassified launcher starts. Windows has no
  exec-by-handle, so a file swapped between the gate's hash and process creation is not caught.
- **The ACP SDK is not pinned** (user ruling 2026-10-14): acpx 0.17.1 accepts any
  `@agentclientprotocol/sdk ^1.4.0`. Its `package.json` is bound by content, and `prepare`/`doctor`
  note a version outside `CHECKED_SDK_VERSIONS` (1.4.0, 1.8.0); after a reinstall run `python
  tools/m0_probe/real_client_checks.py all --acpx-cli <.../acpx/dist/cli.js>` and add the version
  only when it passes.
- **Not bound:** the launch-surface record and the client and carrier versions. A changed
  `AGENTS.md` does not change the digest.
- A launch built by hand rather than by `resolve_launch_config` carries no digests, so the spawn
  gate has nothing to compare it with. A Node or npm upgrade changes the digests and needs a new
  `prepare` and approval.
- An artifact that lacks a field this build binds (configuration, contract, launch content,
  ledger path) cannot authorize a run that resolved one: prepare and approve again. Its digest is
  unchanged, so a consumed approval never looks unused.

**Trust model: trusted-local, user-attested.** Enforced: the binding; consumption bounded per
authorization id by a guarded UPDATE plus a CHECK constraint (a restart, a new run id or a
resubmitted spec cannot restore it); `provided_by` must be `user`; an `authorization_id` already
recorded with another binding or immutable field is refused, never overwritten; `origin`
separates a user artifact from the CLI's offline record, which no real driver accepts. **Not
enforced:** provenance - nothing distinguishes bytes you typed from the same bytes written by the
executing agent - and a fresh authorization id is a fresh allowance. Unattended execution
therefore stays disabled. Before any dispatch a zero-model preflight runs the client with
`--version`; its failure refuses the run and consumes nothing.

## Root budgets and the dispatch ledger

A **root** is one requirement (`project_id`, canonical repository path, `task_id`) across
revisions; its id `root-<32 hex>` is derived, never chosen. `--root-budget-file` on `prepare`,
`approve --task` and `run --task` declares what it may spend in total:

```json
{ "limits": { "max_top_level_submissions": 4, "max_repairs": 1, "deadline_seconds": 86400 },
  "note": "free text; never an approval" }
```

| Field | Meaning | Range |
|---|---|---|
| `max_top_level_submissions` | implementer + reviewer dispatches for the whole root | 1..64 |
| `max_repairs` | additional implementer attempts: every implementer after the root's first is one, a later revision's first included. A ceiling, not a switch: only a task's `repair_policy` arms a repair | 0..8, default 0 |
| `deadline_seconds` | wall clock from the root's first successful reservation, never reset. A dispatch past it is refused; each role's deadline, the checks' shared countdown and the repair are capped by what is left; acceptance re-checks it. It stops no remote request or bill | >= 60, default 86400 |

Unknown members, a missing `limits` or an out-of-range value is refused, never defaulted.

- A real-driver root run needs an authorization that binds the same root and whose `root_limits`
  equal the file's `limits`. An offline root run without one is charged to a CLI-minted record
  (`AUTH-offline-...`, `origin: cli_offline_synthetic`) whose text says it is not an approval.
- One transaction reserves each dispatch: ownership, phase, root allowance, deadline, authorization
  allowance, attempt and invocation rows and every counter commit together or not at all.
  Replaying the same invocation id charges nothing.
- **One root runs one task at a time**; the whole remaining loop must fit before the implementer
  is bought; one review per candidate.
- A **pending** invocation (`reserved`, `requested`, `started`, `unknown`, `launch_unknown`) blocks
  every later dispatch of its root, under a new revision too, and also a rootless run of the same
  task. Nothing is refunded, retried or re-dispatched.

The ledger keeps four facts apart: an allowance reserved, a launch requested, a launch that
happened (and separately whether a process was reported), and a result settled. The states are
defined in `docs/architecture.md`, "State". `not_started` is recorded only on a driver's own word
that nothing launched, or when no driver was asked; a launch requested and never reported is
`launch_unknown`. A settled, `not_started`, `unknown` or `launch_unknown` entry never moves again
on a result or a stop; the refused settlement becomes a `dispatch:` note.

**`hflow resume <run-id>` on an ended run** closes entries left open (a confirmed stop whose ledger
write failed, a driver that raised, a failed settlement write) once the owner is provably gone: an
entry named by the run's own confirmed stop is closed from that fact; every other one becomes
`unknown` (a launch was recorded) or `launch_unknown`, with a `dispatch: resume closed ...` note.
The run's state, block code, receipt and outcome stay as they were; a second `resume` is a no-op.
Until then `status`/`report` show an `open entries` section. A pre-v6 run that recorded a
controller pid with no host needs `--legacy-owner-gone --attest "<text>"` (1-2000 characters, no
NUL): your attestation that its controller exited, recorded verbatim with the OS user. It never
passes an owner this build recorded and never takes a live run over.

**`hflow ledger settle <invocation-id> --as consumed|void --attest "<text>"`** moves one `unknown`
or `launch_unknown` entry of an ended run to `operator_settled` and appends a row to
`invocation_settlements` (prior state, choice, OS user - recorded, not authenticated - UTC time,
attestation). `consumed` (default) keeps every counter spent; `void` is only for `launch_unknown`
and returns that dispatch's root charge (and its repair charge; a voided first implementer no
longer counts as the root's first), never the authorization counter or the run's turns.

| `ledger settle` refuses | Exit |
|---|---|
| a placeholder, blank, NUL-bearing or oversized attestation (2000 characters, counting the suffix `--legacy-owner-gone` appends) - checked before the ledger opens: `refusing: <problem>; nothing was done` | `4` |
| an unknown invocation id | `4` |
| an open or final entry, a run that has not ended, `void` on `unknown` | `2` |
| an owner not provably gone (lock free and identity `gone`; a run with no owner token passes only when it recorded no controller process, or with `--legacy-owner-gone`), or the entry's own child not reading `gone` on the owner's recorded host (`matching` or `unknown` refuse; a pre-v6 run with no host reads `unknown` until `--legacy-owner-gone`) | `2` |

Settling unblocks the root for a new revision only; that revision needs its own approval. The entry
then reads "settled by operator attestation (not observed)" and billed usage stays unknown.

## When a repair may happen

A repair is **one** second implementer attempt in the same run and revision, starting from the
frozen candidate, followed by fresh checks and a fresh review. It is never a command, never revives
a `BLOCKED` run and never retries an environment or transport failure. It is armed only by the
task's own `repair_policy` (or `--repair-policy-file`, the same bare document, which becomes part
of the effective TaskSpec and its digest; a different policy in the task file is refused):

```json
"repair_policy": { "max_attempts": 1, "check_exit_codes": { "unit": [1], "lint": [1, 2] }, "allow_reviewer_changes": true }
```

`max_attempts` is pinned to 1. A policy with no trigger, an empty code list or exit code `0` is
refused. A real-driver repair needs a root (`used_repairs + needed <= max_repairs`, checked before
anything exists); a fully offline run may repair rootless.

**A check failure buys the repair only when all hold**, for the current attempt and candidate: the
check id is in `check_exit_codes`; its recorded exit code is listed for it (an undeclared code never
repairs); its `exit_reason` is `completed` or `nonzero_exit` (`timed_out`, `settlement_forced`,
`settlement_unknown`, `output_capture_error`, `not_launched`, `boundary`, `startup`, `empty_argv`,
`no_artifact_dir` and an empty reason never do); and the round has no ERROR row at all. Otherwise a
`not_a_business_failure` decision names the check, its code and what the policy declared.

**A reviewer rejection buys it** when the verdict is a valid `changes_requested` on the current
candidate, `allow_reviewer_changes` is set, and it carries at least one finding. Findings are typed
(`contracts.Finding`): `body` (required, non-blank), optional `title`, `location` (`path`,
`line_start >= 1`, `line_end >= line_start`), `severity` (`P0`-`P3`, recorded, gates nothing) and
`id`; nothing else, nothing coerced. `changes_requested` with `[]` records `no_findings` and stops.
A finding that breaks the schema makes the whole answer a `review_protocol_error`, never a
rejection. The schema checks shape only: a `location` is not checked against the candidate and
`severity` is the reviewer's own label. Review evidence stored before typed findings existed is
displayed as stored and never re-validated.

**The repair round** is its own `attempts` row (`is_repair = 1`; the schema allows one). Its
packet carries the original goal, scope and base, the previous candidate, the failed checks and
every finding (JSON-quoted, values over 2048 bytes cut and marked; a findings section that pushes
the packet past 32 KiB blocks the run before the repair is bought). Its reviewer sees the
cumulative diff from the original base plus a labelled "this round's change". A second failure, or
a repair that changes no scoped content (`verification_failed`, or `no_content_change` ->
`scope_violation` when only unscoped files moved), ends the run. The repair reuses the first
round's worktree only while it is exactly the previous candidate (same HEAD and tree, clean, no
assume-unchanged/skip-worktree flags) and the shared Git metadata is unchanged; otherwise
`workspace_drift` blocks `scope_violation` before anything is bought. Ignored caches the first
round's checks left outside the fingerprinted scope are carried as literal entries; an ignored
file inside a `write_allow` directory refuses the repair. Every decision, refusals included, is a
`RepairRecord` in the run's record.

Offline: `kind=fake` evidence records `not_launched` and never repairs; an offline test that wants
the repair path declares the exit reason it models
(`FakeCheckRunner(exit_reasons={"unit": "nonzero_exit"})`).

## Reading a run

`hflow status <run-id>` prints the state, the block code, the root and ledger counters, attempts,
model/stream/launch lines per invocation, repair decisions, evidence, integrations and `next:`
lines. What each counter does and does not mean: `docs/architecture.md`, "Reading the counters
honestly". `--project-root` changes the drift check's workspace (default: the project root the run
recorded, while it exists).

**`hflow status` with no run id is the inbox**: every run whose stored facts name something for
you, from ledger reads and the clock only (no model, no process probe, no Git, no write; no ledger
is created), grouped and printed as one line of facts and one `next:` per run:

| Group | Runs |
|---|---|
| Running | a live run within the per-dispatch deadline its admission recorded; an accepted candidate an integration process may still be working on |
| Ready | an accepted Git candidate with nothing else in the way: no integration yet, one ready to apply or hand-merge, one `stale` or `superseded` to prepare again |
| Attention | a live run past that deadline (probably interrupted; `resume` decides), a blocked run, open or unresolved ledger entries, an integration that ended `conflict`/`checks_failed`/`interrupted`/`failed`, an interrupted cleanup |

`--json` prints the `runs --json` entries plus `group`; an unreadable run is an entry with `error`
in `attention`. Exit `0`, or `6` when a run could not be read (named on stderr); `--project-root`
without a run id exits `4`.

## Reading a blocked run

### Block codes

| Code | Meaning | Next |
|---|---|---|
| `budget_exhausted` | the turn ceiling, the authorization or the root could not cover the next dispatch, or the root's deadline passed (at a dispatch, during the checks or at acceptance) | split the task or raise limits deliberately; a root is never topped up |
| `verification_failed` | an approved check failed (the worker's exit code is irrelevant), or a repair changed nothing | read the evidence; new revision |
| `scope_violation` | a changed path outside `write_allow` or under any deny list; the worker moved HEAD; an index flag hid changes; the delivery left the scope; a `write_allow` entry started resolving outside the worktree; `no_content_change`; `workspace_drift`; the shared Git metadata changed after dispatch | inspect the paths; for metadata, restore it before `clean` or another worktree run |
| `evidence_stale` | the candidate changed after verification, or the worktree is no longer the frozen candidate at acceptance | re-run; never reuse the evidence |
| `review_rejected` | a validated `changes_requested` | read the findings; new revision |
| `review_protocol_error` | no usable verdict: missing, malformed or ambiguous output, a `messageId` that resumes after another message, a chunk after the prompt response, a stream not read to its end (`review_ambiguous`), a reviewer that could not start or `FAILED`. A wire failure, never the reviewer's judgment | fix the cause; new revision |
| `outcome_unknown` | nobody knows how an invocation ended: an unconfirmed stop (every cross-process `cancel`), an interrupted controller, or an unknown driver outcome (table below) | `hflow resume` once the owner is provably gone; the root stays blocked while the entry is open |
| `owner_lost` | `resume` took a live run over from an owner proven gone | see "Recovering from a controller crash" |
| `cancelled_by_operator` | a confirmed stop, or a stop before any dispatch | nothing runs; workspace and evidence kept |
| `driver_failed` | the implementer's driver failed before or without a result (e.g. `model_rejected_before_prompt`) | fix the environment or profile; new revision |
| `workspace_client_config`, `workspace_env_file`, `dsh_home_in_workspace`, `no_credential_source`, `launch_content_changed` | the driver's spawn gate refused, before any process existed, something that appeared or changed after admission (for the reviewer, in the candidate worktree). The invocation is `not_started`; its allowance stays consumed | fix it, then a **new revision** (an identical TaskSpec returns this run; under a root its first implementer is charged as a repair) |
| `context_file_change` | the candidate changes a file DSH loads as instructions or skills (`AGENTS.md`, `CLAUDE.md` and their `.local` forms at any depth, root `.dsh/skills`, `.agents/skills`) that no `write_allow` entry names by its path, or touches the root `.env` at all | name each file in `write_allow` in a new revision; a root `.env` change is never accepted |
| `internal_error` | a controller step that is not the worker's result failed (worktree creation, `freeze incomplete`, an implementer prompt-digest mismatch, a driver that raised, a refused transition) | `block_reason` names the step |
| `not_implemented` | a driver or delivery level this build does not have | use `--driver fake` or `local_candidate` |

### Driver error codes

Stored with the invocation's result (`attempts.result_json` / `review_json`), quoted in
`block_reason`; not block codes.

| `error_code` | Outcome | Meaning |
|---|---|---|
| `unbound_completion` | `OUTCOME_UNKNOWN` | a terminal response answers no observed `session/prompt` id |
| `model_rejected_before_prompt` | `FAILED` | acpx refused the `--model` value before any prompt was sent; nothing reached a model, nothing is refunded |
| `boundary_not_empty` | `OUTCOME_UNKNOWN` | the client exited but its Job could not be confirmed empty |
| `cancelled_unrequested` | `FAILED` | settled `cancelled` with no stop requested (DSH also does this when it disposes of a session) |
| `stop_reason_<reason>` | `FAILED` | `max_tokens`, `max_turn_requests` or `refusal` |
| `unknown_stop_reason` | `OUTCOME_UNKNOWN` | a stop reason outside ACP v1's set |
| `prompt_error_response` | `OUTCOME_UNKNOWN` | the prompt was answered with a JSON-RPC error (DSH's failed turn); code and the first 500 characters recorded. Whether a model call was made is not observable. An error carrying the prompt's id after an agent request reused that id is not attributed (limitation `prompt_error_unattributed`) |
| `no_stop_reason`, `output_limit_exceeded`, `unparseable_output`, `completion_timeout` | `OUTCOME_UNKNOWN` | the stream does not say how the turn ended |
| `reader_failed` | `OUTCOME_UNKNOWN` | the stream reader stopped early or could not start; nothing is judged from the prefix |
| `stream_not_drained` | `OUTCOME_UNKNOWN` | implementer: the output was not drained in time after exit (a reviewer gets `review_ambiguous`) |
| `prompt_write_incomplete` | `OUTCOME_UNKNOWN` | the prompt could not be fully written to stdin |

`stream_order` (where the prompt response fell, and how many updates followed it) is recorded for
every bound result read to its end and shown as a `stream` line; a count above zero is a
limitation and changes no outcome except the reviewer's. `end_turn` means the turn settled, not
that the work succeeded: acceptance is decided by checks and review.

## Worktree runs: freeze, scope and receipt

A task selects isolation with `"workspace": {"mode": "worktree", "base_commit": "<sha or ref>"}`
(`hflow init task` writes the current branch). The worktree is created at a staging path and moved
into place with `git worktree move`; HFlow never runs `git worktree repair`. Your HEAD, index,
working files, stash and branches are not written to, but the worktree shares the repository's
objects and admin files.

The freeze (after the implementer, before any check):

- **HEAD must not move.** A worker commit, amend, reset or checkout is refused `scope_violation`
  ("the worker moved HEAD from X to Y").
- **No index entry may be flagged** assume-unchanged or skip-worktree ("index flags hide worktree
  changes from the freeze"); HFlow never clears the flags.
- A changed path under the task's or project's `write_deny` or the built-in list (`.git`, `.hflow`
  at the root, `.acpxrc.json` anywhere) blocks before the freeze, even inside an allowed directory.
- Each `write_allow` entry is staged with `git add -A` under literal pathspecs (a deleted listed
  file is committed as a deletion); anything staged outside the scope refuses the commit, and a
  worktree that still shows a change afterwards blocks `internal_error` ("freeze incomplete").
- **Ignored bytecode is not a candidate.** An ignored file the scoped fingerprint would hash, a
  sourceless `.pyc` outside `__pycache__`, or any ignored file under `__pycache__` other than a
  `.pyc` refuses the freeze. Before each `command` check of a worktree run, regular `.pyc` files
  directly inside a real `__pycache__` are deleted (counted in the evidence and a
  `bytecode_removed` note); a link there refuses the check (`bytecode_not_cleared`). An in-place
  run deletes nothing.
- A directory link or junction is one entry (`link:<target>`), never walked.
- After the freeze, and again at acceptance, the whole diff from the original base is held to the
  scope and every deny list, then classified against DSH's context list (`context_file_change`,
  above). Path lists use `--no-renames`, so a move names its deleted source too.
- Declared context files reach the reviewer and repair packets in a section headed as untrusted
  data, with a bounded diff (8 KiB). DSH still loads the candidate's version in the reviewer's and
  the repair implementer's worktree; the section is a label, not enforcement. Not covered at all:
  such files already in the base commit, in-place runs, a link to a directory that holds one, an
  ignored `AGENTS.md` under an allowlisted cache directory, a `.env` below the root, and anything
  under the DSH home.

**HFlow's own Git cannot be hooked or redirected.** Every call forces `core.hooksPath` below a
system file no worker can write, disables maintenance, gc, fsmonitor, signing, `ignoreStat` and
sparse checkout, sets `safe.bareRepository=explicit`, switches off replace objects, lazy fetch,
grafts and the system config, drops inherited repository-locating and read-redirecting variables
(`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG`, `GIT_EXTERNAL_DIFF` and the rest), and
commits with `--no-verify`; the list is in `docs/architecture.md`. Your global config is still
read, and filters stay on (LFS needs them), so a filter is caught by comparison instead:

**Shared Git metadata is compared.** Right after `worktree add`, HFlow digests every configuration
key your checkout and the worktree see, the files they come from, both `config.worktree` files,
`.git/info/attributes` and the global attributes file, and records only the digest
(`git_metadata: snapshot before dispatch sha256:...`). It compares again before the freeze,
before a repair round and at acceptance; a difference blocks `scope_violation` ("shared Git
metadata changed since the pre-dispatch snapshot (...)", keys shown as `filter.*.clean`, at most
five). A run that ends without a receipt for another reason gets a note only (`git_metadata:
changed when the run ended ...`) and keeps its block. `status`/`report` show every such note as a
`git metadata` line. Git output that is not text in the locale's encoding counts as unreadable.
What to do: `git config --list --show-origin --show-scope`, inspect `.git/info/attributes` and
`git var GIT_ATTR_GLOBAL`, and restore them before `hflow clean` or another worktree run (HFlow
restores nothing); see "What is not safe yet" for what the comparison does not cover. An in-scope
`.gitattributes` the worker wrote is a receipt limitation, not a refusal.

What the receipt gives you:

| Field | Meaning |
|---|---|
| `candidate.base_commit` | the resolved SHA the worktree started from; after a repair still the original base |
| `candidate.git_commit` / `git_tree` | the frozen candidate as Git objects, kept by `refs/hflow/candidates/<run-id>/<attempt-id>` |
| `candidate.fingerprint` | content hash over the write scope - **not** a Git id |
| `candidate.worktree` | where the candidate lived until you clean it |
| `candidate_paths` | paths changed from the base to the final candidate |
| `verification.status` / `evidence_ids` | the approved checks' result on that candidate |

`hflow diff <run-id>` (or `--last`, `--stat`) prints the Git diff between those two recorded
commits; it never reads a working tree.

## Integrating an accepted candidate

A run ends at `ACCEPTED / LOCAL_CANDIDATE`. Putting the change on a local branch is a separate
delivery with its own record and `IntegrationReceipt`; the run's receipt is never rewritten.
Nothing here calls a model, dispatches or spends a budget. Contract:
`docs/batch-i-integration-plan.md`.

### `hflow integrate <run-id>`

```sh
hflow integrate <run-id> [--target <branch>] [--project <project.json>] [--owner-gone --attest "<text>"] [--keep-worktree] [--json]
```

It continues the run's integration from whatever state it is in, and is safe to run again. It
continues the run's integrated record if one exists, else its newest (only records for
`--target`'s branch when given):

| State found | What it does |
|---|---|
| none, or `stale` / `interrupted` / `failed` / `superseded` | prepares (below). Target: `--target`, else that record's branch, else the task's base branch when it is a local branch of the run's repository (otherwise exit `4`: pass `--target`) |
| `ready` (including just prepared) | reconciles first (a hand merge is recorded `integrated`, a moved target `stale`). Still `ready` and stdin is not an interactive terminal: stops with `next: hflow integrate <run-id>  # run it at an interactive terminal ...` (exit `0`). At a terminal: prints the record and an 8-hex **confirmation code** on stderr, reads the code you type (any case), and only on a match applies with `--expect-target` bound to the tip just shown - a branch that moved in between ends `stale`, never moved from elsewhere |
| apply finds the target checked out | prints `git -C <checkout> merge --ff-only <M>` and `next: hflow integrate <run-id>` (exit `3`); run it again after your merge to record it |
| `preparing` / `checking` / `applying` | reconciles, passing `--owner-gone --attest` through |
| `integrated` | reports it; nothing written |
| `conflict` / `checks_failed` | reports it (exit `3`) and does not prepare again: that decision is yours (`integrate prepare`, or a new revision) |

| Exit | When |
|---|---|
| `0` | stopped at `ready` without a terminal, or `integrated` |
| `2` | wrong confirmation code (nothing applied), a refusal, a Git error |
| `3` | hand-off, `stale`, `interrupted`, `conflict`, `checks_failed`, `failed`, or a ref update that failed and left the record `ready` |
| `4` | unknown run, no default target, `--project` needed, a bad `--attest` |
| `5` | another process may still be working on the integration (the `next:` line says when to run it again, or offers `--owner-gone` when HFlow cannot tell) |
| `6` | an unreadable stored record |

**Auto-clean.** When `integrate <run-id>`, `integrate apply` or `integrate reconcile` brings an
integration to `integrated` in this process (and `integrate <run-id>` whenever it finds one
integrated), the run's worktree is released by the same guarded cleanup `hflow clean <run-id>
--apply` runs, never forced. The result prints as `cleanup <STATUS>: ...` with any `refuse` lines
(`--json`: a `cleanup` member); a refusal, `PARTIAL` or error never changes the exit code.
`--keep-worktree` keeps the worktree (`cleanup KEPT`).

### The steps underneath

```sh
hflow integrate prepare <run-id> --target <branch> [--project <project.json>]
hflow integrate apply <integration-id> --expect-target <full tip printed by prepare> [--keep-worktree]
hflow integrate reconcile <integration-id> [--owner-gone --attest "<text>"] [--keep-worktree]
hflow integrate show <integration-id> [--json]
```

**prepare** fixes the target's tip `T` and builds one commit `M` on it with Git plumbing:

| Target since the task's base `B` | Mode | `M` |
|---|---|---|
| unchanged (`T == B`) | `squash` | one commit with the candidate's tree on `B`; earlier repair rounds never enter the branch |
| moved on (`B` is an ancestor of `T`) | `replayed` | `git merge-tree --write-tree --merge-base=B T C`, committed on `T`; a conflict ends `conflict` with its paths and commits nothing |
| rewritten | - | refused `target_moved` |

`M` is kept by `refs/hflow/integrations/<run-id>/<integration-id>` and checked out in a worktree
of its own (`<repo>.hflow-worktrees/<integration-id>`), where the run's required checks run again
(evidence kind `integration-check`). `T..M` must change only paths the delivery changed, inside
the scope. The checks are bracketed by a shared-Git-metadata snapshot: a change ends
`checks_failed` with a detail starting "shared Git metadata changed", leaves that worktree in place
(`LEFT`, no status read), and adds a run note `git_metadata: changed|unreadable during integration
...` - so `hflow clean` of the run refuses until the metadata is restored. Otherwise the worktree is
removed without `--force` (left and named if Git refuses). A pass ends `ready`. prepare never moves
a branch or writes your checkout. It refuses: a contract whose digest differs from the one the run
was accepted under (`project_mismatch`, exit `2`; with no `--project`, exit `4` when
`<project root>/.hflow/project.json` no longer is that contract); a `kind=fake` check; Git older
than 2.40; a `--target` no ref carries in exactly that spelling; a run whose change already
reached that branch through an earlier integration. A newer prepare marks the run's `ready`
record `superseded`, unless the branch already contains it (then it is recorded `integrated`).

**apply** is your approval. `--expect-target` must be exactly `T`; the run must still be
`ACCEPTED`, the integration ref must point at `M` and its evidence must still describe `M`. The
intent (`applying`) is recorded before `git update-ref refs/heads/<target> M T` (compare-and-set).
It never forces, creates or deletes a branch.

| apply finds | Result |
|---|---|
| target at `T`, not checked out | moved to `M`; `integrated` (`hflow_ref_update`), receipt written |
| target checked out in any worktree - HEAD names it in any letter case, or a rebase or bisect will return to it | not moved: `git -C <checkout> merge --ff-only <M>`, then reconcile (exit `3`) |
| target already contains `M` | `integrated` (`operator_merge_observed`) |
| target moved elsewhere | `stale`; prepare again |
| the ref is locked (`<ref>.lock` held by another Git process or left by a crash) | the compare-and-set fails with Git's own "cannot lock" message and the record returns to `ready` (exit `3`); HFlow never deletes a lock file |

**reconcile** settles a record from what Git shows and never re-runs an update or a check:
`applying` with its process gone -> `integrated` (`observed_after_interruption`) if the target
contains `M`, `ready` if it is still `T`, else `stale`; `preparing`/`checking` with its process
gone -> `interrupted` (its worktree removed without force when possible); `ready` -> `integrated`
once the target contains `M` (how a hand merge is recorded), `stale` once the target moved
elsewhere, else unchanged. Terminal records are only reported. While the process may still run it
writes nothing (exit `5`). When HFlow cannot tell (another host, no creation time, a pid it may
not open), `--owner-gone --attest "<text>"` (at most 500 characters, recorded whole in the run's
audit note with your OS user) attests that it exited; a process seen running is never overridden.

States: `preparing -> checking -> ready -> applying -> integrated`; terminal `conflict`,
`checks_failed`, `stale`, `interrupted`, `superseded`, `failed`. One active record per run, one
`applying` per repository and branch. Subcommand exits: `0` when it did what it was asked
(`prepare` -> `ready`, `apply` -> `integrated`, `reconcile` -> `ready` or `integrated`), `3` for
anything needing your decision, `5` for a record another process may still be working on, `2`
refused (Git errors included), `4` unknown id, bad `--attest` or `--project` needed, `6`
unreadable record.

An integration receipt claims nothing about a remote (nothing is pushed), a review of a merged
tree (a `replayed` tree is checked, not reviewed), or who merged a hand-off. `M` carries HFlow's
fixed identity.

## Releasing a workspace (`clean`)

`clean` removes a run's working directory, never the delivery:

```sh
hflow clean <run-id>              # preview (same as --dry-run); changes nothing
hflow clean <run-id> --apply      # remove the worktree, without --force
hflow clean <run-id> --reconcile  # after an interrupted cleanup, decide from recorded facts
```

`--apply`, `--dry-run` and `--reconcile` exclude each other (exit `4`). The preview prints the
path, Git common directory, registration, HEAD, candidate ref and every reason. `--apply`
re-checks everything, claims the cleanup in a transaction that requires a terminal run and no
active attempt, and succeeds only when both the directory and its registration are gone (exit `0`;
also for an already recorded removal; otherwise `3`). It identifies the worktree from the
`WorkspaceProvenance` recorded with the run, never from notes or receipt paths.

| Refusal | Why |
|---|---|
| `no_managed_workspace`, `workspace_provenance_missing`, `provenance_unreadable`, `provenance_mismatch`, `not_a_worktree`, `not_registered`, `is_source_repository` | not provably this run's linked worktree |
| `execution_active`, `run_in_flight` | an attempt or the run is live |
| `stop_unconfirmed` | a stop of a live run came back `unknown`/`still_running`, or an intent has no receipt (a stop that reached an already-ended run and answered `unknown` does not refuse) |
| `git_metadata_changed` | the run carries a `git_metadata: changed` or `unreadable` note and the shared metadata does not match its pre-dispatch snapshot (or none was recorded); checked before any status read, since that read would run a filter the metadata names |
| `unfrozen_changes`, `head_drift` | changes never frozen; HEAD is neither the frozen candidate nor (without a receipt) the base |
| `unretained_commit` | without a receipt, HEAD is a commit no branch, tag or `refs/hflow/` ref contains; create a ref (`git branch <name> <commit>`) to keep it |
| `unknown_ignored_files` | ignored files other than `*.pyc` and `.pytest_cache` (an `.env`, a `.ruff_cache`) |
| `index_flags_hide_changes` | assume-unchanged or skip-worktree flags |
| `unsupported_status` | unmerged or submodule records |

A refusal keeps everything and releases the claim; several end with read-only `git -C <worktree>
...` commands to inspect it. What survives a successful clean: the candidate commit and its ref,
the receipt, the evidence and the run's history. Limits:

- A file something holds open makes `git worktree remove` delete part and fail: `PARTIAL`, state
  `REMOVING`. Git no longer knows the directory and HFlow never deletes one itself; remove what is
  left, then `hflow clean <run-id> --reconcile` records `REMOVED`.
- Windows can hold a directory briefly after a check exits; HFlow retries once, never forces.
- Never `git gc`, `git clean`, `worktree prune` or `rmtree`; worktrees are not collected
  automatically. DSH on Windows may leave a permission entry on a worktree (documented upstream).
- It guards against HFlow's own concurrent operations, not another process of the same user.

## Running a real Harness task

A real run (`acpx-dsh` through a profile) needs an approval from the plan flow above; there is no
flag substitute and no fallback to the fake driver. A scripted flow writes the artifact by hand:
see "Appendix: writing the artifact by hand". Before a first live run, validate the installed
client offline:

```sh
python tools/m0_probe/real_client_checks.py all
```

It runs the model-free checks: the client reports its version through the production launcher,
accepts the config the driver writes in both permission modes, and completes an `exec` round trip
against the project's mock agent. No live task runs without its own explicit approval.

### M0 transport probe

The transport decision (`docs/adr/0001-transport.md`) rests on this probe:

```sh
cd .probe/acpx && npm install --no-fund --no-audit acpx@0.17.1   # project-local, once
python tools/m0_probe/run_probe.py --phase a --phase b           # local checks + mock agent; no model
python tools/m0_probe/run_probe.py --phase c --live --live-max-submissions 2 \
    --live-credential-ref DEEPSEEK_API_KEY                       # real DSH: needs its own approval
python tools/m0_probe/write_results_doc.py                       # regenerate docs/m0-results.md
```

The probe never touches `~/.dsh`, `~/.acpx`, `PATH` or a global install: each child gets a
probe-private `USERPROFILE`/`HOME`/`DSH_HOME` under the gitignored `.probe/`. The live phase is
bounded by a persisted submission counter, and `--live-credential-ref` is the only path that reads a
credential (one named reference, in memory, never written or logged). `docs/m0-results.md` is
generated from recorded artifacts only (a hand edit is overwritten); its forced-stop row predates
M2 trial A, which `docs/m2-live-acceptance-result.md` supersedes for one machine, one acpx/DSH
version and one binding.

### What each role is told

The controller renders one input packet per role (`src/hflow/packet.py`) from recorded facts; the
driver sends it verbatim on stdin.

| Role | Packet |
|---|---|
| implementer | goal, acceptance criteria and check ids, allowed/forbidden paths, workspace, side-effect limits, deadline, effective file permission |
| reviewer | the same task facts, the frozen candidate (base, commit, tree, fingerprint, diff reference, worktree), per-check results and evidence rows, the review rules and the `ReviewOutput` contract, the read-only constraint |

- Each packet is bounded at 32 KiB; one that does not fit is refused before dispatch. A full diff
  or log travels by reference.
- A failed check is rendered as its reference line (status, exit, reason, artifact, stream sizes and
  digests, never shortened) and then only the check's own text after that reference block,
  JSON-quoted (line separators escaped) and capped at 600 escaped characters. The environment
  summary stays in the evidence row and is not repeated in the packet.
- The digest of the exact text is a `role_input_packet` run note and is reported back by the
  driver (`prompt_digest`); a mismatch blocks the run. It is a local record of what was sent, not
  a receipt from the agent.
- The implementer never sees candidate identities or check results; the reviewer never sees the
  implementer's own summary.
- Each role's stated deadline is the one its invocation gets: the configured value (the reviewer's
  900 s) capped by the root's remaining clock. `prepare`'s preview renders the configured value.

### Transport evidence, kept apart

| Evidence | Proves |
|---|---|
| offline fake driver | the controller's state machine, budget, evidence and receipt rules |
| production driver + Python stand-in for acpx (`tests/test_driver_acpx_dsh.py`) | launch, framing, event projection, stop and reconcile |
| installed pinned acpx + stub or mock agent (`tests/test_packet_wire.py`, `tests/test_real_client_review.py`) | the real client carries the exact packet and completes a turn under the hardened launch |
| real DSH with a model | nothing in this repository claims it (`docs/m2-live-acceptance-result.md` is historical) |

A fake result is never a real-client proof, and a real-client result is never a live-model proof.

### Check output and environment

- A check's stdout and stderr are drained into a bounded sink: 8 MiB retained per stream (the
  invocation budget is 32 MiB). The evidence records `total_bytes`, `retained_bytes`, the SHA-256
  of the retained bytes and `truncated`; files live under `<data-dir>/artifacts/<evidence-id>/`
  with an `artifact.json` manifest. Truncated output never authorizes acceptance on its own.
- Pipes still held open after exit (a descendant inherited them) are `output_capture_error`; an
  artifact directory failure is `no_artifact_dir` before launch; a manifest write failure is
  `output_capture_error`. Each ends `BLOCKED / verification_failed` and never buys a repair.
- The environment is an allowlist (`PATH`/`PATHEXT`, `SystemRoot`/`TEMP`, `USERNAME`, the usual
  shell and locale variables, `VIRTUAL_ENV`/`CONDA_PREFIX`, Python's encoding switches) plus
  `PYTHONDONTWRITEBYTECODE=1` and `PYTEST_ADDOPTS=-p no:cacheprovider`. A credential-like name is
  withheld even when declared, and listed as `withheld_secret_like=`. `PYTHONPATH`, `NODE_PATH` and
  `NODE_OPTIONS` are not inherited; a check that needs one sets it inside its approved argv. Only
  names and counts are recorded. A check still runs with your rights.

### Worker log retention

- Each invocation retains its raw stdout up to the protocol share of a 32 MiB budget, with a 1 MiB
  line bound and a 20,000 nonempty-record bound on parsed state. Past either bound output still
  drains and is counted, but nothing parsed can authorize an outcome: `OUTCOME_UNKNOWN /
  output_limit_exceeded`, no verdict, no refund.
- Stderr is drained from a pipe HFlow owns and retained up to its share; the rest is read and
  counted, not written. Release never waits on that pipe: a still-blocked reader is recorded in the
  driver's release notes.
- A failure to open, seek or read the protocol file is `reader_failed`, never EOF.
- **The client's own protocol file is measured, not bounded.** acpx writes `stdout.ndjson` itself
  and HFlow never trims a file another process writes. Its size is a `client protocol file <path>:
  N bytes read from it (measured, not bounded ...)` limitation in the invocation's result, next to
  what was retained of it.

### Effective permissions per role

| Role | acpx `defaultPermissions` |
|---|---|
| implementer with the write opt-in, in a disposable worktree | `approve-all`: every tool permission request is approved, which is broader than file writes |
| implementer otherwise | `approve-reads` |
| reviewer | `approve-reads`; never inherits the implementer's write permission |

The policy lives only in the per-invocation global config HFlow writes (no permission flag on the
command line), which is safe only because a workspace `.acpxrc.json` is refused. acpx permission
mediation is not a sandbox.

### What DSH does that HFlow does not control

Read from upstream source (DSH up to `dsh-v0.2.0-rc.2`, acpx 0.17.1); documented, not observed:

- **`approve-all` approves DSH's escalations.** DSH's permission requests carry only a tool-call
  id, so the write-capable implementer approves sandbox escalations too (up to
  `danger-full-access`). Do not claim DSH's sandbox confined an implementer run.
- **DSH runs with its own sandbox defaults** (`workspace-write` with `ask`; partial on Windows).
  HFlow removes `DSH_PERMISSION_MODE` and `DSH_TOOLS_MODE`; binding a per-role value is an open
  decision. On the ACP path the mode does not confine DSH's own file tools (DSH #9693,
  unconfirmed), and an ACP client cannot change it per session, so the launch-time mode is final
  (DSH #9735). HFlow's scope check and user-checkout fingerprint are what notice an outside write.
- **DSH uploads session logs by default** (`session-log-deepseek`), so packets and repository
  content can reach DeepSeek that way too. HFlow does not disable it.
- **A failed DSH turn is a JSON-RPC error**, and blocked or aborted turns settle as `end_turn`.
- **acpx `--timeout` is per phase** and sends no `session/cancel`; HFlow's own deadline is the
  bound. HFlow never passes `--prompt-retries`.
- **DSH loads from the workspace and its home:** every `AGENTS.md`/`CLAUDE.md` (and `.local`)
  from the nearest `.git` down to its cwd, skills under `.dsh/skills` and `.agents/skills`,
  `<cwd>/.env` then `$DSH_HOME/.env` (a workspace `.env` setting a bootstrap or `DSH_` name makes
  DSH throw before serving ACP), `cordis.patch.yml` layers in the home, and `$DSH_HOME/AGENTS.md`.
  Credential precedence: launch environment, `$DSH_HOME/.credentials.yaml`, `<cwd>/.env`,
  `$DSH_HOME/.env`. DSH 0.2.0-rc.2 rewrites a pre-release flat `.credentials.yaml` in place at boot
  (DSH #9845), so a first launch may change that file's format.
- **Turn-time git (DSH #9382):** the Web/Desktop bundle's `workspace-changes` plugin runs `git add
  --all` with the repository's config at each turn; the ACP profile HFlow launches does not load it.
  A bound home whose patch layers mention it is noted in the launch record.

### Client launch hardening

- **A workspace `.acpxrc.json` or `.env`, a `DSH_HOME` in or around a workspace, and a launch with
  no credential source are refused twice**: at admission (the table in "Before you run anything")
  and again at the driver's spawn gate, for the role's actual cwd and the environment actually built
  for that child - which catches a file the implementer planted for the reviewer. acpx lets
  `<cwd>/.acpxrc.json` override the agent argv and permissions, and DSH loads `<cwd>/.env`; both
  are matched in any letter case among the root's entries only. At the gate a
  `<repo>.hflow-worktrees/<run>` cwd also protects the checkout it names. Not covered: a file
  written between the gate and the client's read, and 8.3 short names in `DSH_HOME`.
- **Every launch program is an absolute file outside the workspace**, found on absolute `PATH`
  entries only (with `PATHEXT`), never a bare name. `dsh` alone has a second place, used only when
  `PATH` has none: the npm CLI installed with `--ignore-scripts` at `<data-dir>/dsh/0.2.0-rc.2`
  (user ruling 2026-10-14, the route around DSH #9626). The source is the launch's first content
  note (`dsh=PATH` or `dsh=npm (data dir)`). With `HFLOW_ACPX_CLI` unset and no copy under
  `<data-dir>/m0/acpx`, the entry falls back to the HFlow checkout's `.probe/acpx`, so a run on the
  HFlow repository itself is not resolvable until `HFLOW_ACPX_CLI` names an install outside it.
- **The DSH batch shim runs through `%SystemRoot%\System32\cmd.exe /d /c`** (`/d` skips AutoRun);
  a shim path holding `& | < > ^ % ! " ( ) @ ; , =` is not resolvable.
- **The child never searches its cwd for a program**: every child environment (the preflight's
  too) gets `NoDefaultCurrentDirectoryInExePath=1` and drops relative or empty `PATH` entries, and
  `hflow` sets the same variable in its own process before any command runs (a planted `git.exe`
  in the starting directory ran instead of Git before that; observed on Windows 11). An approved
  check's own environment does not carry it.
- **The agent child environment withholds secret-like variables.** Every name for which
  `artifacts.is_secret_like` is true (containing `API_KEY`, `APIKEY`, `TOKEN`, `SECRET`,
  `PASSWORD`, `PASSWD`, `CREDENTIAL`, `AUTH`, `COOKIE`, `SESSION_KEY`, `PRIVATE_KEY` or
  `ACCESS_KEY`) is removed, except `DEEPSEEK_API_KEY`, the credential DSH needs (user ruling
  2026-10-16). The launch record notes `agent environment: withheld_secret_like=<names>`. It is a
  denylist, not a sandbox: proxy variables pass, and a credential under another name or in a file
  still reaches the agent. Repository-locating Git variables, `DSH_PERMISSION_MODE`,
  `DSH_TOOLS_MODE` and `DSH_AGENTS_HOME` are removed in any letter case; `DSH_HOME` is set only to
  the bound value.

### Launch-surface record

For each invocation, just before the spawn gate, the driver records what DSH would read besides
the packet (`src/hflow/drivers/dsh_surfaces.py`). It is a record: nothing in it is enforced,
refused or bound.

- **Fields:** the DSH home (`bound`, or `per_invocation`: `<data-dir>/invocations/<id>/home/.dsh`,
  created empty, USERPROFILE/HOME pointed at it - inferred from upstream source) and its patch
  layers, `.env`, `AGENTS.md` and `skills`; the workspace's `.env`, instruction files and skill
  directories; the `DSH_*` names reaching the child, whether `DEEPSEEK_API_KEY` is among them, and
  the withheld secret-like names; `<child home>/.agents/AGENTS.md`; acpx, SDK and carrier versions;
  and the `sandbox` line (the mode from a bound home's patch layers, else `workspace-write`, with
  the #9626 warning under the Desktop shim).
- **How it is read:** fixed paths, never a listing. A regular file gets its size and SHA-256 (up to
  4 MiB); a `.env` and a bound home's `.credentials.yaml` are found by stat only, never opened or
  hashed. An entry that cannot be examined is `unknown`. Values are never recorded, nothing runs.
- **Carrier:** a Windows batch shim's text is classified as `desktop` (version from
  `primary-runtime/runtime.json`), `npm` (from its `@deepseek-ai/dsh` package.json), `unknown`, or
  `not_applicable` for an override argv.
- **Where it shows:** `prepare`'s `launch_surfaces`, `doctor --profile`, the per-attempt `launch`
  lines of `status`, and `report --json`'s `launch_surfaces` / `review_launch_surfaces`. The
  offline driver, an older row or a stop that won the gate shows "not recorded". DSH reads the
  files after the look.

## Model selection

| `model_selection` | Meaning |
|---|---|
| `"native_profile"` | no model flag; DSH's own profile decides |
| a token such as `"deepseek-v4-pro"` | 1-128 characters from `A-Za-z0-9._:/-`, not starting with `-` |
| `["deepseek-official", "deepseek-v4-pro"]` | a `[provider, model]` pair, stored compact |

Anything else is refused at profile load. A value other than `native_profile` is passed to the
acpx client as `--model <value>` before `exec` and is part of the effective-configuration digest
(a fixed flag under AGENTS.md rule 9; user ruling 2026-10-02). The pinned acpx sends
`session/set_config_option` before the prompt when the value is not already current and **fails
closed** on an unadvertised value (`FAILED model_rejected_before_prompt`). `status` prints one
`model` line per invocation:

| `model_applied` | Meaning |
|---|---|
| `not_passed` | no `--model` flag |
| `passed` | no change was needed and the last reported value is still the request |
| `accepted` | the change request succeeded and the effective value equals the request |
| `rejected` | the client's confirmed pre-prompt refusal, or a change request answered only with errors |
| `unknown` | anything the stream does not settle |

Keep profiles on `native_profile`: the capability is `documented` for real DSH, no live
`set_config_option` round trip has been observed, and which values DSH 0.2.0 advertises needs a
zero-prompt live check that needs its own approval. Changing a role's model changes the digest.

## Stopping a run

`hflow cancel <run-id>` records the intent first, decides from the run's state as read after it,
asks the driver and reports what happened:

| Receipt | Meaning |
|---|---|
| `confirmed_stopped`, `mechanism=forced` | the managed process boundary (a Windows Job Object) was terminated: local execution stopped |
| `confirmed_stopped`, `mechanism=none` | nothing was left to stop |
| `still_running` / `unknown` | not confirmed: the run blocks `outcome_unknown`, nothing is re-dispatched |

- A **live run** gets its receipt and terminal block in one statement; a confirmed stop then
  finishes the attempt and closes the invocation's ledger entry. A bookkeeping failure after that
  is a `dispatch:` note on an already-terminal run (`resume` closes such an entry).
- A **run that already ended** keeps its outcome; its last invocation is still asked to stop and
  the receipt says `run_already_ended: true`. Such an `unknown` answer does not make `clean`
  refuse. An `ACCEPTED` run is never reopened.
- **`hflow cancel` from another shell** holds no handle to the controller's child: it records the
  intent and an `unknown` receipt, the live run blocks `outcome_unknown`, and `resume` reconciles
  once that controller is provably gone (until then it writes nothing and exits `5`). A cancel that
  lands during the checks or the review handoff leaves no open entry, so the root accepts a new
  revision at once even while the original controller finishes.
- The stop goes to the **role that is running**, recorded as a `cancel_target` run note. Every
  registration and block is conditional on "no stop recorded", and the driver creates a child only
  inside a gate both stop paths take: a stop that wins the gate means no process is created; one
  that loses finds the published handle and terminates it.
- A client that exited is not an empty boundary: the stop asks the Job. A live stop is confirmed
  only when the Job is empty and the client is shown to have exited; a process Windows will not
  open (`ERROR_ACCESS_DENIED`) is unanswered, never gone. Off Windows the boundary is
  `direct_child_only` and a forced stop does not kill even the direct child.
- After a stop, a result of either role is a fact, not a decision: it is kept only as a
  `late_result` note, creates no candidate ref or verdict, and settles no ledger entry.

A confirmed stop is not a rollback, not a protocol cancellation (unsupported on this launcher:
HFlow starts the client in a new process group, which disables Ctrl+C there, and acpx does not
handle Ctrl+Break; M0 observed a CTRL_BREAK kill the client before any cancel reached the agent),
and says nothing about a remote request or bill.

## Recovering from a controller crash

The durable facts are the `runs`, `attempts` and `invocations` rows. A run is owned by one
controller **process** (user ruling 2026-10-03): a random token, its pid, creation time
(`GetProcessTimes`) and host, recorded with the run in the same insert, plus an exclusive OS file
lock on `<ledger dir>/owners/<token>.lock` held for the process's lifetime. `status`/`report` print
the owner and a read-only `liveness` probe that is never stored. A second `hflow run` of the same
TaskSpec while that owner holds it returns the run as it stands with a note naming the owner (exit
`5`; no setup, no block).

- **Interrupted** (Ctrl+C, `SystemExit`) while an invocation was starting or running: the driver
  waits in slices of at most 0.1 s, so the interrupt arrives at once and the invocation's boundary
  is terminated (best effort, not recorded as a stop). The controller then records, best effort,
  the block `outcome_unknown`, the open entries as `unknown`/`launch_unknown`, and the attempt as
  unknown; the command exits `130`. Once that process has exited, `hflow resume <run-id>`
  reconciles (records `reconcile_json`, never re-dispatches); while it still runs `resume` writes
  nothing and exits `5`.
- **Killed hard** (process kill, power loss, an interrupt during checks or the freeze): nothing is
  recorded at the time; the run stays `RUNNING` with its owner (token, pid, creation time, host,
  claim generation). A Ctrl+C during a check reaches the controller within about 0.1 s and
  terminates the check's Job; no evidence row is written for it. `hflow resume` takes the run over
  **only** when the owner is proven gone - its lock can be taken and its pid plus creation time
  name no running process (exited, no such pid, or a reused pid) - and blocks it `owner_lost`
  (exit `3`) in one transaction: open entries become `unknown`/`launch_unknown`, live attempts
  end unknown, the claim generation increments so the old owner's writes fail, and each recorded
  child pid is probed and recorded ("may still be running; nothing was stopped" or "reads gone").
  A held lock or an owner reading `matching`/`unknown` changes nothing (exit `5`). There is no
  lease timeout.
- After a takeover the old owner's dispatch reservation, result application, check transition,
  repair reopen and acceptance carry its token and generation and are refused (`owner_lost`); its
  launch registration and review attachment write nothing; acceptance also requires the receipt to
  name this run, its current attempt and its revision. Writes other than these fenced ones (notes,
  evidence rows, spawn reports) are guarded only by the run's state and ledger rules, which the
  takeover set to `BLOCKED`/`unknown`.
- A **pre-v6 run** has no token. One with a controller pid (no host) reads `unknown` and is never
  taken over: `hflow cancel` (which blocks it `outcome_unknown`) followed by `hflow resume` is the
  way out. One that recorded no controller process reads `not_recorded` (not proof of death) and is
  taken over ("no owner was recorded") only when it has no attempt and no invocation - wrong only
  for a pre-v6 controller still in setup on a ledger migrated under it.
- An `hflow run` interrupted in setup (no attempt, no invocation) needs no `resume`: once its owner
  is gone, resubmitting the same TaskSpec adopts it in one compare-and-set (token and generation
  replaced, the run kept `DRAFT`/`READY`, a note "adopted from a controller that is provably
  gone"), and only under its original admission binding (project root, Git directory, workspace
  path, base, contract, configuration and launch content, drivers, deadline, root). A missing,
  unreadable or changed binding returns the run unchanged with exit `5`; restore the inputs, or
  cancel it and submit a new revision.

**What the owner lease does not cover.** File locks on network shares or cloud-synced folders (a
ledger under a OneDrive-synced Desktop, say) may not exclude anything, and such a location is not
detected. An owner running elevated or as another user, on another host, or off Windows (no
creation time is read there; documented, not observed) reads `unknown`: its run is never taken
over and its `outcome_unknown` block is never reconciled. A hard-killed owner's lock is released
promptly but not instantly, so a takeover right after a kill can be refused once (run `resume`
again); sleep or hibernation keeps the owner alive. A takeover stops nothing - a child that "may
still be running" runs until it exits or you end it - and no takeover is automatic. No writer that
sets `BLOCKED` relabels a run that already ended or carries a receipt, and a controller never
records its own refusal as the block of a run another controller reserved. The spawn coordination
behind a stop covers the one driver that creates processes (`AcpxDshDriver`).

`cancel` and `resume` build an observer per role from the run's recorded configuration, so their
records name the recorded driver, never the offline fake.

## What is not safe yet

What this build does not do is listed once, in the README's "Not implemented" section. In
operating terms, and what is not verified even where something works on one binding:

- **Scope is detected after the fact**: a worker or check that writes outside the scope has already
  written. A managed worktree is a workspace boundary, not a permission boundary, and an in-place
  run edits the project root directly. The reviewer is a separate process and invocation but runs
  with `isolation=prompt_only`: a prompt-level instruction, not an enforced read-only boundary.
- **Authorization is trusted-local**: provenance is not authenticated and a fresh id resets the
  allowance.
- **Stopping** covers what the Windows Job Object owns: not a descendant that leaves it, not
  another platform, and the access-denied answer that keeps a stop `unknown` has only been
  injected in offline tests. A cross-process `cancel` cannot stop a child at all.
- **Shared Git metadata is compared, not confined**, and in offline tests only (never against a
  real DSH worker). The comparison is point-in-time: a process running as you can change the
  metadata between a comparison and HFlow's next git command. Nothing is restored and there is no
  cross-run baseline: a later or concurrent run takes the changed state as its baseline. False
  positives fail closed - your own `git config`, `push -u`, an IDE writing `branch.*`, a check
  running `git config` - each costing a new revision (under a root, one charged as a repair). Not
  compared: ignore rules, system attributes (`GIT_ATTR_SYSTEM`; they can only select a filter the
  compared config defines), hooks (neutralised anyway), object and ref metadata. A FIFO, device or
  directory in a metadata path is recorded by type, never read. In-place runs make no git call.
- **Your checkout is compared by stat, not content.** An accepted worktree run compares HEAD, the
  stash ref, the index entries and, for every `git status --ignored` entry, its path, status, size
  and mtime, before dispatch and after acceptance; a difference records a WARNING, and the "left
  untouched" note is written only without one. A run that blocks or stops before acceptance records
  no after-comparison. A rewrite that keeps size and mtime, or a change inside a directory git
  reports collapsed, is not detected.
- **Billed usage stays unknown.** An agent-reported `usage` (UNSTABLE in ACP, disputed in ACP
  #1860) or `usage_update.cost` never fills `provider_billed_tokens`, `provider_cost` or
  `subscription_quota_remaining`; DSH 0.2.0-rc.2's source sends neither, and the recorded live
  streams carry neither. Do not read `null` as `0`.
- **Model selection** is exercised with the pinned acpx against a mock agent only.
- **Message boundaries without `messageId` are inferred**: a non-chunk update or a sequence gap
  ends a message and only the last segment is the answer. Every chunk in the recorded live DSH
  streams carries a `messageId`, so no recorded run took this path; DSH's "text, reasoning, text"
  order inside one message is handled in offline tests only.
- **What DSH does on its own** (above) is documented upstream and recorded, not observed or
  controlled; whether DSH expands a leading `~` in `DSH_HOME` is not verified (HFlow refuses one).

## Running the tests

```sh
python -m pytest -q             # add -n 8 with pytest-xdist; -n 16 exhausted the Windows page file (WinError 1455)
python -m pip install -e ".[dev]"
```

Tests need no model, network or DSH: the offline fake driver, a stand-in for the acpx client
(`tests/fixtures/fake_acpx_client.py`) and stub ACP agents that are real separate processes
(`tests/fixtures/stub_acp_agent.py`), one of which ignores cancellation and holds a helper child,
so a decorative process boundary fails. `tests/conftest.py` points `HFLOW_DATA_DIR` at a per-test
temp directory (and clears `HFLOW_PROFILE`), puts a stand-in `dsh` first on `PATH` (an npm-style
`dsh.CMD` on Windows) that only exits 1 and is never run, and sets a stand-in `DEEPSEEK_API_KEY`
that replaces any real one; the tests of `no_credential_source` remove it. Test knobs: the client
stand-in mirrors `--model` (`STUB_MODEL_CATALOG`, `STUB_CLIENT_FAIL_BEFORE_PROMPT`); the stub agent
can answer with a JSON-RPC error or a reused id (`STUB_TERMINAL_RESPONSES=2:!<code>` /
`2:?<method>`), without a stop reason (`2:v2:<stopReason>`), send updates after its response
(`STUB_TRAILING_UPDATES`, `STUB_REVIEWER_TRAILING_UPDATES`) and report usage
(`STUB_REPORTED_USAGE`); the mock agent has `dsh-catalog`, `prompt-error` and `trailing-update`
scenarios. Some tests skip when a precondition is absent (a directory-link right, `node` plus the
pinned acpx at `.probe/acpx`, the recorded M2 ledger, Windows, Git 2.48+, a non-single-byte locale)
and say so.

Git: 2.31+ for worktree runs, 2.40+ for `hflow integrate`. The global attributes file is located
with `git var GIT_ATTR_GLOBAL` on Git 2.42+, else from `core.attributesFile`, else
`$XDG_CONFIG_HOME/git/attributes`, else `~/.config/git/attributes`. One tool writes a tracked
file: `tools/m0_probe/write_results_doc.py` regenerates `docs/m0-results.md`.

## Appendix: writing the artifact by hand

The plan flow writes the authorization for you. A scripted flow that cannot use a terminal writes
it by hand: `hflow schema` prints its shape (`AuthorizationRecord`, next to `RootBudgetPlan`,
`RepairPolicy` and the rest). Copy `authorization.binding` from `hflow prepare --json` whole -
every field it prints, `ledger_path` and, for a root run, `root_budget` included - and add your own
text:

```json
{
  "schema_version": 1,
  "authorization_id": "AUTH-<unique>",
  "provided_by": "user",
  "user_text": "<your approval, verbatim>",
  "authorized_at": "<UTC time>",
  "max_top_level_submissions": 2,
  "binding": { "...": "authorization.binding from hflow prepare --json, unchanged" },
  "root_limits": { "...": "root_budget.limits from prepare, for a root run only" }
}
```

`max_top_level_submissions` is at most 4 and must cover
`authorization.max_top_level_submissions_required`. Leaving out a field `prepare` printed does not
narrow the approval, it breaks it. Then run with every resolution flag `prepare` was given and
`--authorization-file <file>` (`hflow run --task ... --authorization-file auth.json`), or prepare a
plan with the same inputs and use `run --plan P --authorization-file <file>`.

The synthetic live-acceptance package (`python tools/m2_live/prepare_m2_live.py --out
.probe/m2-live`) builds a project with two real defects, its contract and task, and prints an
`hflow prepare` command for them; add `--allow-writes --plan-out <plan>` to it. Its history and
recorded results:
`docs/history/HFlow_M2_Controlled_Live_Acceptance.md` and `docs/m2-live-acceptance-result.md`.
