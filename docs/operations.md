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
hflow resume <run_id>                      # never re-dispatches: reconciles a blocked run, takes a live run over from a provably gone owner (blocks it owner_lost), or closes an ended run's open ledger entries once its owner is gone
hflow cancel <run_id>
```

`run` prints a JSON payload that includes the receipt. Use `--receipt-out <path>` to
write only the receipt, and check the exit code: `0` accepted, `2` refused at admission,
`3` blocked after dispatch, `4` usage (any argument-parsing error), `5` the run exists and is
not finished (`DRAFT`/`READY`/`RUNNING`/`CHECKING`: a submission that found the run claimed by
another owner process, `resume` refusing a takeover because that owner may be alive, or a
never-dispatched run whose admission binding is missing, unreadable or changed), `6`
`status`/`report` found a stored record that no longer validates (`StoredRecordUnreadable`). The
error names the record; nothing substitutes guessed data for it. `cancel`/`resume` can still work
from the run row without those presentation records.

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

`model_selection: "native_profile"` passes no model flag: the DSH launcher's own profile decides
the model. Any other value is passed to the acpx client as `--model <value>` - see "Model
selection" below for the accepted values, what acpx does with the flag, and why concrete values
for real DSH are not chosen yet. `hflow doctor --profile dsh-local` prints the resolved launch
per role - the agent argv and either the exact `--model` flag or "no --model flag" - so nothing
about the launch has to be assumed. `hflow doctor` (with or without `--profile`) also names the
DSH home a child uses (`child_dsh_home`), which is not the `dsh_home` your own shell's `dsh` uses
unless `DSH_HOME` is set.

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
| a `model_selection` that is not `native_profile`, one token, or a two-token pair (empty, spaces, quotes, a leading `-`) | refused (`invalid_spec`, naming `model_selection`) at profile load, so by `prepare`, `run` and `doctor` alike |

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
| `effective_config` | which profile, which agent/driver per role, the **resolved launch** (client entry point and interpreter, agent argv, the `model` passed as `--model` when one is set, DSH home/profile), which write permission, and a `digest` |
| `admission` | every problem with the task's definition that `run` would refuse on |
| `dispatch_preconditions` | every problem with running it *now* that `run`'s dispatch gate would refuse on - the repair-policy rules included, for offline runs too |
| `write_allow` / `write_deny` | the scope the worker will be told (the project's deny list and the built-in deny list are enforced too, but not listed to the worker) |
| `checks` | the approved checks this task's acceptance needs, and which criterion needs each |
| `budget` | implementer + reviewer turns required, against the task budget and project ceiling. With a repair policy `required_turns` is the **worst case** (I1 + R1 + I2 + R2) and `repair_cycles` is 1 |
| `root_budget` | with `--root-budget-file`: the root binding, its ceilings, the normal dispatch count (`single_loop_dispatches`) next to the worst case the gates must cover (`required_top_level_submissions`), the repair switch and the deadline. `null` without the flag (a legacy-shaped run) |
| `repair_plan` | always present: `enabled`, the policy digest, the declared check exit codes, whether reviewer changes count, the enabled triggers, `single_loop_dispatches` and `worst_case_dispatches`. `enabled=false` is a stated fact, not a missing field |
| `packet_preview` | the implementer's input packet, byte length and digest. It states the configured deadline; a run under a root whose `deadline_seconds` is lower states, and sends, the capped one |
| `authorization` | the binding an approval would have to cover, and nothing that is one. Its `base_commit` is the SHA the task's base resolved to now, not the name written in the task |

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
| a `repair_policy` on a task whose `workspace.mode` is not `worktree` (`scope_violation`) | shared precondition, offline runs included |
| a `repair_policy` on a task that will not be reviewed (`not_implemented`) | shared precondition, offline runs included |
| a `repair_policy` whose worst-case loop (I1 + R1 + I2 + R2) exceeds `budget.max_agent_turns` (`budget_exceeded`) | shared precondition, offline runs included |
| a `repair_policy` while a role uses a real transport and no root is bound: **a real-driver repair needs `--root-budget-file`** (`budget_exceeded`, location `root_budget`). A fully offline fake run may still repair rootless | shared precondition |
| the task declares write paths but `workspace.mode` is not `worktree` | shared precondition, real driver |
| the task declares write paths but `HFLOW_ALLOW_WRITES` is not enabled | shared precondition, real driver |
| a required review, but `budget.max_agent_turns` is 1 | shared precondition, real driver |
| a role's launch could not be resolved: no acpx client on this machine; no `dsh` (or no `node`/`python` the client needs) on an absolute `PATH` entry - there is no bare-name fallback; an explicitly named program that is not an absolute path; a launcher, `dsh`, client interpreter or acpx client entry inside the project root or the worktree directory (`<repo>.hflow-worktrees`), or one of those directories inside the `node_modules` tree the client entry loads its modules from; or no `SystemRoot` to find the absolute `cmd.exe` the DSH batch shim needs | shared precondition, real driver |
| the workspace the run starts in already holds `.acpxrc.json`, in any letter case, at its root - the project root for an in-place run, the base commit's tree for a worktree run (`workspace_client_config`, location `workspace`; the detail names the spelling found) | shared precondition, real driver; refused before a run row, an authorization record, a reservation or a process exists |
| the workspace the run starts in already holds `.env`, in any letter case, at its root - the project root for an in-place run, the base commit's tree for a worktree run (`workspace_env_file`, location `workspace`; found by a listing or `ls-tree`, never opened). Remedy: remove or rename it; for a worktree run commit its removal (a real run then needs an authorization for the new base) | shared precondition, real driver; refused before a run row, an authorization record, a reservation or a process exists |
| a role's launch binds a `DSH_HOME` that is relative (a leading `~` included), cannot be resolved, or - as written or with links resolved, case-folded - equals or lies inside the project root or the worktree directory (`<repo>.hflow-worktrees`), or contains one of them (`dsh_home_in_workspace`, location `launch.dsh_home`). Remedy: point `DSH_HOME` at an absolute directory outside them, or unset it (the per-invocation home is unaffected), then prepare again - the launch changes, so a real run needs a new authorization | shared precondition, real driver; refused before anything exists |
| a real delivery whose approved checks are `kind=fake` | admission |
| a `write_allow` entry containing `*`, `?` or `[` (only literal file or directory paths are accepted; `write_deny` keeps its globs), a `write_allow` entry under the built-in deny list (`.git`, `.hflow`, `.acpxrc.json`), or a `write_allow` entry that is, or passes through, a symbolic link or junction in the checkout (a link can name a different place in the run's worktree) | admission (`scope_violation`) |
| unknown check, scope violation, risk below the project floor, unmet delivery level, reuse not decided, budget above the project ceiling | admission |
| an invalid `model_selection` in the profile | profile load (`invalid_spec`) |
| not enough authorization allowance left for the whole fixed loop | controller, before a run row, a root row or an authorization record exists (`budget_exhausted`); for an admitted run that never dispatched, before it continues - read-only and before this process claims or adopts the run, so the run is left exactly as it was and a new authorization for the same TaskSpec and unchanged admission binding, from any later process, can continue it |
| a root with a policy armed whose `max_repairs` cannot cover the repair this run may buy (`used_repairs + needed > max_repairs`, where `needed` is 2 when an earlier run already dispatched an implementer on the root) | controller, before a run row, a root row, an authorization record or a dispatch exists (`budget_exhausted`), so resubmitting with a root file whose `max_repairs` covers it works; `prepare` reports `max_repairs` 0 with a policy armed under `dispatch_preconditions` (location `root_budget`) - the ledger-dependent part only the run sees. For an admitted run that never dispatched (unclaimed, this controller's, or adopted from a provably gone owner) the counter can never be raised, so the run ends `BLOCKED` `budget_exhausted` instead of staying `DRAFT` |
| a run without `--root-budget-file` for a task that already has a root in this ledger | controller, before a run row or an authorization record exists (`budget_exhausted`, "pass --root-budget-file"), so resubmitting the same TaskSpec with its root and a covering authorization dispatches; `prepare` does not open the ledger and cannot see it. The dispatch transaction repeats the check as a race backstop: a root registered after the run was admitted blocks that run (no driver starts, no counter moves), and since an identical resubmission only returns it, that case needs a new revision |

The `shared precondition` rows are the ones `prepare` reports under `dispatch_preconditions`, and
they are the same list the run's own gate raises - one function (`predictable_dispatch_problems`),
two callers. The repair-policy rows are facts about the task, so they are reported for offline
runs too; the others are facts about this machine and are reported only for a real driver. A
policy problem therefore shows up under `dispatch_preconditions` while `admission` still reads
`ok` - look in both.

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
  the approval digest, because all of them change which program would run. The launch programs
  (`dsh`, `node`, `python`, the agent launcher) are bound as absolute paths: only absolute `PATH`
  entries are searched, a missing program makes the launch not resolvable rather than falling
  back to a bare name, and an explicit program (`HFLOW_ACPX_NODE`, for one) must be absolute.
- The launch is resolved **once**, before the approval, and then consumed by the driver. Nothing
  re-reads the environment after the check, so a variable changed mid-run cannot swap the client.
- `DSH_HOME` is part of the launch in both directions: a bound value is set on the child, and
  *absence is bound too* - when the resolution found no DSH home, the variable is removed from
  the child environment rather than inherited, including any value passed in through the
  driver's `extra_env`. A `DSH_HOME` that appears after the resolution therefore cannot reach
  the process. With none bound, DSH's home is the per-invocation
  `<data-dir>/invocations/<id>/home/.dsh`, created empty (inferred from upstream source, not
  observed): the child's USERPROFILE/HOME point at the invocation's own home.
- The launch-surface record (what DSH reads on its own: see "What DSH reads on its own" below)
  and the client and carrier versions are **not** part of the binding. A changed AGENTS.md or
  `.env` does not change the digest; the launch entry files are bound by content (below).
- An artifact written before config binding existed still loads, still lists, and still keys its
  own single-use ledger row - but it cannot authorize a run that resolved a configuration,
  because nothing in it says which one. Re-issue it.
- The digest of an old artifact is unchanged by this build: the new fields are omitted while
  they are empty, so an already-consumed approval cannot look unused again.
- **The project contract is bound too** (`project_contract_digest`, printed by `prepare` as
  `contract`): every check with its argv and timeout, `write_deny`, the limits, the review floor
  and `review_required`. Editing `.hflow/project.json` (or pointing `--project` at another file)
  after approval refuses the artifact ("the project contract changed since approval"); an
  artifact without the field cannot authorize a run that resolved a contract. Re-issue it.
- The binding's `roles` are the roles the run will actually dispatch (the reviewer only when the
  task needs a review) and `run` compares them, so a change that drops the reviewer is refused.
- **Launch entry files are bound by content; transitive modules and anything Node loads later
  remain bound by path** (`launch_content_digest`, user ruling 2026-10-03). The list is fixed and
  short: the client interpreter (`node.exe`), the acpx entry file and its package's
  `package.json`, the dsh launcher the agent argv starts, and for a Desktop or npm carrier the
  carrier entry its shim runs (the one `%~dp0`/`%dp0%`-relative `.js` path in the shim) and that
  file's `package.json`. For the installed Desktop carrier that script
  (`..\..\..\app.asar\dsh\node_modules\@deepseek-ai\dsh-desktop-host\lib\cli.js`) and its
  `package.json` exist only inside `resources\app.asar`, a regular Electron archive file: when the
  entry's path passes through a regular file named `*.asar`, the archive file itself is bound
  (`carrier_archive`, streamed SHA-256; ~121 MB, about 0.1 s warm on the development machine) in
  place of the entry and its `package.json`, with the note "the carrier entry inside <archive> is
  bound through the archive's digest". The archive is not parsed. An entry that also exists under
  `app.asar.unpacked` is refused (which copy Electron runs is in the archive header), as is a shim
  whose entry exists neither as a file nor inside such an archive. `prepare` prints each file's kind, SHA-256, size and final path under its
  role's launch plus the combined `launch_content_digest`; `doctor --profile <id>` prints them as
  `launch content:` lines. Replacing one of those files at the same path refuses the artifact
  (`launch_content_changed`); an artifact without the field cannot authorize a run whose launch
  resolved digests (re-issue it). The digests are left out of `config_hash`, so the refusal names
  the changed file rather than "a different configuration".
- The driver hashes the same list again inside the spawn gate, after the `.acpxrc.json` check and
  just before `CreateProcess`, and refuses a changed byte, a changed size, or a path that now
  resolves elsewhere (`launch_content_changed`, spawn reported as not created, the run blocked;
  `prepare` again and re-issue). Hashing `node.exe` (~90 MB) costs about 60 ms warm.
- What this does not cover: `node_modules` trees and everything Node resolves at runtime, DSH's
  own code loading, the Desktop carrier's `DeepSeek Harness.exe` and `app.asar.unpacked` tree,
  the npm shim's `node`,
  `cmd.exe`, and whatever an unclassified dsh launcher starts (the launcher file itself is bound).
  Windows has no exec-by-handle: a file swapped between the spawn gate's hash and process creation
  is not caught. Each file's final path is resolved once (links and junctions followed once), and
  that path is both hashed and started, so a link cannot point the hash at one file and the spawn
  at another. A missing or unreadable file, a Node entry outside any `node_modules` package, or a
  Desktop/npm shim whose carrier entry cannot be named makes the launch not resolvable.
- **The base commit is bound as a SHA.** `workspace.base_commit` (or `--base-commit`) may name a
  ref such as `HEAD` or `main`; `prepare` and `run` resolve it once to a 40-hex commit, and the
  binding's `base_commit` is that SHA, not the name. The task text and its `spec_digest` do not
  change. An approval written while `main` pointed at one commit stops applying once `main`
  moves (a `base_commit` mismatch), and an artifact that carries a symbolic `base_commit` no
  longer matches at all - re-issue it from a fresh `prepare`. The run records the resolution as a
  note (`base 'main' -> <sha>`), uses that one commit for the worktree, every round and the
  receipt, and a branch that moves during the run changes neither the base nor the delivery.
- **Artifacts issued before the 2026-10-02 launch hardening must be re-issued.** The DSH batch
  shim is now wrapped in the absolute `%SystemRoot%\System32\cmd.exe` instead of a bare `cmd.exe`,
  which changes the bound agent argv for a `.cmd` launcher, and a profile whose `model_selection`
  is not `native_profile` now binds the `--model` value it really passes. Both change the
  effective-configuration digest. A `native_profile` launch passes no flag and digests exactly as
  before the model change, but the absolute `cmd.exe` still changes its digest whenever the DSH
  launcher is a `.cmd`/`.bat` shim - which it is on Windows. On a machine where an earlier build
  bound `dsh`, `node` or `python` as a bare or relative name, the launch now binds the absolute
  path (or is not resolvable at all), so those digests change as well.

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
| `max_repairs` | how many *additional* implementer attempts the root may buy. The root's first implementer attempt is not a repair; every later one is, including a later revision's first implementer. **A ceiling, not a switch**: the dispatch transaction enforces it, and any later implementer on the root spends it whatever its `repair_policy`; only a task's explicit `repair_policy` arms the in-run repair round (see "When a repair may happen"). A later revision on a root with no repair left is refused `budget_exhausted` before its run row or authorization record exists (a policy-less one on a root that also has an unresolved invocation is instead blocked by the dispatch transaction's "unresolved invocation" refusal, as before). With the default 0 a root file used with a repair policy is refused before the first dispatch, and before the root is registered: set at least 1, and at least 2 for a later revision that wants its own repair, and resubmit (`prepare` reports the 0 case) | 0..8, default 0 |
| `deadline_seconds` | wall-clock ceiling measured from the root's **first successful reservation**, recorded as `deadline_at` and never reset by a new run or revision (before that first reservation there is no clock yet, and a role gets the smaller of its configured value and this `deadline_seconds`, since the clock will start with exactly that much - "no clock" is not "no time left"). What it caps: a dispatch past it is refused; each role's invocation deadline (the implementer's configured value, the reviewer's 900 s) is the smaller of its own value and what the root has left, and the driver's local wait and forced stop follow that deadline; the checks share **one** countdown of the remaining time, so each check runs for at most what is actually left and a check with under a second left is not started (recorded as a `timed_out` error); the repair attempt is given the same capped deadline; and acceptance re-checks the clock, so a candidate finished after the deadline is not accepted (`budget_exhausted`). It does not stop a remote model request or a remote bill | >= 60, default 86400 |
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
  reviewer), the repair switch and the deadline, and prints the same root inside the pending
  authorization binding, so the digest a user approves is the digest the run checks. The repair
  switch is the **task's own `repair_policy`**, not the root file's `max_repairs` (see "When a
  repair may happen"). `prepare` creates nothing.
- One transaction reserves a dispatch: ownership, role/phase, root ownership and allowance,
  authorization allowance, the attempt row, the invocation row and every counter commit together
  or not at all. The deadline and the counters are read and written inside it, so two controllers
  cannot both spend the last submission. Replaying the same invocation id returns the recorded
  reservation and charges nothing, and the caller may then only coordinate it - a retried call
  cannot start a second process.
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
  `launch_unknown` - blocks every later dispatch of that root, **including under a new revision
  and including a run started without `--root-budget-file`**: a rootless run of a task that
  already has a root in this ledger (same project, repository and task) is refused with
  `budget_exhausted`, naming the root - by the controller before its run row exists, and again in
  the dispatch transaction as a race backstop. An unresolved invocation is never
  refunded, retried or re-dispatched; `resume` records an observation and settles nothing. An
  `unknown` or `launch_unknown` entry of a run that has ended is closed only by
  `hflow ledger settle` (next item), an operator attestation; until then the root stays blocked.
  An entry left **open** on a run that ended for another reason - a confirmed stop whose ledger
  write failed (`cancelled_by_operator`), a driver that raised without reporting a spawn fact
  (`internal_error`, or `review_protocol_error` for the reviewer), a settlement write that failed
  on a run that then ended (`ACCEPTED` included) - is refused by `hflow ledger settle`, so
  `hflow resume <run_id>` closes it first (batch I1). It acts only once the run's owner is
  provably gone, by the owner half of the settle rule below (a run with no owner token passes
  only when it recorded no controller process: `resume` takes no attestation, so a pre-v6 run
  with a recorded controller pid is refused and its entry stays open); otherwise it writes
  nothing and says why. An entry named by the run's own recorded confirmed stop - the stop that
  ended the run, not one that reached a run already ended - is closed from that stop fact, as the
  stop would have closed it (`started` -> `settled`/cancelled, requested -> `launch_unknown`,
  never requested -> `not_started`); every other open entry becomes `unknown` (a launch was
  recorded) or `launch_unknown` (no launch recorded), with a `dispatch: resume closed ...` note. No
  driver is called, nothing is dispatched or refunded, and the run's state, block code, receipt
  and outcome stay as they were; a second `resume` is a no-op. `status`/`report` show one
  `open entries` line naming `hflow resume <run_id>` while such entries remain, and the run's
  failure note names the same two steps.
- `hflow ledger settle <invocation_id> --as consumed|void --attest "<text>"` (ruling
  2026-10-03; storage version 7) moves one `unknown` or `launch_unknown` entry of a run that has
  ended to `operator_settled` and appends a row to `invocation_settlements` (prior state, choice,
  `basis = operator_attested`, the OS user name - recorded, not authenticated - the UTC time and
  the attestation, 1-2000 characters). `consumed` (default) keeps every counter spent. `void` is
  refused for `unknown` and, for `launch_unknown`, returns that dispatch's root charge (one
  top-level submission, plus one repair if it was charged as the repair; a voided implementer
  entry is also no longer counted as the root's first attempt). The authorization counter and
  the run's turns are not returned. Both the controller's advance repair-allowance check and
  the transactional dispatch charge use that same effective implementer history: a voided
  first implementer does not make the next revision's first implementer a repair; a `consumed`
  entry still does. Resolve-once; open or final entries, a run that has not
  ended and a blank or oversized attestation are refused (exit 2); an unknown id exits 4. The
  run keeps its state, block code and outcome, nothing is re-dispatched, and the entry renders
  as "settled by operator attestation (not observed)" in `status`/`report` (and as counts in
  `doctor`); billed usage stays unknown. Settling unblocks the root for a new revision only - that
  revision still needs its own explicit approval.
  Before the store transaction the CLI also refuses (exit 2, nothing written) while anything
  recorded may still act: the run's owner must be provably gone by the takeover rule (owner lock
  free or absent **and** identity `gone`; a run with no owner token passes this half only when it
  recorded no controller process at all, or when you add `--legacy-owner-gone` to attest that the
  pre-v6 controller it recorded has exited - recorded with the settlement, never checked), and
  the entry's own child (`process_pid` +
  `process_started_at`, probed on the owner's recorded host) must read `gone` - `matching` or
  `unknown` refuses. Wait for the controller or child to exit (or stop its process tree
  yourself), run `hflow resume <run_id>`, then settle again. An entry with no recorded pid rests
  on the owner rule alone.
- A result or stop settlement only closes an **open** entry (`reserved`, `requested`,
  `started`). An entry that is already `not_started`, `settled`, `unknown` or `launch_unknown` keeps that state whatever a
  later result or stop reports; the refused settlement becomes a run note ("dispatch: invocation X
  was not settled as <outcome>: the ledger already records it as <state>, and that state
  stands"). A confirmed stop closes an open entry as: `started` -> `settled`/cancelled, never
  requested -> `not_started`, requested with no spawn report -> `launch_unknown`. A spawn report
  that arrives after an entry was closed as `launch_unknown` (by a reconcile or a confirmed stop)
  never reopens it: a reported launch records its process facts and the entry becomes `unknown`,
  a reported non-launch records `not_started` - the same states the closure would have recorded
  had the report come first, and neither is open, so no later result or stop can release the root.
  The ledger separates four facts: `requested` (a driver was asked), `started` (the launch
  happened), the process count (a driver reported a pid) and the settlement. `not_started` is
  recorded only on a driver's own word that nothing launched (its spawn report, a refusal before
  launch, or - for a driver that never reports - the stop-before-launch case in the next item), or
  when no driver was ever asked; a launch that was requested and never reported back is
  `launch_unknown`, because an empty timestamp is not evidence that nothing ran - a forced stop of
  a real child is evidence of the opposite.
- `status`/`report` read the ledger back: root id, used/limit submissions, repairs used/limit,
  deadline, and per-invocation role, launch state and spawn kind. The `processes` line counts
  reported operating-system children, so an offline run shows zero however many times it settled.
  A run recorded before E1 has no root row and says `legacy / not recorded` - absent facts, never
  zeros. A driver that never sends a spawn report is read from the row: completed work is recorded
  as a launch with `spawn=unknown` and no process, so it is counted as launched but never in
  `processes`; a cancelled return with no work is recorded `not_started` only when the stop was
  already recorded when the controller handed the invocation to the driver. Otherwise the entry
  stays `requested`: a confirmed stop then records `launch_unknown`, and an unconfirmed one leaves
  it open until `resume` records `launch_unknown`.
- Automatic repair exists only under an explicit policy and is bounded to one round: see "When a
  repair may happen" for the two triggers, what stops it, and why an undeclared exit code never
  repairs. E1's ledger and dispatch transaction are unchanged underneath it. A repair on a run
  whose roles use a real transport **needs `--root-budget-file`**; only a fully offline fake run
  may repair rootless.

## When a repair may happen (batch E2)

A repair is **one** second implementer attempt inside the same run, on the same revision, starting
from the frozen candidate, followed by fresh checks and a fresh independent review. It is never a
`hflow repair` command, it never revives a historical `BLOCKED` run, and it never retries an
environment or transport failure. The switch lives in the task, not in the root budget file:
`BudgetRequest.max_repair_cycles` keeps its historical default of `1` and **authorizes nothing**.

```json
"repair_policy": {
  "max_attempts": 1,
  "check_exit_codes": { "unit": [1], "lint": [1, 2] },
  "allow_reviewer_changes": true
}
```

| Field | Meaning |
|---|---|
| `max_attempts` | pinned to `1`. This build implements exactly one bounded repair per run; anything else is refused when the spec is loaded |
| `check_exit_codes` | the check ids that may trigger a repair, and the exit codes that mean *that check's business assertion failed*. A check id absent from this map never triggers one, whatever its code |
| `allow_reviewer_changes` | may a substantive `changes_requested` on the current candidate buy the repair? |

A policy that names no trigger at all, a check with an empty code list, and exit code `0` listed as
a failure are all refused - not defaulted. HFlow does not claim to read a root cause out of an
arbitrary process exit code, so "every non-zero code is repairable" is not an available policy.

**How the policy gets into a run.** Write it in the task file as `repair_policy`, or pass
`--repair-policy-file PATH` to `prepare` and `run` - the same bare `RepairPolicy` document, not a
task file and not an authorization. The flag is applied *before* admission and becomes part of the
effective TaskSpec, so the stored spec, `spec_digest` and the authorization binding all cover the
repair that was asked for. If the task file already names a policy and the file names a different
one, the run is refused rather than one silently winning; the same policy written twice (compared
by digest) is not a disagreement, so re-running a policied task with its own policy file works. A
missing file, a non-object document or a contract-invalid policy is refused **before the store is
opened**: no run row, no worktree and no authorization is created. Whether the *scope* can honour
the policy at all - an isolated worktree, a review, a run ceiling covering the worst case of four
top-level dispatches - is refused before anything is dispatched as a **dispatch precondition**:
`prepare` reports it under `dispatch_preconditions` (not `admission`), for offline runs as well.

**A real-driver repair needs a root.** When any role uses a real transport, a policy without
`--root-budget-file` is refused the same way (`budget_exceeded`, location `root_budget`), and
`run` refuses before a run row or an authorization record exists: the root's repair counter and
clock are what bound a task's repairs across revisions, and without them each new revision could
buy another. A fully offline fake-driver run may still perform its one repair without a root; that
repair is charged to no root counter. With a root bound, the root must also be able to afford the
repair before the first dispatch: `used_repairs + needed <= max_repairs`, where `needed` is 1, or 2
when an earlier run already dispatched an implementer on this root (a later revision's first
implementer is itself charged as a repair; a later revision without a policy needs 1 for that
reason alone). Otherwise the run is refused `budget_exhausted` before
anything exists - no run row, no authorization record and no root row, so a corrected root file
is not refused later as a different ceiling - and if the counter is found spent at the decision itself a `budget_exhausted`
repair record is written instead of `allowed`.

**A policy only classifies a failure it can see.** A check id the project contract does not run
never produces a failed row, so that entry cannot buy anything however it is written; and when a
check does fail whose id or exit code the policy does not declare, `business_failure_for` returns
false and the run records a `not_a_business_failure` decision naming the check, its exit code and
what the policy declared for it (`declared: nothing` for an id the policy does not list). The
decision is stored before the run blocks, so an operator can read exactly which check failed and
why the policy did not cover it.

**What a check failure must look like to trigger a repair.** All of these, from the *current*
attempt and the *current* candidate only (a previous round's evidence is part of the same run and
can never describe this one):

1. the check's id is declared in `check_exit_codes`;
2. its recorded exit code is listed for that id - an **undeclared exit code never repairs**,
   however non-zero it is, and neither does a row with no exit code at all;
3. its recorded `exit_reason` says the check *ran to completion*: `completed` or `nonzero_exit`
   (`verify.CLEAN_EXIT_REASONS`). Everything else `verify.py` writes is ineligible: `timed_out`,
   `settlement_forced` (a check that left descendants), `settlement_unknown` (the boundary could
   not be observed), `output_capture_error` (an incomplete capture), `not_launched` (nothing
   ran - every `kind=fake` check and every kind this build cannot execute), `boundary` and
   `startup` (the check could not be launched inside its boundary), `empty_argv`,
   `no_artifact_dir`, and an **empty reason** - every evidence row written before storage v5.
   The reason is a structured fact stored in its own column; it is never back-filled from an
   exit code and never guessed from the `verification_failed` text or a log keyword;
4. the round contains **no ERROR row at all**. A mixed round - one declared business failure plus
   one check that timed out, or a review that was malformed - stops the run without buying a
   second implementer.

**What a reviewer rejection must look like.** The verdict must be a valid `changes_requested` on
the current candidate, the policy must set `allow_reviewer_changes`, and it must carry at least
one finding. Findings are typed (`contracts.Finding`; user ruling, 2026-10-03):

| key | required | value |
|---|---|---|
| `body` | yes | non-blank string: what is wrong, why, and the input or scenario that triggers it |
| `title` | no | string |
| `location` | no | `{path: non-blank string, line_start?: integer >= 1, line_end?: integer >= line_start}`; `line_end` needs `line_start` |
| `severity` | no | `"P0"`, `"P1"`, `"P2"` or `"P3"` - recorded and rendered, gates nothing |
| `id` | no | string |

No other key is accepted, an optional key is omitted rather than `null`, and nothing is coerced
(`"3"` or `true` is not a line). Because `body` is required and never blank, every valid finding
is usable. `changes_requested` with `[]` records `no_findings` and stops the run: there is no
target to repair, and HFlow does not guess one. A finding that breaks the schema - `{}`, a blank
body, an unknown key or severity, a bad line range, the old untyped shapes - makes the whole answer
invalid: a wire failure (`review_protocol_error`), like any malformed verdict, and an unbound one
is an unknown outcome; neither is a rejection, and neither repairs anything. The reviewer packet's
output contract lists the same keys and rules as the schema embedded under it. Review evidence
recorded before typed findings keeps its untyped findings; `status`/`report` read it as stored,
and it never buys a repair.

**What the repair attempt is told.** The repair packet carries the original goal, criteria and
scope, the original base, the previous candidate, the failed checks with their current evidence,
and the findings. Each finding is rendered with its title, location, severity and body (an
absent key is printed as `(not given)`; reviewer text is JSON-quoted so its newlines cannot open a
packet section), followed by a note that severity filtered nothing; a value longer than 2048 UTF-8
bytes is cut on a character boundary and marked `…[truncated N bytes]`. Findings
are never dropped to fit: a findings section that pushes the packet past 32 KiB blocks the run
before the repair is reserved or dispatched. The previous candidate's paths, on the other hand,
are capped like the reviewer's lists - 20 entries plus a `(+N more)` count - followed by a
`full path list: git diff --no-renames --name-only <original base> <previous candidate>`
reference (`--no-renames`, like every path list HFlow records, so a move names its deleted source
too), so a wide first round cannot push the repair packet past its bound. The packet's remaining
deadline is the deadline the repair attempt actually has - the configured value, capped by the
root's remaining clock when a root is bound - never `0` for a clock that does not exist.

**What happens after the repair.** The repair round is its own `attempts` row (`is_repair = 1`,
same run and revision; the schema allows exactly one), and the reviewer for that round attaches to
the run's *current* attempt, so round two's verdict can never be recorded against round one's
candidate. That reviewer is shown the candidate as it would be delivered: base = the task's
original base, "paths changed from the base commit" = the cumulative diff from that base, plus a
labelled "this round's change" (`git diff <previous candidate> <new candidate>`); each path list is
capped at 20 entries plus a "(+N more)" count. So on the check-failure path the second reviewer
also reviews round one's change. Checks are fresh (the first round's passing rows are never reused
for the repaired candidate) and the review is fresh; on a root run the root's `max_repairs`
ceiling and the deadline are checked at every handoff, and a stop wins every handoff. A second
failure, or a repair that changes no content, ends the run - there is no third implementer. "No
content" means the scoped fingerprint equals the previous round's: with the tree unchanged too the
run blocks `verification_failed`; if the tree changed but the scoped fingerprint did not (for
example an un-ignored `__pycache__` file under an allowed directory) the run records
`no_content_change` and blocks `scope_violation`, buying no reviewer and no re-run of the checks.

The repair runs in the first round's worktree, so before anything is bought that worktree must
still be exactly the previous candidate: same HEAD, same tree, nothing changed, and no index entry
flagged assume-unchanged or skip-worktree (`git ls-files -v`; git's status cannot show whether a
flagged file still holds the checked bytes, and HFlow never clears the flag). A worktree that
moved or became dirty after the freeze - for example a check that wrote a file git does not
ignore - or when the shared Git metadata changed since dispatch (see "Shared Git metadata is
compared before HFlow's git reads it again" below), records `workspace_drift`, blocks
`scope_violation` and buys nothing; nothing is reset or
overwritten. Ignored files the first round's checks or review left **outside** what the scoped
fingerprint hashes (a `.ruff_cache` or `.mypy_cache` at the repository root, say) are not drift:
the first freeze refused every ignored path off its allowlist (and every freeze refuses an ignored
file the scoped fingerprint hashes, and a sourceless `.pyc` outside `__pycache__`), so whatever
else is ignored now came from HFlow's own checks or is a cache the allowlist names, and the repair
round's freeze accepts exactly those paths, as literal
entries. The worker may not change them (the round's manifest comparison still sees them and
refuses a change as outside the scope), an ignored file is never staged, and an ignored path that
appears during the repair round still refuses the freeze. An ignored file a previous check left
**inside** a `write_allow` directory, where the scoped fingerprint would hash it (`src/run.log`
under `write_allow: ["src"]`; `__pycache__` and `.pytest_cache` contents are not hashed), is not
carried: no candidate commit holds it, so the fingerprint would describe bytes the frozen commit
does not have, and a worker edit to it would pass the manifest comparison. That repair is refused
as `workspace_drift` before it is bought, and the file is not deleted.

Each decision, refusals included, is stored as one `RepairRecord` row in `run_repair_records` and
appears in the run's inspection record, so "we did not repair because that check failed for an
environmental reason" is readable afterwards instead of leaving the run looking arbitrary.

**Offline checks.** `kind=fake` starts no process, so its evidence records `not_launched` and can
never trigger a repair. An offline test that wants the repair path exercised must declare the clean
process exit it is modelling (`FakeCheckRunner(verdicts=..., exit_reasons={"unit": "nonzero_exit"})`).
That is a deliberate modelling choice in the offline facility, not a compatibility claim about a
live harness.

## Where the data lives

| Data | Path |
|---|---|
| Runtime database | `%LOCALAPPDATA%\HFlow\hflow.sqlite` (Windows), `$XDG_DATA_HOME/hflow/` otherwise |
| Machine profiles | `<data-dir>/profiles/<id>.json` |
| Override | `--data-dir <dir>` or `HFLOW_DATA_DIR` |
| Profile selection | `--profile <id>` or `HFLOW_PROFILE` |
| Build id recorded into every run | `HFLOW_BUILD_ID`, else `hflow/<version>` plus the short git SHA |

`run`/`status`/`report` accept `--data-dir` before or after the subcommand. Runtime data
is never written inside a project checkout, so a run cannot dirty the tree it measures. A
controller writes its scratch and check artifacts under the data directory of the ledger it
opened (`<data-dir>/artifacts/...`, `<data-dir>/invocations/...`) - for `cancel` and `resume`
with `--data-dir` too; the platform default applies only when no data directory is given. The
test suite points `HFLOW_DATA_DIR` at a per-test temp directory (and clears `HFLOW_PROFILE`), so
running it never reads or writes your real data directory.

## Storage version and migrations (batch E1, extended by E2)

The database records its own **storage version**, separate from the public contract version.
Batch E1 adds `root_budgets` and `invocations` (plus a few columns); batch E2 adds
`evidence.exit_reason` - the structured reason a check ended - the `run_repair_records` table
that holds the run's repair decisions, and `attempts.is_repair`. The file is migrated to the
current `migrate.STORAGE_VERSION` (8 as of batch I) the first time a build that understands it
opens it. All DDL lives in `src/hflow/migrate.py`; `store.py` only decides when to migrate.

| Version | What it adds, and what an older row gets |
|---|---|
| v1 | the original bootstrap: `runs`, `attempts`, `evidence`, `run_notes`, `authorizations` |
| v2 | the root ledger (`root_budgets`) and the per-dispatch record (`invocations`) |
| v3 | `invocations.launch_requested_at`, splitting "a launch was requested" from "a launch happened", and `authorizations.origin`. Every v2 row past `reserved` keeps its own state and outcome and becomes a launch *request*, because v2 wrote that timestamp before asking the driver |
| v4 | the process facts a driver reports (`process_started_at`, `process_pid`, `spawn_kind`); older rows keep `spawn_kind = unknown`, because nothing reported a process to inherit. v4 also repairs rows the first v3 step left behind (a `settled`/`unknown` v2 row that kept its request time in `started_at`): only rows with `started_at` set and `launch_requested_at` empty are touched, so a correctly migrated row is never rewritten |
| v5 | `evidence.exit_reason`, `run_repair_records` and `attempts.is_repair`; older rows keep an empty reason and `is_repair = 0`, because the build that wrote them observed neither |
| v6 | the run's owner identity (`owner_token`, `owner_pid`, `owner_created`, `owner_host`, `claim_generation`); an older row keeps a NULL owner, read as "owner unknown" |
| v7 | `invocation_settlements`, the append-only record of `hflow ledger settle`; only adds a table |
| v8 | `integrations` (batch I2), one row per attempt to integrate a run's accepted candidate into a local branch, with partial unique indexes (one `preparing`/`checking`/`applying` record per run, one `applying` record per repository and target branch); only adds a table. A run accepted before v8 has no integration record and its receipt is untouched |

A file whose bootstrap never stamped a version is read by its *shape*, newest first, so a v2 file
without a version is never re-`ALTER`ed as if it were v1.

One v5 step **rebuilds** `attempts` instead of adding to it, because v1 had declared
`UNIQUE (run_id, task_revision, role)` and SQLite cannot drop a table-level constraint. The
rebuilt table keeps every row and every column and replaces that key with
`UNIQUE (run_id, task_revision, role, is_repair)`: one first attempt and at most one repair
attempt per run, revision and role, so a third implementer attempt cannot exist even if a bug
asked for one. Existing rows get `is_repair = 0` - every attempt written before v5 was a first
attempt. The rebuild follows SQLite's own procedure (create under a temporary name, copy, drop
the old table, rename the new one into place - renaming the *temporary* name is what keeps
`evidence` and `invocations` pointing at `attempts`) with foreign-key enforcement suspended before
the transaction opens (`PRAGMA foreign_keys` is a no-op inside one) and `PRAGMA foreign_key_check`
verified before the transaction commits, so a reference that stopped resolving rolls the
migration back rather than committing. An `attempts` table whose columns are not the ones this
step expects is refused rather than copied blind. The E1 phase rule stays as it was - an
implementer is only reserved before the candidate is checked - so `store.reopen_for_repair` moves the run back to its implementation phase in one transaction,
immediately before the repair reservation, and refuses a stopped or terminal run.

What happens on that first open:

1. The version is read **before** anything is written. A file that records a version newer than
   this build understands is refused with nothing touched - no snapshot, no write.
2. If the file is at version > 0, it is copied first with the SQLite backup API to
   `<db>.pre-v<version>.bak` (for example `hflow.sqlite.pre-v1.bak`), next to the database. A
   brand-new file gets no backup: there is nothing to protect. The copy is written to
   `<db>.pre-v<version>.bak.partial`, checked (`PRAGMA integrity_check` and its storage version),
   flushed and only then renamed into place, so an interrupted copy never takes the `.bak` name.
3. The migration runs inside one `BEGIN IMMEDIATE` transaction of plain statements. Any failure
   rolls the whole thing back, and re-opening an already-migrated file does nothing (no second
   backup, no rewrite). The backup is never overwritten, so the earliest pre-migration state
   stays restorable.
   - An existing `.bak` is checked the same way before it is trusted. If it is empty, damaged
     or of another version (for example left by an older build's interrupted backup), the open
     is refused with the file named and the ledger unchanged. Move that file aside, then open
     again to take a fresh snapshot.

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
invocation reservation, a delivery decision recorded later, a repair decision). Restoring is a
deliberate data loss of everything after the snapshot; it is the only supported rollback, and it is
why the snapshot is never overwritten.

Two things the migration deliberately does **not** do: it never back-fills `evidence.exit_reason`
for an old row (a stored exit code is not an observation that the process completed cleanly, and an
empty reason is exactly what keeps a pre-v5 row ineligible to trigger a repair), and it never
rewrites the reason of a row that has one.

## Reading a blocked run

`hflow status <run_id>` prints the block code and the attempts. The codes you will
actually meet today:

| Code | Meaning | Next action |
|---|---|---|
| `budget_exhausted` | the turn ceiling, the authorization or the root could not cover the next dispatch: root submissions or repairs spent, the root's deadline passed (at a dispatch, during the checks, or at acceptance), or a rootless run of a task whose root was registered after the run was admitted (a rootless run of a task that already has a root is normally refused before its run row exists) | split the task, or raise `limits` deliberately; for a root, pass `--root-budget-file` with a fresh approval - nothing tops a root up |
| `verification_failed` | an approved check failed; the worker's exit code is irrelevant. Also a repair round that changed nothing | read the evidence detail, fix the code or the check, submit a new revision |
| `scope_violation` | files changed that the TaskSpec did not authorize, or a changed path matches the task's or the project's `write_deny` or the built-in deny list (`.git`, `.hflow`, `.acpxrc.json`) - even inside an allowed directory, and before anything is frozen. Also: the worker moved the worktree's HEAD (its own commit, amend, reset or checkout: "the worker moved HEAD from X to Y"); an index entry flagged assume-unchanged or skip-worktree ("index flags hide worktree changes from the freeze"); the candidate commit or the delivery changes a path outside the scope or under a deny rule (checked after the freeze and again at acceptance); a `write_allow` entry that started resolving outside the worktree during the run; a repair round that moved the tree but not the scoped content; a repair refused as `workspace_drift`; the shared Git metadata changed after dispatch ("shared Git metadata changed ...", before the freeze, before a repair round or at acceptance) | inspect the reported paths; nothing was accepted, and no receipt was written; for a metadata change, restore it before `hflow clean` or another worktree run |
| `evidence_stale` | the candidate changed after verification | re-run; do not reuse the old evidence |
| `review_rejected` | the reviewer returned a validated `changes_requested` | read the finding, then submit a new revision |
| `review_protocol_error` | the reviewer turn produced no usable verdict: missing, malformed or ambiguous output, a reviewer stream whose `messageId` resumes after another message started (the review evidence reads `invalid: agent_message_chunk on line N continues message ...`), or a final message holding two verdicts, for example one on each side of its own reasoning (`ambiguous`), a prompt-digest mismatch, a reviewer that could not be started, or a reviewer turn that `FAILED` (for example `model_rejected_before_prompt`, `cancelled_unrequested`, `stop_reason_max_tokens`), or a reviewer that sent an `agent_message_chunk` after its own prompt response, or whose output was not read to its end (`review_ambiguous`: the final answer is not identified). It is a wire failure, never the reviewer's judgment | read `block_reason` and the review evidence; fix the cause before a new revision |
| `outcome_unknown` | nobody knows how an invocation ended: its stop was not confirmed (including every cross-process `hflow cancel`), the controller was interrupted while it ran, or the driver reported an unknown outcome (`unbound_completion`, `boundary_not_empty`, `no_stop_reason`, `prompt_error_response`, `unknown_stop_reason`, `output_limit_exceeded`, `unparseable_output`, `completion_timeout`, `reader_failed`, `stream_not_drained` (implementer only; an undrained reviewer is `review_protocol_error` / `review_ambiguous`), `prompt_write_incomplete`) for either role | `hflow resume` to reconcile once no controller owns the run; the root stays blocked while the stopped invocation's ledger entry is open - a cross-process `hflow cancel` that lands during the checks or the review handoff leaves none, so the root then accepts a new revision even while the original controller is still finishing; submit a new revision only after it is resolved |
| `cancelled_by_operator` | a stop was requested and confirmed (or the run was stopped before any dispatch) | nothing runs; the workspace and evidence are kept |
| `driver_failed` | the implementer's driver reported a failure before/without a result (for example `model_rejected_before_prompt`: the profile's `--model` value is not one the agent advertises, so no prompt was sent) | read `block_reason`; fix the environment or the profile, do not blindly retry. An identical TaskSpec returns this blocked run; to run again submit a new revision, whose first implementer, under a root budget, is charged as a repair (a root with none left refuses that revision before its run row exists) |
| `workspace_client_config` | an entry named `.acpxrc.json` (in any letter case) appeared at the workspace root after admission (for the reviewer, in the candidate worktree), so the launch was refused at the driver's spawn gate before any process existed; the invocation is `not_started` and its allowance stays consumed. (A file already in the starting workspace never gets this far: it is refused before the run exists, see "What stops a run before it dispatches") | remove the file or directory, then submit a **new revision**: an identical TaskSpec returns this blocked run, and under a root budget the new revision's first implementer is charged as a repair, so the root needs a repair left (without one the revision is refused before its run row exists) |
| `workspace_env_file` | an entry named `.env` (in any letter case) appeared at the workspace root after admission (for the reviewer, in the candidate worktree), so the launch was refused at the driver's spawn gate before any process existed; the file was listed, never opened; the invocation is `not_started` and its allowance stays consumed. (One already in the starting workspace is refused before the run exists) | remove or rename the file or directory, then submit a **new revision**, as for `workspace_client_config` |
| `dsh_home_in_workspace` | the launch's bound `DSH_HOME` is relative, unresolvable, or in or around the role's cwd, the user's checkout (named from a `<repo>.hflow-worktrees/<run>` cwd) or the worktree directory, found at the driver's spawn gate before any process existed (admission normally refuses it first) | point `DSH_HOME` at an absolute directory outside them or unset it, prepare again (new authorization), then submit a **new revision** |
| `internal_error` | a controller step failed that is not the worker's result: the Git workspace could not be created, the candidate freeze failed or was incomplete (`freeze incomplete`), a prompt-digest mismatch on the implementer, an implementer driver that raised instead of returning a result (its ledger entry stays `requested`, which keeps the root blocked), an acceptance write that failed for a reason other than a stop, or a refused phase transition | read `block_reason`; it names the step |
| `not_implemented` | the requested driver does not exist in this build, or the TaskSpec asked for a delivery level this build cannot reach | use `--driver fake`, request `local_candidate`, or wait for the implementation |
| `context_file_change` | the frozen candidate changes a file DSH loads as instructions or skills (`AGENTS.md`, `CLAUDE.md` and their `.local` forms at any depth, root `.dsh/skills`, `.agents/skills`) that no `write_allow` entry names explicitly, or adds, changes or deletes the root `.env` (refused even when named); no candidate ref was written and no check ran | if the change is intended, name each file in `write_allow` by its path (a skill by an entry under its skill directory) in a new revision; a root `.env` change is never accepted |

The driver's own `error_code` (`unbound_completion`, `model_rejected_before_prompt`,
`boundary_not_empty`, `cancelled_unrequested`, `stop_reason_<reason>`, ...) is not a block code.
It is stored with the invocation's result in the ledger (`attempts.result_json`, and
`review_json` for the reviewer), and the driver's message is usually quoted in `block_reason`, and
for an unknown outcome of either role the code is quoted with it. What each one means:

| Driver `error_code` | Outcome | Meaning |
|---|---|---|
| `unbound_completion` | `OUTCOME_UNKNOWN` | a terminal response arrived, but it answers no observed `session/prompt` id; the turn's completion cannot be attributed to this prompt, so nothing is frozen, checked or taken as a verdict |
| `model_rejected_before_prompt` | `FAILED` | a `--model` value was passed, acpx refused it (or relayed the agent's refusal) with its own JSON-RPC error and exited, no `session/prompt` was sent, and the process tree is gone. Nothing reached a model; nothing is refunded |
| `boundary_not_empty` | `OUTCOME_UNKNOWN` | the client exited, but its Job could not be confirmed empty afterwards; work it started may still be running |
| `cancelled_unrequested` | `FAILED` | the harness settled the prompt as `cancelled` although no stop was requested for this invocation (DSH also does that when it disposes of a session) |
| `stop_reason_<reason>` | `FAILED` | the prompt's own response settled as `max_tokens`, `max_turn_requests` or `refusal` |
| `unknown_stop_reason` | `OUTCOME_UNKNOWN` | the prompt's own response carried a stop reason outside ACP v1's closed set (`end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`). Before this build it was `FAILED stop_reason_<reason>`; it now blocks the root for both roles, and for the reviewer it is no longer `review_protocol_error` |
| `prompt_error_response` | `OUTCOME_UNKNOWN` | the observed `session/prompt` was answered with a JSON-RPC error, ACP v1's failed-prompt shape. DSH sends -32603 `Internal error: turn failed: ...` / `assistant output delivery failed: ...` after the turn ran; before any model work it sends `prompt was not queued: ...` (-32603), invalid params (-32602), or a content-admission error with no fixed prefix (documented from DSH source, not observed). The code and the first 500 characters of the message (repr-quoted) go into `error_message`, the limitation and `block_reason`. The outcome stays unknown because whether a model call was made is not observable. A prompt answered with an error and a stop reason is unknown too. An error answering another id is not attributed, and one carrying the prompt's id after a request from the agent reused it is recorded as the limitation `prompt_error_unattributed` - DSH numbers its own permission requests from 0, so a long turn can hit this |
| `no_stop_reason`, `output_limit_exceeded`, `unparseable_output`, `completion_timeout` | `OUTCOME_UNKNOWN` | the stream does not say how the turn ended - including a prompt response with no `stopReason` (such as the `{messageId}` insertion acknowledgement in an unreleased ACP v2 RFD sketch); a later `state_update` with a stop reason settles nothing |
| `reader_failed` | `OUTCOME_UNKNOWN` | the driver's stream reader stopped before the end of the client's output (the exception class is named), or could not be started after the client was created; nothing is judged from the part it read |
| `stream_not_drained` | `OUTCOME_UNKNOWN` | implementer only: the reader had not finished the client's output within the drain wait after the client exited, so a stop reason in the part read settles nothing (a reviewer keeps `COMPLETED` with no verdict, `review_ambiguous`) |
| `prompt_write_incomplete` | `OUTCOME_UNKNOWN` | the turn settled with `end_turn`, but the prompt could not be fully written to the client's stdin (broken pipe), so the reported prompt digest is not what the client received. Recorded as a limitation on every other outcome. A client that reads part of its stdin and then closes it is not detected: the pipe accepts a write before the client reads it |

`stream_order` (`prompt_response_line`, `updates_after_prompt_response`,
`message_chunks_after_prompt_response`) is stored in `attempts.result_json` / `review_json` for
every bound result whose stream was read to its end without wire-state truncation, and `status`
prints it as a `stream` line per
invocation. A count above zero adds the limitation `updates_after_prompt_response=N: ...`. It
changes no outcome except the reviewer case above (`review_ambiguous`). What is pinned:
`test_real_client_prints_an_update_sent_right_after_the_prompt_response` shows that, with the
installed acpx 0.17.1 and the mock agent, a chunk written right after the response is printed after
it. From the 0.17.1 source (documented, not a timing guarantee): `exec` does not wait for idle after
the response and keeps reading while it closes the agent - 100 ms after ending its stdin, then
SIGTERM with a 1.5 s grace. The five recorded live DSH streams had no update after the response.

`end_turn` means the turn settled, not that the work succeeded (DSH maps blocked and aborted turns
to `end_turn` too - documented, not observed); acceptance is decided by checks and review.

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
  to claim there - and its terminate does nothing, so a forced stop or a deadline teardown
  (`completion_timeout`) there does not kill even the direct child - and nothing here reaches a
  remote model request or a remote bill. A
  `hflow cancel` typed in another shell is a different process from the controller running the
  task: it holds no handle, answers `unknown` and blocks the run `outcome_unknown` (see "Stopping
  a run").

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
digest describe what actually ran. `base_commit` may be a ref (`HEAD`, `main`); it is resolved
once to a SHA, and that SHA - not the name - is the worktree's start, every round's base and the
receipt's `candidate.base_commit`.

What the freeze commits, and what it refuses:

- `scope.write_allow` takes **literal** file or directory paths only. An entry containing `*`,
  `?` or `[`, or one that is or passes through a symbolic link or junction in the checkout, is
  refused at admission and by `prepare` (`scope_violation`, location `scope`); `write_deny` (task
  and project) keeps its globs. An entry that only starts resolving outside the worktree during
  the run (the worker replaced it with a link) blocks the run `scope_violation` before the
  freeze, and the run ends instead of being left `RUNNING`.
- A `write_deny` entry is matched after normalization (a leading `./`, `.` segments and repeated
  `/` removed), so `./config/**` blocks like `config/**`. One that is empty after that, absolute,
  drive-qualified or holds `..` could never match and is refused at admission and by `prepare`
  (`scope_violation`), the task's and the project's alike.
  An entry with a leading `/` or `\` (gitignore-style `/config/secrets/**`) is refused too, with
  its own message; an earlier build stripped that slash silently, so drop it from such an entry.
- **Ignored bytecode is not a candidate.** Every freeze, the first round's included, refuses
  `scope_violation` on an ignored file the scoped fingerprint would hash and on a sourceless
  `.pyc` outside `__pycache__` (Python imports it in place of a missing source, and no commit
  holds it); the freeze deletes nothing. `__pycache__` and `.pytest_cache` stay allowed. Before
  every `command` check of a worktree run, each regular `.pyc` directly inside a real
  `__pycache__` of the worktree is deleted (an emptied `__pycache__` too), so worker-left bytecode
  is never loaded, whether or not the check runs Python with `-I` or `-E`. Those files are never
  committed or fingerprinted, so the candidate commit and fingerprint do not change; `.pyc` files
  the candidate commit tracks stay, and `.git` and anything outside `__pycache__` are never
  touched. The walk follows no link or junction: a `__pycache__` link or junction (or a `.pyc`
  link) is left alone and the check is not started (`bytecode_not_cleared`, never a repair). Each
  check's evidence says how many files were removed, and a `bytecode_removed` run note gives the
  total. An in-place run deletes nothing in the user's checkout and gets no such protection.
- **The worker may not move HEAD.** The freeze first checks that the worktree's HEAD is still the
  commit the round started from - the original base in round one, the previous candidate in a
  repair. A worker that ran `git commit`, `--amend`, `reset` or `checkout` inside the worktree is
  refused `scope_violation` ("the worker moved HEAD from X to Y"): its commit never passed the
  scope and deny checks, which read the uncommitted status, so it is neither built on nor
  delivered.
- **No index entry may be flagged.** An entry marked assume-unchanged or skip-worktree (a
  lowercase or `S` tag in `git ls-files -v`) makes status, `git add` and the staged diff trust the
  index instead of the file, so the commit would hold old bytes while the checks read new ones.
  The freeze refuses `scope_violation` ("index flags hide worktree changes from the freeze")
  before it reads the status or stages anything, and the repair round's reconcile refuses the same
  way (`workspace_drift`, before the repair is bought). HFlow never clears the flags.
- Each `write_allow` entry is staged with `git add -A -- <entry>` under literal pathspecs, so a
  deleted, exactly listed file is committed as a **deletion** - the receipt's commit is the
  content that was checked and reviewed.
- A changed path matching the task's `write_deny`, the project's `write_deny` or the built-in
  deny list (`.git` and `.hflow` at the workspace root, `.acpxrc.json` at any depth) blocks
  `scope_violation` **before** the freeze, even inside an allowed directory: it is never staged,
  never committed, and no reviewer is bought. A `write_allow` entry under the built-in list is
  refused at admission. A project whose tools generate files under an allowed but denied
  directory will therefore block; a repository may keep an `.acpxrc.json`, but no task can change
  it (and a real driver refuses to launch on it, see "Client launch hardening").
- Anything staged outside the scope or denied refuses the commit, and if the worktree still shows
  any change after the commit the run blocks `internal_error` ("candidate freeze failed: freeze
  incomplete"). A repository whose line-ending or attribute settings leave a tracked file
  permanently modified fails closed here instead of freezing silently. (HFlow's Git ignores the
  system config, while a worker's own `git` in the same worktree reads it - Git for Windows ships
  `core.autocrlf=true` there - so the two can disagree about line endings on CRLF content a worker
  commits.)
- After the freeze, before a candidate ref is written or a check runs, the whole change from the
  original base to the candidate - as Git computes it - is held to `write_allow` and every deny
  list once more, and the delivery paths are checked the same way at acceptance, before a receipt
  names them, so a change the manifest scan skips (it does not read `.git`, `.hflow`,
  `__pycache__` or `.pytest_cache`) is still held to the scope. A refusal after the freeze writes
  no candidate ref and runs no check; one at acceptance writes no receipt.
- Path lists are taken with `--no-renames`: a moved file appears as both its old and its new path
  in the staged set, the round and reviewer lists and `candidate_paths`, so the deleted side of a
  move is held to the deny rules too.
- The same cumulative list is classified against DSH's context list (instruction files, the root
  skill directories, a root `.env`; see "What the harness does that HFlow does not control"),
  right after the cumulative scope check. A root `.env` the candidate adds, changes or deletes
  blocks `context_file_change` even when `write_allow` names it; any other listed file blocks
  `context_file_change` unless a `write_allow` entry names it by its path (a skill: an entry that
  is or lies under `.dsh/skills` or `.agents/skills`) - `src` or `.` containing it is not a
  declaration. Like a scope refusal, it writes no candidate ref and runs no check. What passes is
  kept as that attempt's `dsh_context` record, after the candidate ref is written; a stopped or
  refused candidate gets no record.
- Declared context files reach the reviewer packet in a section whose heading calls them
  untrusted data, not instructions (`packet.CONTEXT_FILES_HEADING`): the file list, a
  `git diff --no-renames <base> <candidate>` reference and a unified diff read through
  `GitRepo.diff_text_bounded` (`--no-ext-diff --no-textconv`, at most 8 KiB in all, fenced by
  delimiters carrying a digest of the fenced text, cut with an explicit marker). If the packet
  would then exceed 32 KiB it is rendered with the list and a marker instead. A repair packet
  lists the same files with the same framing. DSH still loads the candidate's version in the
  reviewer's and the repair implementer's worktree; the section is a label, not enforcement.
- HFlow's own Git commands cannot be hooked or redirected. Every call through its repository
  handle forces `core.hooksPath` to an empty HFlow-owned temporary directory,
  `core.fsmonitor=false`, `commit.gpgsign=false`, `core.ignoreStat=false` and
  `core.sparseCheckout=false` (through `GIT_CONFIG_COUNT`, read after the global, repository and
  worktree config files; when the caller's environment carries a `GIT_CONFIG_PARAMETERS` - what
  `git -c` exports, read after that list - the forced keys are appended to it too, so a caller's
  `git -c` cannot override them while its other settings still apply). It sets
  `GIT_CONFIG_NOSYSTEM=1` and `GIT_NO_REPLACE_OBJECTS=1`, and drops an inherited `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
  `GIT_COMMON_DIR` or `GIT_NAMESPACE` (every call names its repository by its working directory,
  so a variable inherited from a hook or an alias cannot point it elsewhere); the freeze commit
  also passes `--no-verify`. So no repository, user, worker or caller hook, monitor command or
  signer runs during `worktree add`, the freeze or a ref write, and HFlow's own `worktree add`
  never checks an entry out flagged assume-unchanged or skip-worktree (a flag set any other way
  refuses the freeze, above). The user's global config is still read for everything else, and
  filters cannot be switched off without breaking legitimate ones such as LFS, so a filter a
  worker configures is caught by comparison instead (next bullet).
- Shared Git metadata is compared before HFlow's git reads it again. A worktree shares
  `.git/config` with your checkout, and a worker (or a check, or the reviewer) can configure a
  clean, smudge or process filter there and select it through `.git/info/attributes` or
  `core.attributesFile` - no `.gitattributes` needed - so HFlow's own status, add or commit would
  run it. Right after `worktree add` (which can itself write `extensions.relativeWorktrees` under
  `worktree.useRelativePaths`, or the run's `config.worktree`) and before the first dispatch,
  HFlow snapshots every configuration key `git config --list` reports in your checkout and in the
  run's worktree, the files they come from (includes and `includeIf` targets too), both
  `config.worktree` files, `.git/info/attributes` and the global attributes file, and records
  only a digest: `git_metadata: snapshot before dispatch sha256:...` (never values). It compares
  again before the freeze, before a repair round (which records `workspace_drift` and buys
  nothing) and at acceptance (which writes no receipt); any difference blocks `scope_violation`
  with a reason that starts "shared Git metadata changed since the pre-dispatch snapshot (...)"
  or "... or became unreadable". Keys are shown with the subsection hidden (`filter.*.clean`,
  `url.*.insteadof`), plus file paths, at most five. The `git_metadata: changed <where>` notes
  carry the same list, because a stop that already decided the run keeps its own reason. A run
  that ends without a receipt for another reason (a failed check or a rejection that buys no
  repair, a driver failure, an unknown outcome, a stop while the worker ran) is compared once
  more and gets only a note, `git_metadata: changed when the run ended ...`; it never relabels the
  block, so an `outcome_unknown` stays. A comparison that cannot read the metadata at that point
  notes `git_metadata: unreadable when the run ended ...` and the run's block still stands. You
  see these warnings without opening the store: `hflow run` prints the at-exit note with its
  other notes, and `hflow status` / `report` print every `git_metadata: changed ...` /
  `unreadable ...` note as a `git metadata` line under the block (`git_metadata_notes` in
  `report --json`). Output git prints that is not text in the locale's encoding (a config value
  is raw bytes, so a worker can write any; a legacy GBK value under a UTF-8 locale) counts as
  unreadable: "... or became unreadable" after dispatch, and `internal_error` ("git workspace
  failed: ...") before it, when no snapshot can be taken - such a value already in your config
  blocks every worktree run until you fix it. What to do: run `git config --list --show-origin
  --show-scope`, inspect `.git/info/attributes`, the file `git var GIT_ATTR_GLOBAL` names and any
  included files, and restore them before `hflow clean` or another worktree run - both read the
  metadata again, and HFlow restores nothing. If the edit was your own (any change counts, even
  `user.name`), submit a new revision: an identical TaskSpec returns the blocked run, and under a
  root budget the new revision is charged as a repair (refused before its run row exists when the
  root has none left). An in-scope `.gitattributes` the worker
  wrote is not refused; it appears in the receipt's `limitations`, because HFlow's `git add`
  applied it at the freeze. Requires Git 2.31+. Offline-tested only (README, "Not verified").
- The worker packet still lists only the task's `write_deny` under forbidden paths; the project's
  and the built-in deny lists are enforced, not shown.

What the receipt gives you, and how to read it:

| Field | Meaning |
|---|---|
| `candidate.base_commit` | the fixed commit the worktree started from: always the resolved SHA, and after a repair still the task's original base |
| `candidate.git_commit` / `git_tree` | real Git objects for the frozen candidate |
| `candidate.fingerprint` | content hash over the write scope; **not** a Git id |
| `candidate.worktree` | where the candidate lived (until you clean it) |
| `candidate_paths` | the paths changed from that base to the final candidate - both rounds' changes after a repair |
| `verification.status` / `evidence_ids` | the approved check's result on that candidate |

`--driver acpx-dsh` (or a profile that binds it) launches the real Harness. It needs
`--authorization-file <auth.json>` plus `--authorization-mode`, and the artifact must cover
exactly this run. It refuses before reading credentials, creating a
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

The run's internal `WorkspaceProvenance` records its project root, Git common directory and
worktree path in the same transaction that attaches the worktree path. `clean` compares that
identity with the observed workspace and Git registration before using the repository for
cleanup. Run notes and receipt paths cannot redirect cleanup or replace that provenance record.
Historical records may use the root ledger's source repository; a missing path without a reliable
source is unknown, never proof that cleanup completed. Invalid provenance refuses cleanup or
leaves reconciliation unknown.

The preview prints the resolved path, the Git common directory, the registration, HEAD, the
candidate ref and its target, the tracked/ignored/unsupported status, and every reason for
the decision. It creates no ref and removes no file. `--dry-run` is the same preview;
combining it with `--apply` is refused.

`--apply` re-checks everything (an earlier preview is not a standing permission), claims the
run's cleanup intent in a short transaction that requires a terminal run and no active attempt,
and calls `git worktree remove` **without `--force`**. A run that becomes live before the claim
cannot be removed. Success requires both the working directory and its Git registration to be
gone; Git returning zero is not sufficient. It refuses when:

| Refusal | Why |
|---|---|
| `no_managed_workspace` | the run did not use a worktree |
| `workspace_provenance_missing` | a legacy rootless run has neither workspace provenance nor a root ledger repository; keep the workspace until its source can be established |
| `provenance_unreadable`, `provenance_mismatch` | stored workspace provenance cannot be read, or the observed path or Git common directory differs from it; no replacement identity is inferred from notes or receipt paths |
| `not_a_worktree`, `not_registered` | the path is not the linked worktree git has for this run |
| `is_source_repository` | the path is the repository's main worktree |
| `execution_active`, `run_in_flight` | an attempt or the run is still live |
| `stop_unconfirmed` | a cancellation was requested but never confirmed: a stop that reached a live run and came back `unknown` or `still_running`, any `still_running` answer, or an intent with no receipt. A stop of a run that had **already ended**, answered `unknown` (what `hflow cancel` gets when it asks about an ended run's invocation - the CLI holds no handle) and recorded with `run_already_ended`, decided nothing about the run: it does not refuse, the preview says so, and the run faces exactly the gates it had before the stop |
| `unfrozen_changes`, `head_drift` | the worktree holds changes that were never frozen. For a run without a receipt, `head_drift` means HEAD is neither the latest attempt's recorded frozen candidate nor the run's base |
| `unretained_commit` | a run without a receipt whose HEAD is not its base commit, and no branch, tag or `refs/hflow/` ref contains HEAD (a worker commit, or a commit refused after the freeze, which gets no candidate ref). Removing the worktree would drop that commit. Inspect it; to keep it, create a ref (`git branch <name> <commit>`) and run `clean` again |
| `unknown_ignored_files` | ignored files that are not known build artifacts (an `.env`, local data - and also a check's cache such as `.ruff_cache` or `.mypy_cache`: only `__pycache__`, `*.pyc` and `.pytest_cache` are known) |
| `index_flags_hide_changes` | assume-unchanged or skip-worktree flags can hide modified tracked bytes from Git status; inspect the files and flags before trying again |
| `unsupported_status` | unmerged or submodule records that cannot be interpreted |

A refusal keeps everything and releases the cleanup claim, so the same command works once
you fix what blocked it. Every deletion attempt, including the bounded retry, rechecks the
guards and the claimed workspace identity. What survives a successful `clean`: the candidate commit (reachable
through `refs/hflow/candidates/<run-id>/<attempt-id>`), its tree, the receipt, the evidence,
and the run's history. A run without a receipt has no receipt to keep; its HEAD is kept only
because it is the run's base or a ref named in the preview contains it. Repeat `--apply` is idempotent; a path that vanished without a cleanup
record is reported `MISSING`, never as a success. A new `--reconcile` conclusion uses the same
repository source and both removal facts; a surviving registration or directory is unfinished
cleanup, not a successful removal. A repeated `--apply` after a recorded success reports
"previously removed and recorded": it removes nothing and makes no new observation about the
path. It must not delete a directory that appeared there later. An existing completion record
remains historical and is not rewritten by reconciliation.

## Known limits of `clean`

- It protects against HFlow's own concurrent operations, not against another process running
  as the same user that deliberately holds files open or rewrites the ledger. Workspace
  provenance is controller-recorded consistency data, not an authenticated record.
- Windows can keep a directory undeletable for a moment after a check's child exits. HFlow
  retries once after a short settle; if git still refuses, it reports the failure and does
  not force anything.
- DSH on Windows can leave a standing permission entry on the worktree directory it worked in
  (documented upstream, not observed here); `clean` does not change permissions.
- Worktrees are not garbage-collected automatically, and `clean` deliberately never runs
  `git gc`, `git clean`, `worktree prune`, or `rmtree`.
- It does not check the run's shared-Git-metadata warnings. Even the preview runs `git status`
  in the worktree (and `--apply` then `git worktree remove`), and a status read runs any clean
  filter that metadata names. When `hflow status` shows a `git metadata` line, inspect and
  restore the metadata first (the "Shared Git metadata is compared ..." item under "M2 runs
  through the CLI").
- Without a receipt, HEAD still needs to be the recorded base or a commit retained by a ref.
  Those retention gates do not prove removal: reconciliation still needs a reliable repository
  source to check registration after the path has disappeared.

## Integrating an accepted candidate (`integrate`, batch I2)

A run ends at `ACCEPTED / LOCAL_CANDIDATE`: a frozen commit in a detached worktree. Putting that
change on a branch you work with is a **separate delivery** with its own record and its own
receipt. The run's `ResultReceipt` is never rewritten: `status` and `report` keep showing
`delivery LOCAL_CANDIDATE` for the run and list its integrations beside it. Nothing in this
section calls a model, dispatches an agent or spends a budget. The contract is
`docs/batch-i-integration-plan.md`.

```sh
hflow integrate prepare <run-id> --target main --project .hflow/project.json
hflow integrate apply   <integration-id> --expect-target <full tip printed by prepare>
hflow integrate reconcile <integration-id> [--owner-gone --attest "<what you know>"]
hflow integrate show    <integration-id> [--json]
```

**prepare** fixes the target branch's current tip `T` and builds exactly one integration commit
`M` on top of it, with Git plumbing only:

| The target since the task's base `B` | Mode | `M` |
|---|---|---|
| unchanged (`T == B`) | `squash` | one new commit with the candidate's tree on `B`. Earlier repair rounds (each round's candidate is a child of the previous one) never enter the branch's history |
| moved on (`B` is an ancestor of `T`) | `replayed` | `git merge-tree --write-tree --merge-base=B T C`, committed on `T`. A conflict is recorded as `conflict` with its paths; nothing is committed |
| rewritten (`B` is not an ancestor of `T`) | - | refused `target_moved`: HFlow does not guess how the candidate relates to that history |

`M` is kept reachable by `refs/hflow/integrations/<run-id>/<integration-id>`, then checked out in a
fresh detached worktree of its own (`<repo>.hflow-worktrees/<integration-id>`) where the run's
required checks run again (evidence kind `integration-check`, a new execution every time). The
paths `T..M` changes must be paths the accepted delivery changed and inside the task's scope.
The worktree is then removed **without `--force`**; if Git refuses (a check left untracked files),
it is left in place and named, and the integration's result does not depend on it. A passing
prepare ends `ready` and prints the apply command; prepare itself never moves a branch and never
writes your checkout or index. The project contract must be the one the run was accepted under
(same checks digest), and a `kind=fake` check is refused: it cannot verify a tree that is about to
land on a real branch. Git 2.40 or later is required. `--target` must be the branch's exact
spelling: on a case-insensitive file system `MAIN` resolves through `main`'s file, and every later
comparison (which worktree has it checked out, which ref an apply locks) would then miss the real
branch, so a name no ref carries exactly is refused. A run whose change already reached that branch
through an earlier integration is refused too, even if the change was reverted since; a `ready`
integration of the run whose commit the branch now contains (your merge after a hand-off) is first
recorded as `integrated`, never superseded.

**apply** is your approval. `--expect-target` must be the exact tip the integration was checked
against. Before anything moves, apply re-checks that the run is still `ACCEPTED`, that the
integration ref still points at `M` and that its evidence still describes `M`, records its intent
(`applying`) in SQLite, and only then runs `git update-ref refs/heads/<target> M T` - Git's
compare-and-set. It never forces and never creates or deletes a branch.

| What apply finds | Result |
|---|---|
| the target at `T`, not checked out anywhere | moved to `M`; `integrated`, basis `hflow_ref_update`; an `IntegrationReceipt` is written |
| the target checked out in any worktree - your main checkout included, or a worktree rebasing or bisecting it | **not moved** (moving it would leave that checkout's index and files at the old commit while its HEAD names the new one). You get `git -C <checkout> merge --ff-only <M>` to run yourself, then `hflow integrate reconcile <id>`. Exit `3` |
| the target already contains `M` | `integrated`, basis `operator_merge_observed` |
| the target moved elsewhere | `stale` (terminal); prepare again against the new tip |
| `<ref>.lock` exists | refused; HFlow never deletes a Git lock file |

A second apply of an integrated record writes nothing and returns the same receipt.

**reconcile** settles a record from what Git shows and never re-runs the update or a check. An
`applying` record whose process is gone becomes `integrated` (`observed_after_interruption`)
when the target contains `M`, `ready` again when the target is still `T`, `stale` otherwise. A
`preparing`/`checking` record whose process is gone becomes `interrupted` (its worktree is removed
without force when possible). A `ready` record becomes `integrated` (`operator_merge_observed`)
once the target contains `M` - this is how a hand merge after a hand-off is recorded. While the
recorded process may still run, reconcile refuses and writes nothing (exit `5`). When HFlow
cannot tell (`unknown`: another host, no creation time recorded, a pid it may not open),
`--owner-gone --attest "<what you know>"` lets you attest that the process has exited; the
attestation and your OS user name are recorded as such, never as an observation. A process HFlow
sees running (`matching`) is never overridden. `apply` runs the same reconciliation first when it
finds an `applying` record. A record another process settled in the meantime is reported as stored
rather than raised.

States: `preparing -> checking -> ready -> applying -> integrated`; terminal `conflict`,
`checks_failed`, `stale`, `interrupted`, `superseded` (a newer prepare of the same run replaced a
ready one) and `failed`. A run has at most one `preparing`/`checking`/`applying` integration, and
a repository has at most one `applying` integration per target branch.

Exit codes: `0` when the subcommand did what it was asked - `prepare` left the record `ready`,
`apply` left it `integrated`, `reconcile` left it `ready` or `integrated`; `3` for anything that
needs your decision (a conflict, failed checks, a stale or interrupted record, a hand-off because
the branch is checked out, a ref update that failed and left the record `ready`); `5` for a record
another process is still working on, or whose process may still run; `2` refused, nothing written
(including a Git error); `4` unknown id; `6` a stored integration record no longer validates.

What an integration receipt does **not** claim: anything about a remote (nothing is pushed), a
review of a merged tree (a `replayed` tree is checked, not reviewed again - the review covered
`B..C`), or who merged a hand-off (`operator_merge_observed` records only that the branch contains
`M`). The integration commit carries HFlow's fixed identity, not yours.

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

The examples in this file are illustrations; the shapes are generated. `hflow schema` prints
`AuthorizationRecord` (the artifact, defined in `authorization.py`), `RootBudgetPlan` (the root
budget file) and `RepairPolicy` (the `--repair-policy-file` document) next to `TaskSpec`,
`ProjectConfig`, `MachineProfile` and the rest. Write `base_commit` as the 40-hex SHA that
`hflow prepare --json` prints under `authorization.binding`, never as a branch name. The example
binding above is not complete: a real run's artifact also carries `roles`,
`effective_config_digest` and `project_contract_digest`, copied from `hflow prepare --json`
together with the rest of `authorization.binding`. Without the two digests it cannot authorize a
run that resolved a configuration and a contract, and a missing `roles` reads as implementer and
reviewer, which a run that dispatches no reviewer refuses.

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
  commit, task digest and task path, the roles the run will dispatch, the effective
  configuration, the project contract and, for a root run, the root binding (see the
  authorization section above). Any mismatch lists the offending fields;
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
  and the reviewer is never handed the implementer's own summary of its work;
- the implementer's stated deadline is the deadline its invocation is actually given: the
  configured value capped by what is left of the root's clock, or, before that clock has started,
  by the root's `deadline_seconds`. A packet rendered earlier that names a longer one is
  re-rendered before launch (the value can only shrink, so the packet cannot grow). `hflow
  prepare`'s packet preview still renders the configured value, so when a root's
  `deadline_seconds` is below the task's deadline the preview's deadline line, length and digest
  differ from the packet the run sends.

### Three kinds of transport evidence, kept apart

| Evidence | What it proves | Where |
|---|---|---|
| offline fake driver | the controller's state machine, budget, evidence and receipt rules | `tests/test_controller.py` and most of the suite |
| production driver + Python stand-in for acpx | the driver's launch, framing, event projection, stop and reconcile logic | `tests/test_packet_wire.py` (most cases), `tests/test_driver_acpx_dsh.py` |
| **installed pinned acpx + input-sensitive ACP stub** | the real Node client carries the rendered packet, and the agent checks the values it received | `tests/test_packet_wire.py`, the two `real_acpx` cases |
| **installed pinned acpx + the project's mock agent** | the hardened launch (absolute `cmd.exe`, stripped `DSH_*` mode variables) completes a turn with the exact packet bytes; a workspace `.acpxrc.json` is never launched on; `--model` is applied with `session/set_config_option` before the prompt against the mock's DSH-shaped `dsh-catalog` (placeholder ids), an unadvertised value fails before any prompt, and a missing catalog is recorded as not advertised; a prompt answered with a JSON-RPC error (`prompt-error` scenario) is relayed with the prompt's id and no client error line of its own, and is recorded as `prompt_error_response`; a chunk the agent writes right after its prompt response is printed and counted after it (`trailing-update`) | `tests/test_real_client_review.py` |
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
| a task with a `repair_policy` but no `--root-budget-file` | a real-driver repair is charged to a root's repair counter and clock; without one each new revision could buy another |
| a root whose `max_repairs` cannot cover the repair an armed policy may buy | the predictable half of a repair the root could never pay for; the root is not registered, so resubmitting with a root file that covers the repair works |
| a starting workspace that already holds `.acpxrc.json` at its root, in any letter case (the project root in place, the base commit's tree for a worktree) | the driver would refuse to launch there only after the dispatch was reserved, and an identical resubmission would then return the blocked run; refused now, nothing was recorded (not even the root a `--root-budget-file` names), so resubmitting works once the file is gone from that workspace (for a worktree run that means a base commit without it, so a real run needs an authorization issued for that base) |
| a launch program that is not an absolute file outside the workspace, or an acpx client entry inside the workspace (see "Client launch hardening") | a bare or relative name, or a program inside the workspace, would be looked up where the agent can write |

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
- an artifact factory or directory-creation failure records `ERROR/no_artifact_dir` without
  starting the check. A manifest write failure records `ERROR/output_capture_error`, keeping
  the observed exit code and captured stream references; `artifact=` names the directory,
  rather than presenting a missing or partial manifest as readable evidence. The run ends
  `BLOCKED/verification_failed`, not `CHECKING`; neither failure can buy a repair;
- `elapsed` covers launch, the check itself, the settle window and the reaping, so it is not a
  claim that the check's own timeout bounds the whole call.

### Worker log retention (what is bounded, and what is only measured)

Two different things live here, and the difference is stated rather than glossed:

**Bounded (HFlow's own retention and wire state).** Each invocation has a raw-log retention
budget (32 MiB by default), a 1 MiB pending-line bound, and a 20,000 nonempty wire-record bound:

- every stdout read is captured before parsing. `events.ndjson` retains the raw prefix up to
  the protocol share (`max_raw_log_bytes` minus the stderr share), including empty lines,
  invalid UTF-8 and an EOF without a final newline. `total_bytes` counts everything read;
  `digest` is SHA-256 of the retained bytes, so it verifies the file that remains, not the full
  discarded stream;
- all accumulated wire state is limited by both that byte share and 20,000 nonempty records.
  Records with no neutral event count too. At either bound the raw-line window, events,
  prompt/terminal/error records, model state and answer transcripts stop growing; output still
  drains and is counted. A stop reason beyond the bound cannot authorize completion;
- the client's **stderr** is drained from a pipe this driver owns and retained up to the stderr
  share. What exceeds it is read, counted and digested but not written. (It used to be a file the
  child wrote into and a reader that had nothing to read: the record said "0 bytes, not truncated"
  while the child wrote mebibytes. It is a pipe now, so the bound is real and the record is true.)
  Releasing an invocation never waits on that pipe: when some holder of its write end outlives
  the teardown (a process the Job could not run down, or one outside it), `release()` leaves the
  pipe to its daemon reader, which closes it at EOF, and records "stderr reader still blocked at
  release ..." in `driver.release_notes(invocation_id)` instead of hanging the controller after
  the outcome was already recorded;
- a line over 1 MiB is counted as unusable and discarded through its next newline. Its raw bytes
  still count toward capture; its suffix cannot become a new JSON message;
- spending the budget is recorded **on the read that spends it**, not on the next line, so a stream
  whose final line crosses the budget is reported as `output_limit_exceeded` with an
  `OUTCOME_UNKNOWN` outcome. Reaching the wire-record bound has the same error code, with its
  own reason named. Neither case produces a verdict, refunds allowance or re-dispatches.

When wire state is incomplete, `model_observation` and `stream_order` are absent. A launch that
passed `--model` records `model_applied=unknown`; one without it records `not_passed`. An observed
prompt still records `agent_turns=1`; when none was observed and the stream is incomplete, the
count is unknown rather than a claim that no prompt was sent. `prompt_response_line` keeps its
zero-based **nonempty-line ordinal**, so raw blank lines can make it differ from a physical file
line number. Historical records and replay indexing are not reinterpreted.

The protocol file is created before its reader starts. Failure to open, seek or read it,
including the final tail read after client exit, is `reader_failed`, not EOF. A valid prompt
response already read from the prefix cannot turn that failure into completion. No reviewer
verdict, complete-stream ordering or final model observation is taken from that prefix;
confirmed operator stops and unconfirmed process boundaries keep their existing precedence.

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
  *labelling itself* agent-authored;
- an approval is **not edited in place**: re-using an `authorization_id` is refused when the
  binding digest or any immutable field changed (`user_text`, `provided_by`, `authorized_at`,
  `max_top_level_submissions`, `mode`, `origin`), and the recorded row is never overwritten, so a
  consumed artifact cannot be revived or enlarged by editing its file. `origin` tells a user's
  artifact (`user_artifact`) from the record the CLI synthesizes for an offline root run
  (`cli_offline_synthetic`), which is refused for any driver but the offline fake.

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
not confine anything by itself. The policy lives only in the per-invocation global config HFlow
writes; no acpx permission flag is passed on the command line, which is safe only because a
workspace `.acpxrc.json` is refused (next section).

### What the harness does that HFlow does not control (documented, not observed)

These come from reading upstream source and documentation (DSH `dsh-v0.1.7-rc.1` through
`dsh-v0.2.0-rc.2`, pinned acpx 0.17.1) during the 2026-10-02 survey. None of them was observed in
a run on this machine; they are stated so nobody claims the opposite:

- **`approve-all` approves DSH's permission escalations.** DSH's ACP permission requests carry
  only a tool-call id (no kind, no title), so under `approve-all` - the write-capable implementer's
  mode - every request is approved, including a sandbox escalation the model asks for (up to
  `danger-full-access`) and, from 0.2.0 on Windows, a bundled skill that runs an ACL-changing
  PowerShell script unconfined after one approval. HFlow must not claim DSH's sandbox confined an
  implementer run. Under `approve-reads` (the reviewer) the same requests are effectively denied.
- **DSH runs with its own sandbox defaults.** HFlow removes `DSH_PERMISSION_MODE` and
  `DSH_TOOLS_MODE` from the child, so DSH falls back to its defaults (`workspace-write` with
  `ask`). On Windows DSH itself reports that sandbox as partial (reads unconfined), and it can
  leave a standing permission entry on the worktree directory.
- **DSH uploads session logs by default.** The shipped `session-log-deepseek` row is enabled at
  both surveyed versions (0.2.0 caps a request at 8 MiB), so what HFlow sends - rendered packets,
  and repository content tools read in the worktree - can reach DeepSeek as the session-log
  extension as well as model input. HFlow does not disable it.
- **`end_turn` is turn settlement, not success.** DSH maps blocked and aborted turns to
  `end_turn` as well as completed ones. HFlow reports such a turn as `completed` and decides
  acceptance from checks and review only.
- **A failed DSH turn is a JSON-RPC error, not a stop reason.** DSH answers `session/prompt` with
  `RequestError.internalError` (-32603) `turn failed: ...` after the turn, or `prompt was not
  queued: ...` (and other admission errors) before it; only blocked and aborted turns settle as
  `end_turn`. HFlow records `prompt_error_response`, leaves the outcome unknown and does not tell
  the pre-model form from the post-model one.
- **Turn order is the agent's.** Stable ACP v1 requires a turn's updates before its prompt
  response, and ACP #554 records agents that send the response first. From reading DSH
  0.2.0-rc.2's `dsh_session.ts`: DSH settles a prompt only once its agent is idle and its output
  queue has drained, but queues a model-catalog `config_option_update` off that chain, so one can
  follow the response. From the acpx 0.17.1 source: `exec` does not wait for idle after the
  response, but keeps reading while it closes the agent (100 ms after closing its stdin, then
  1.5 s after SIGTERM). HFlow records what arrives (`stream_order`) and gives a reviewer whose
  message arrives late no verdict.
- **acpx `--timeout` is per phase.** It bounds start-up, session creation, the model change and
  the prompt separately, not the whole call, and its expiry sends no `session/cancel`. HFlow's own
  invocation deadline - capped by the root's clock - is the bound, enforced by the local wait and
  the forced stop. HFlow never passes `--prompt-retries`.
- **DSH loads instruction files, skills and an env file from the workspace.** These facts were
  read at `dsh-v0.2.0-rc.2` (639ed015) only, not at the earlier tags. Its agent-instructions
  (maxBytes 65536) loads every present `AGENTS.md`, `CLAUDE.md`, `AGENTS.local.md` and
  `CLAUDE.local.md` from the project root - the nearest `.git`, a worktree's `.git` file included -
  down to its cwd, and in a deeper directory after a tool touches a file there, following links.
  Its skill-filesystem scans the root `.dsh/skills` and `.agents/skills`, and loadLayeredEnv reads
  `<cwd>/.env`. The reviewer's and a repair implementer's cwd is the candidate worktree. HFlow
  refuses a frozen candidate that changes one of these files without `write_allow` naming it, or
  that touches the root `.env` at all (`context_file_change`), and records the declared ones
  (`dsh_context`, shown by `status` and `report`, with a receipt limitation, and listed in the
  reviewer and repair packets as untrusted data); an ignored file of this kind already refuses the
  freeze, and files already in the base commit are not flagged. A root `.env` that appears in a
  worktree any other way refuses a real launch at the driver's spawn gate (`workspace_env_file`).

Pinned to dsh-v0.2.0-rc.2 (639ed015), documented, not observed:

- **DSH layers two `.env` files into its environment.** `loadLayeredEnv` reads `<cwd>/.env`
  (the workspace acpx starts DSH in), then `$DSH_HOME/.env`; a name the child already has wins.
  A bootstrap name (49 of them: `PATH`, `NODE_OPTIONS`, the proxy and CA variables, Git's SSH,
  pager and editor commands, and so on) or a `DSH_`, `XDG_`, `DYLD_` or `BASH_FUNC_` prefix in a
  workspace `.env` makes DSH throw before it serves ACP; only the home `.env` may set the
  proxies. HFlow does not parse either file, so it cannot predict this.
- **Credential precedence:** the launch environment, then `$DSH_HOME/.credentials.yaml`, then
  `<cwd>/.env`, then `$DSH_HOME/.env`. With `DSH_HOME` unbound the home is the per-invocation
  empty one, so the launch environment and a workspace `.env` are what is left - and a real launch
  refuses a workspace `.env` (`workspace_env_file`), so in practice the launch environment.
- **`cordis.patch.yml` layers** in `$DSH_HOME` and `$DSH_HOME/profiles/<profile>` can replace
  the shipped sandbox-policy and approval rows.
- **Agent instructions and skills:** `$DSH_HOME/AGENTS.md`, the AGENTS.md/CLAUDE.md(.local)
  chain from the nearest `.git` marker down to the cwd, and skills under `<root>/.dsh/skills`,
  `<root>/.agents/skills` and `<home>/skills`. Nested instruction files DSH reads after a tool
  call cannot be captured by any record taken before the launch.
- **`DSH_TELEMETRY_*` and `DSH_AGENTS_HOME` pass through** to the child; HFlow removes only
  `DSH_PERMISSION_MODE` and `DSH_TOOLS_MODE`.
- **`~/.agents/skills` resolves inside the per-invocation home**, because `~` is the redirected
  USERPROFILE.

## Client launch hardening

What the launch path does, for both roles, since the 2026-10-02 refinement:

- **A workspace `.acpxrc.json` is never launched on, and is refused as early as it is knowable.**
  acpx always loads `<--cwd>/.acpxrc.json` and lets it override HFlow's per-invocation config -
  scalar settings such as permissions win over the global file, and a project `agents.<name>`
  entry replaces the agent argv HFlow bound. There is no flag to skip or pin it (upstream
  openclaw/acpx issue #835, open as of v0.19.4). The name is matched **without regard to case**,
  and only among the workspace root's own entries: on a case-insensitive filesystem (NTFS by
  default) acpx's open of `.acpxrc.json` finds `.ACPXRC.JSON` too, so every spelling refuses and
  the refusal names the one it found, while a nested `src/.ACPXRC.JSON` or a lookalike such as
  `.acpxrc.json.bak` does not. The refusal happens in two places:
  - **At admission**, which `hflow prepare` and the run's dispatch gate share: a real run whose
    starting workspace already holds the file - the project root for an in-place run (a listing
    of that directory), the base commit's root tree for a worktree run
    (`git ls-tree -z --name-only <base>`, no pathspec, because a pathspec matches
    case-sensitively) - is refused `workspace_client_config` (location `workspace`) before a run
    row, an authorization record, a reservation or a process exists. An uncommitted copy in your checkout is not in the worktree
    and does not refuse a worktree run. Nothing was recorded (with `--root-budget-file`, not even
    the root), so resubmitting works once the file is gone; for a worktree run that means
    committing its removal, and because the binding carries the resolved base SHA, a real run then
    needs an authorization issued for the new base.
  - **At the driver's spawn gate**, for every invocation, before any process exists: a workspace
    whose root contains **any** entry by that name in any case (file, directory or link) is
    refused, and so is one whose root cannot be listed for a reason other than not existing. This
    now only catches a file that appeared after admission - most importantly in the candidate
    worktree the reviewer runs on, which stops an implementer from planting a config the
    reviewer's acpx would load (the freeze already denies writing one). The dispatch is already
    reserved by then: the run blocks `workspace_client_config`, the invocation is recorded
    `not_started` and its allowance stays consumed. The remedy is not "submit again": remove the
    file and submit a **new revision** - an identical TaskSpec returns the blocked run, and under a
    root budget the new revision's first implementer is charged as a repair, so the root needs a
    repair left (`used_repairs < max_repairs`; otherwise that revision is refused
    `budget_exhausted` before its run row or authorization record exists).
  The spawn-gate check runs just before the spawn, and acpx reads the file a moment later - a
  process writing the workspace in that window is not covered.
- **A workspace `.env` is never launched on** (user ruling 2026-10-03). DSH loads `<cwd>/.env`
  at launch into its own environment and its tool processes (documented at dsh 5badb150, not
  observed). HFlow refuses rather than parses: the file is never opened, and every surveyed
  name denylist grew after incidents. The rule mirrors `.acpxrc.json` exactly - any letter case,
  root entries only, any kind of entry - at admission (`prepare.start_workspace_env_file`: the
  project root's listing, or `git ls-tree` of the base commit's root) and at the driver's spawn
  gate for the role's actual cwd, with the same remedies and the same time-of-check window. The
  offline fake driver is unaffected.
- **A bound `DSH_HOME` the agent can write, or that sits above the workspace, is never launched
  with** (user ruling 2026-10-03). DSH never checks where its home is, and loads patch layers,
  `AGENTS.md`, skills and `.env` from it. `acpx_dsh.dsh_home_workspace_problem` refuses a relative
  value (a leading `~` included), one that cannot be resolved, and one whose path as written or
  with links resolved (`os.path.realpath`, case-folded) equals or lies inside, or contains, a
  protected directory: at `prepare` and the run gate the project root and the worktree directory;
  at the spawn gate the role's cwd and, for a `<repo>.hflow-worktrees/<run>` cwd, that directory
  and the checkout named by it. An unbound `DSH_HOME` (the per-invocation home) is unaffected. Not
  covered: 8.3 short names `realpath` leaves unexpanded, and other directories the agent may write.
- **Every launch program is an absolute file, and none may live in a workspace.** `dsh` - and
  `node` for a `.js` client, `python` for a `.py` client - is resolved to an absolute file from the
  absolute `PATH` entries of the resolving environment only (with `PATHEXT` on Windows), never
  from the current directory or a relative entry. A missing launcher makes the launch **not
  resolvable** (`prepare` and the dispatch gate refuse it, `hflow doctor --profile` reports the
  role unusable); there is no bare-name fallback, so on a machine with no `dsh` on `PATH` the
  launch is reported missing rather than "resolved". An explicitly named program
  (`HFLOW_ACPX_NODE`, a driver argument, an agent-argv override) must be an absolute path. A
  launcher, `dsh`, client interpreter or acpx client entry (the script `node` runs; its package
  root contains it, so a package root inside a workspace is caught too) inside the project root or
  the directory the run's worktrees are created in (`<repo>.hflow-worktrees`) is refused as
  "inside the workspace": the agent can write there. So is a workspace that lies inside the
  `node_modules` tree the client entry loads its modules from, because the agent could rewrite a
  dependency the client runs. One consequence:
  with `HFLOW_ACPX_CLI` unset and no copy under `<data-dir>/m0/acpx`, the entry falls back to
  the HFlow checkout's own `.probe/acpx/node_modules/acpx/dist/cli.js`, so a run whose project
  root is the HFlow repository itself reports the launch as **not resolvable**. Point
  `HFLOW_ACPX_CLI` at an acpx installed outside the project.
- **The DSH batch shim goes through the absolute `%SystemRoot%\System32\cmd.exe`.** A bare
  `cmd.exe` can be searched for in the agent's working directory (the worktree) before `PATH`.
  If `SystemRoot` is missing from the resolved environment the launch is not resolvable; like
  every other launch program, it never falls back to a bare name.
- **The child never searches its working directory for a program.** Every child environment -
  each invocation's, and the zero-model preflight's - gets `NoDefaultCurrentDirectoryInExePath=1`
  (any other spelling of that name is removed first), and relative or empty `PATH` entries are
  dropped. Without that, cmd.exe running the DSH npm shim's bare `node`, and any bare name acpx
  spawns, would be looked up in the workspace before `PATH`. Only the working directory and
  relative entries are excluded: absolute `PATH` entries are passed to the child as they are.
- **Ambient `DSH_PERMISSION_MODE` and `DSH_TOOLS_MODE` are removed** from the child environment
  (including anything passed through the driver's `extra_env`) and never set, so an operator's
  shell cannot silently switch DSH's sandbox off for a role. `hflow doctor` lists which of the two
  were present and removed. Binding a per-role value instead is an open decision.

### What DSH reads on its own: the launch-surfaces record

For each invocation and role, just before the spawn gate, the driver records what DSH would read
besides HFlow's packet (`src/hflow/drivers/dsh_surfaces.py`). It is a record: nothing in it is
enforced, refused or part of an approval.

- **Fields:** the DSH home and how it is chosen (`bound`, or `per_invocation` when `DSH_HOME` is
  unbound - inferred from upstream source), the home's `cordis.patch.yml`,
  `profiles/<profile>/cordis.patch.yml`, `.env`, `AGENTS.md` and `skills`; the workspace, its
  `.env`, the project root (the nearest `.git` marker), the AGENTS.md/CLAUDE.md(.local) files
  present from that root down to the workspace and the two project skill directories; the names
  of the DSH_* variables that reach the child; whether `DEEPSEEK_API_KEY` is among the child's
  variable names; and the client identity - acpx and @agentclientprotocol/sdk versions from
  their package.json (the SDK found by Node's node_modules walk from the client entry), and the
  dsh carrier.
- **How it is read:** a fixed list of paths, never a directory listing. A regular file gets its
  size and SHA-256 (none above 4 MiB); a `.env` is examined by stat only - presence and size,
  never opened - and the home's stored-credentials file is never read. An entry that cannot be
  examined is `unknown`, not absent. Variable values are never recorded. Nothing is executed.
- **Carrier:** only a Windows batch shim's text is classified. The Desktop shim (it sets
  `ELECTRON_RUN_AS_NODE` and runs an absolute `DeepSeek Harness.exe`) gives `desktop`, with the
  version from `primary-runtime/runtime.json`'s `desktopVersion`, which the Desktop README
  documents as equal to the dsh version (not verified); `payloadDigest` is never read or shown.
  The npm shim gives `npm`, with the version from its `@deepseek-ai/dsh` package.json. Anything
  else is `unknown`; an override argv that does not start the resolved dsh is `not_applicable`.
- **Where it shows:** `prepare`'s `launch_surfaces` and its "launch surfaces" text section (a
  worktree's files are not known before dispatch, and a per-invocation home is named, not looked
  into); `doctor`'s `child_dsh_home` and, with `--profile`, the per-role probe notes; the
  per-attempt `dsh home`, `workspace` and `client` lines of `status`; and `report --json`'s
  `launch_surfaces` / `review_launch_surfaces`. The offline driver, an older row, a stop that won
  the spawn gate and a failed observation show "not recorded"; a failed observation's reason is in
  the result's limitations, and it never changes or stops the launch. In `prepare`, a role whose
  surfaces could not be examined is left out of that section and named in prepare's notes; the
  preview itself still completes.
- **Time of check:** DSH reads the files after the look. A worktree holds only what the base
  commit tracks, so an untracked `.env` in your checkout is not in it.
- A DSH home that lies inside the workspace is noted in the record (the agent could write what
  DSH loads at the next launch) and, since the 2026-10-03 ruling, refused on a real driver
  (`dsh_home_in_workspace`, see "Client launch hardening").

## Model selection

What a profile can say, per role, in `model_selection`:

| Value | Meaning |
|---|---|
| `"native_profile"` | no model flag; the DSH launcher's own profile decides. The launch digests exactly as it did before model passing existed |
| a bare token, e.g. `"deepseek-v4-pro"` | 1-128 characters from `A-Za-z0-9._:/-`, not starting with `-` (so it can never read as a flag) |
| a two-element JSON array, e.g. `["deepseek-official","deepseek-v4-pro"]` | a `[provider, model]` pair - DSH's own option id. Written as an array or as a string holding one, and stored in compact form, which is the exact value acpx and DSH compare |

Anything else is refused when the profile loads. A value other than `native_profile` is passed to
the acpx **client** (not the agent argv) as the global flag `--model <value>`, before `exec`, and
is bound as `LaunchConfig.model` - so it is part of the effective-configuration digest and of what
an approval covers. That this counts as a "fixed flag" under AGENTS.md rule 9 is a user ruling
(2026-10-02): the value is fixed by the machine profile, validated and digest-bound, and is never
task text, user content or a credential.

What the pinned acpx 0.17.1 does with it (read in its bundle, then run against the project's mock
agent - offline evidence only): after `session/new` it looks for the agent's select option with
category `model` (preferring id `model`, grouped options included). If the value is already
current it sends nothing; otherwise it sends `session/set_config_option` before `session/prompt`.
It **fails closed**: an unadvertised value, or an agent with no model option, ends the client with
its own JSON-RPC error and no prompt sent, which HFlow records as `FAILED`
`model_rejected_before_prompt` (nothing reached a model; nothing is refunded). acpx also copies the
value into `session/new` `_meta.claudeCode.options.model` - the same bound value, in a second
place. Every other missing-stop-reason case stays `OUTCOME_UNKNOWN`.

Model and reasoning observations belong to the first valid created session, checked against
the first prompt's session. Updates or model-set requests for another or missing session do
not alter them. A creation/prompt mismatch or request-id collision leaves the requested model
`unknown`. With no model-set request, `passed` needs both the initial and final values to match
the request. The effective value remains the session's last reported configuration, including
same-session updates after the prompt response; it is not proof of the model used to reason
during that turn. A failed protocol read supplies no final model observation.

What `status` and `report` show: one `model` line per invocation, with `model_applied`, whether
a catalog was `advertised`, the `requested` and `effective` values and where the effective one
came from (`session/new`, `session/set_config_option` or `config_option_update`), the number of
changes, and the `thought_level` value. `model_applied` is classified from the stream and never
guessed upward:

| `model_applied` | Meaning |
|---|---|
| `not_passed` | no `--model` was on the client command line (`native_profile`) |
| `passed` | the flag was passed, no change request was needed (the session started on that value), and the stream's last reported value is **still** the requested one |
| `accepted` | `session/set_config_option` for the model succeeded and the effective value equals the request |
| `rejected` | refused before any prompt: the client's confirmed pre-prompt refusal (`model_rejected_before_prompt`), or a change request the agent answered only with errors |
| `unknown` | anything the stream does not settle: a change request with no answer, or an answered one whose effective value is not the request; or no change request and a value other than the request - a later `config_option_update` that moved the model away, or a refusal the stream shows (not advertised, not in the catalog) that an earlier outcome such as `boundary_not_empty` or `completion_timeout` shadowed. That last case stays `unknown`, not `rejected`, because no pre-prompt rejection confirmed it |

`report --json` carries the same facts per
attempt as `model_observation`/`model_applied` and `review_model_observation`/
`review_model_applied`. A run recorded before this, or by the offline driver, says `not recorded`.

What is **not** known, and why profiles should stay on `native_profile` for now: the
`model_selection` capability stays `documented` for real DSH. DSH advertises a `model` selector
(observed in the recorded M0 `session/new` reply) and its source routes `session/set_config_option`
to it, but no live `set_config_option` round trip has been observed, and whether DSH 0.2.0 answers
it with the full option list is open - a reply that reports no effective value leaves
`model_applied` at `unknown`, never `accepted`. Which
values the current DSH build advertises - the M0 catalog was observed on DSH 0.1.5 - needs a
zero-prompt live catalog check first, and that check is a live harness launch that needs its own
explicit approval (AGENTS.md rule 10). Changing a role's model changes the digest, so its
authorization must be re-issued.

## Reading the counters honestly

`status` prints several things that are easy to confuse. This is the real output of `hflow
status` for an offline fake run with a root budget file (one implementer, one reviewer, one
`command` check, `--driver fake --root-budget-file`, a temp data dir; no model), with the long
paths and digests shortened:

```text
run           R-irlfug6bgw
task          T-status-demo revision 1
state         ACCEPTED
delivery      LOCAL_CANDIDATE
claimed_by    local-controller
turns         reserved 2/4, implementer self-reported 1 (a self-report, not a dispatched count and not a bill)
spec_digest   sha256:86d411dd...
configuration command_line (no profile) digest=sha256:b3ff83ec...
  implementer  agent=command-line driver=fake -> fake
  reviewer     agent=command-line driver=fake -> fake
  writes      implementer=False reviewer=False
invocations   implementer=1 reviewer=1 (attempt rows; a deterministic dispatch count, not a model-request count)
root budget   root-7e95f001e712052558f0de0542c8abd8
  covers      project demo, task T-status-demo
  repo        <temp>\demo-project
  ledger      <temp>\data\hflow.sqlite
  submissions used 2/3 (remaining 1)
  repairs     used 0/0 (the root's own counter: a repair dispatch is charged here; whether repair was armed for a run is in that run's repair decisions)
  deadline    2026-10-03T13:20:07Z (root limit 86400s, measured from the first reservation at 2026-10-02T13:20:07Z)
  runs        R-irlfug6bgw
  approvals   AUTH-offline-gsld39zld8
dispatch ledger
  reserved=0 requested=0 started=0 not_started=0 settled=2 unknown=0 launch_unknown=0
  processes   0 operating-system child(ren) reported by a driver (ever started 0); 2 launch(es) ran without one, which is what the offline driver does
  meaning     reserved = the dispatch transaction committed: allowance spent, intent durable. Not a launch, not a process, not a model request
  meaning     requested = the controller asked a driver to launch it; nothing reported yet. A crash here leaves launch_unknown, which blocks the root
  meaning     started = the launch happened. Whether it created a process is the separate count above; a childless launch is still a launch
  meaning     not_started = no launch happened (a stop won the handoff, or the driver reported none); the allowance is kept, never refunded
  meaning     settled = a recorded result was applied; unknown = a launch was never settled, which blocks the root and is never re-dispatched
  provider model requests: unknown - no invocation row records one, and neither a reservation nor a process is counted as one
  invocations
    I-dlqeqsf4tq  role=implementer state=settled spawn=no_process repair=False attempt=A-2432mgtqqj approval=AUTH-offline-gsld39zld8
      launch_requested_at=2026-10-02T13:20:07Z launched_at=2026-10-02T13:20:07Z settled_at=2026-10-02T13:20:07Z outcome=completed detail=implementer invocation completed
    I-zlgdoixhe6  role=reviewer state=settled spawn=no_process repair=False attempt=A-2432mgtqqj approval=AUTH-offline-gsld39zld8
      launch_requested_at=2026-10-02T13:20:08Z launched_at=2026-10-02T13:20:08Z settled_at=2026-10-02T13:20:08Z outcome=completed detail=review invocation completed
candidate     workspace still matches the accepted fingerprint
attempts
  A-2432mgtqqj  revision=1 role=implementer state=SUCCEEDED outcome=completed repair=False
    model         implementer not recorded (this result carries no model observation)
    stream        implementer updates_after_prompt_response=unknown (not recorded: no bound prompt response, no stream read to its end, or a result without this record)
    launch        implementer surfaces not recorded (this result carries no launch-surface record)
    model         reviewer not recorded (this result carries no model observation)
    stream        reviewer updates_after_prompt_response=unknown (not recorded: no bound prompt response, no stream read to its end, or a result without this record)
    launch        reviewer surfaces not recorded (this result carries no launch-surface record)
dsh context   not recorded: no frozen Git candidate was classified for this run (an in-place run, a run that ended or was refused before its candidate was kept, or one recorded before this build)
repair        no repair decision recorded
evidence
  E-69hb2f96cn  kind=verification check=unit status=passed exit=0
  E-upyj46qytc  kind=review check=review status=passed exit=unknown
model_calls   0 (this command; provider-side requests are reported by the receipt and stay unknown when not observed)
```

- The sample predates storage v6. A current build prints two more lines right after
  `claimed_by`: `owner pid=... host=... created=... generation=N token=xxxxxxxx...` (or "not
  recorded" for a run claimed before v6) and
  `liveness <matching|gone|unknown|not_recorded|not_probed> (lock ...; <basis>)`. The basis says
  what the word rests on: "observed by this command, not stored" only when `status` actually asked
  the operating system; "decided by rule, nothing was probed" for an `unknown` that needed no
  question (another host, a pre-v6 pid with no host); "nothing to observe" for `not_recorded` (no
  owner and no controller process was recorded - not proof of death); "not probed" for a terminal
  run like this one (`not_probed`). Nothing of it is stored. `claimed_by` stays the human label.
- `reserved 2/4` means two turns were *paid for* out of a ceiling of four: one for the
  implementer invocation and one for the reviewer invocation. Both were dispatched; a run that
  cannot afford the review turn blocks *before* the reviewer starts.
- `implementer self-reported 1` is the sum of the implementer's self-reports across rounds (a
  repaired run adds both rounds; a result that arrived after a stop adds nothing). It is not the
  reserved total and it cannot return reserved budget. The receipt's `controller_turns_observed`
  is the same sum. The controller's own **dispatch** count is
  the `invocations` line.
- `configuration` is the effective configuration the run recorded once, with its digest; a run
  that predates config binding says `not recorded` instead of being back-filled.
- `root budget` reads the ledger row: `submissions` are top-level dispatches of the whole root
  (every run and revision of the task), `repairs` is the root's own repair counter, and
  `deadline` is fixed at the root's first reservation and never reset. `approvals` names every
  authorization charged; an offline run without `--authorization-file` shows the CLI's labelled
  `AUTH-offline-...` record, which can never authorize a real transport.
- The **dispatch ledger** keeps four facts apart, and they are not one number: an allowance
  *reserved*, a launch *requested*, a launch that *happened* (and, separately, whether it created
  an operating-system *process*), and a result *settled* - the per-state meanings are printed
  under it, and are the `InvocationStartState` values described in `docs/architecture.md`:
  - `processes` (and its historical alias `ever started`) counts invocations whose driver
    reported a pid. It is never derived from a result state, so this offline run shows two
    settled launches and **zero** processes; a real `acpx-dsh` run shows `spawn=process` and a
    count per invocation;
  - `unknown` and `launch_unknown` both block the root and are never re-dispatched; neither
    counts as a process;
  - `operator_settled=N (by attestation, not observed)` appears only once `hflow ledger settle`
    closed an entry; the entry's lines then show who attested, when and the text. It is not a
    result, a process or a provider request;
  - **provider model requests: unknown.** No invocation row records one, and a reservation or a
    process is never counted as one.
- Whether a Harness makes internal model requests per turn is not observable here, so billed
  usage stays `null` and "billed model requests" stays `unknown`. Do not read `null` as `0`.
  ACP `usage_update.used/size` is context-window usage, not a bill, and an agent-reported
  `PromptResponse.usage` (UNSTABLE in ACP) or `usage_update.cost` is not a bill either: neither
  ever fills `provider_billed_tokens` / `provider_cost` / `subscription_quota_remaining`.
- A run with no root ledger row (any run recorded before E1, or one that never used a root
  budget file) prints `legacy / not recorded` for both the root budget and the dispatch ledger.
  That is **not** "0 used": there is no root, no invocation row and no counter to read, and
  nothing is back-filled from the new rules.
- `model` lines: one per invocation, from the stream the driver observed (see "Model
  selection"). The offline driver and runs recorded before model observation print `not
  recorded`.
- `stream` lines: one per invocation, showing where its bound prompt response fell in the stream
  and how many updates for the prompt's session followed it (see "Driver `error_code`" above).
  They read `unknown`, never `0`, for the offline driver, older runs, unbound turns and streams
  not read to their end.
- `launch` lines: one per invocation, the stored launch-surface record (see "What DSH reads on
  its own"). They read `not recorded` for the offline driver, older rows, a stop that won the
  spawn gate and an observation that failed; a recorded one prints its `dsh home`, `workspace`
  and `client` lines instead.
- `repair` lists every repair decision the run recorded, refusals included, or says `no repair
  decision recorded`.
- `dsh context` projects the stored `dsh_context` records. `not recorded` means no frozen Git
  candidate was classified (this demo is in place); otherwise each frozen candidate shows `none on
  the list` or `CHANGED n:` with the files a DSH agent started in its worktree would load as
  instructions, skills or environment. The `list` line is the list each record was classified
  against, as stored with it. The review evidence of an attempt whose record changed files ends
  in `dsh_context=changed`, a join by attempt id rather than a field of the evidence. Each record
  ends in `declaration checked` or `recorded without a declaration check (before the 2026-10-03
  ruling)`. In a record marked `declaration checked`, a listed file is one a `write_allow` entry
  names by its path (an undeclared one, or any root `.env` change, blocks the run
  `context_file_change` before a record exists); a record without that mark was written by an
  earlier build, so its files were never checked against `write_allow` and may not have been
  declared. Files already in the base commit are not listed.
- `candidate` compares the workspace against the fingerprint the run was accepted at. If it
  says `DRIFTED`, the stored `ACCEPTED` describes the candidate as it was, not the files on
  disk now; nothing has been re-verified.

## Stopping a run

`hflow cancel <run_id>` records the intent **first**, then decides from the run's state as read
*after* the intent, asks the driver, and reports what actually happened:

| Receipt | Meaning |
|---|---|
| `confirmed_stopped` + `mechanism=forced` | the managed process boundary was terminated; *local execution stopped* |
| `confirmed_stopped` + `mechanism=none` | there was nothing left to stop: never dispatched, or the client had exited and its Job was already empty |
| `still_running` / `unknown` | the stop could not be confirmed: the run stays blocked, the workspace and evidence are kept, and nothing is re-dispatched |

The receipt (`hflow cancel --json`, `CancellationReceipt` in `hflow schema`) also carries
`run_already_ended`: `true` when the stop reached a run that had already ended, so its answer
decided nothing about that run; `false` - and what every receipt recorded before the field
existed reads as - when it decided a live run.

What the stop does to the run depends on where the run is:

- **A live run** ends here. Its receipt and its terminal block - `cancelled_by_operator` for a
  confirmed stop, `outcome_unknown` for one that was not - are written in **one** statement,
  before any attempt or ledger bookkeeping. The receipt is what makes a repeated `cancel` return
  early, so it never exists without the block: if that one write fails, only the intent is left,
  and a retried `cancel` asks the driver again and ends the run. A confirmed stop then finishes
  the attempt and closes the invocation's ledger entry if it is still open; if that bookkeeping
  fails, the failure is a `dispatch:` note on a run that is already terminal - it can no longer
  leave a run `RUNNING` with its root owned forever (the note names `hflow resume <run_id>`, which
  closes such an entry from this recorded stop once the run's owner has exited, then
  `hflow ledger settle`). A receipt found on a run that is still live (a database written while
  the receipt and the block were separate writes) is not returned as if the stop had finished:
  the block it implies is re-applied, with a "re-applied" `cancel_target` note, and the driver is
  not asked again. On a run with no ledger (no root), a stop recorded between the attempt's
  reservation and the implementer's registration wins: the registration is conditional on it, no
  driver is asked, the attempt ends `CANCELLED`, and the receipt says the reserved turn and
  authorization submission stay consumed.
- **A run that already ended** (`BLOCKED` with any code, or `CANCELLED`) keeps its outcome. The
  invocation it was on is still asked to stop - a child can outlive an unknown or failed result -
  and the receipt (with `run_already_ended: true`), a `cancel_target` note and an "already
  BLOCKED (code)" note are recorded, but the block is not relabelled and neither the attempt nor
  any ledger entry is touched. An `outcome_unknown` run therefore keeps its code and its
  `unknown` entry, and `resume` still reconciles it after `hflow cancel`. Because such a stop
  decided nothing, an `unknown` answer to it - which is what `hflow cancel` gets from another
  process - does not make `hflow clean` refuse `stop_unconfirmed`: the run faces exactly the
  cleanup gates it had before the stop, and the preview says why. A `still_running` answer still
  refuses.
- **Two stops that race** on one live run: the first to write ends the run, and its receipt and
  block are the stored ones. The later stop returns its *own* receipt with `run_already_ended: true`
  (a confirmed forced stop keeps that fact there and in its `cancel_target` note), but it does not
  relabel the block or touch the attempt or any ledger entry, exactly like a stop of an ended run.
- **An accepted run** is not reopened; the stop is recorded (`confirmed_stopped`,
  `mechanism=none`, `run_already_ended: true`), nothing is asked to stop and history is not
  rewritten.

**`hflow cancel` from another shell reports `unknown`.** The CLI is a different process from the
controller executing the task, and it holds no handle to that controller's child. It builds an
observer per role from the run's recorded configuration, so the receipt names the recorded
driver (`driver=acpx-dsh-acp, agent=dsh`, or `unrecorded` for a run with no recorded
configuration), never the offline fake. It records the intent and an `unknown` receipt
(`mechanism=none`), and a live run blocks `outcome_unknown` ("stop could not be confirmed ...
work may still be running"). It settles no ledger entry and does not cancel the attempt; the
owning controller's late result changes nothing, and `hflow resume` reconciles the run once that
controller has exited. The root stays blocked only while the stopped invocation's ledger entry is
still open. A cancel that lands during the checks or the review handoff (the implementer's entry
already `settled`, no reviewer registered yet) leaves no open entry, and the run, now `BLOCKED`,
no longer counts as the root's live run, so the root accepts a new revision immediately, even
while the original controller is still finishing its checks (for an in-place run, in the same
workspace). This holds for offline runs too: the offline fake confirms only the stops of
invocations its own instance started, so a cross-process cancel of an offline run is `unknown` as
well. The owner noticing, while it collects, a stop that another process recorded is not built,
and neither is keeping a root busy for as long as its run's controller process is alive (the
owner identity of storage v6 is recorded, but the root rule does not read it).

The stop goes to the **role that is running**, not always to the implementer: while the review
phase is live it names the reviewer's own invocation and asks the reviewer's driver (a profile
may bind the two roles to different drivers). Which role, driver and invocation were asked, and
what the stop reported, is recorded as a `cancel_target` line in the run's own note table
(`run_notes`, alongside the effective configuration and the role input packets); apart from the
`git_metadata` warnings (the `git metadata` lines), the run's notes are not part of the
`status`/`report` projection, so read them from the store.

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
refunded - but no process is started for it. A driver that reports no spawn fact (one without
`on_spawn`) and returns a cancelled invocation with no work is recorded `not_started` only when
the stop was already recorded when the controller handed it the invocation; a stop recorded while
that role was running proves nothing about a launch, so the entry stays `requested` - a confirmed
stop then records `launch_unknown`, an unconfirmed one leaves it for `resume`, which records
`launch_unknown` too.

**A client that has exited is not an empty boundary.** A stop for an invocation whose client
already exited asks the Job: it is `confirmed_stopped` with `mechanism=none` when the Job is
already empty (including after the result was collected), `forced` when the stop had to terminate
descendants the client left, `still_running` when the Job cannot be emptied (the Job stays open so
it can still be observed), and `unknown` when the Job was closed earlier without being confirmed
empty. The same applies without a stop: when the client exits, collecting its result terminates
whatever is still in the Job and adds `descendants_terminated_after_client_exit: N` to the
result's limitations, and a Job that cannot be emptied makes the result `OUTCOME_UNKNOWN` /
`boundary_not_empty`. Every invocation's Job handle and its `stdout.ndjson` handle are closed when
its result is collected, including after a deadline teardown.

**A process Windows will not open is not a process that has gone.** A live stop is confirmed only
when the Job is empty *and* the client process itself is shown to have exited: the driver's own
`Popen` handle reports its exit, its process object is signalled, or `OpenProcess` answers
`ERROR_INVALID_PARAMETER` (no process has that id). `ERROR_ACCESS_DENIED`, any other failure, and
a wait that fails are unanswered, not gone. A stop whose Job emptied but whose client exit neither
`Popen` nor a re-opened pid shows is `unknown` with `mechanism=none`, never reports
`local_process_stopped=true`, and blocks like any unconfirmed stop. An in-process reconcile
reports a client that its own process handle still sees running as `still_running`. The
unanswered path is exercised with an injected `OpenProcess` failure in the offline tests; it has
not been observed on a real client.

Once an intent is recorded the run's stop state is final: acceptance refuses, and a later
failure (a transport error, a `review_protocol_error`, a rejected review) is recorded against
the attempt without relabelling the run - an unconfirmed stop stays `outcome_unknown`, a
confirmed one stays `cancelled_by_operator`. A result of **either role** that arrives after a
stop - confirmed or not - is a fact, not a decision: the result's first write is conditional on
the stop in the same statement, and a refused write leaves it only as a `late_result` note. The
attempt keeps the state the stop left (for an implementer, `ACTIVE` after an unconfirmed stop and
`CANCELLED` after a confirmed one), the ledger entry is **not settled** and keeps its state (an
unconfirmed stop leaves it open, so it keeps blocking the root until `resume` marks it `unknown`;
an entry the confirmed stop already closed keeps that state, with the refused settlement noted),
and the run keeps the stop's block. An implementer's late result creates no candidate commit and
no `refs/hflow/candidates/*` ref; a reviewer's records no verdict, no review evidence and no
process identity. A stop that
commits after the result was written keeps the applied result; for the implementer, a stop that
lands after the result was applied but before the freeze still prevents the freeze and the ref.
A stop that lands while the acceptance is being written makes the acceptance write refuse the late
success in its own transaction: the run is left in the state the stop recorded, with no receipt,
and `hflow run` reports that outcome (with a note saying so) instead of exiting with a traceback.

`resume` then reconciles such a run: it records what it observed and closes every entry still
open as `unknown` (a launch was reported) or `launch_unknown` (no launch recorded), with the detail
"no result was applied for this invocation - none was observed, or one arrived after the run's
stop and is recorded as a late_result note; reconciled by an operator. The consumption stands and
this root does not re-dispatch."

A confirmed stop is **not** a rollback, **not** a successful protocol cancellation, and
**not** a known business result. Cooperative cancellation (`session/cancel`) is unsupported by
this launcher. The pinned acpx `exec` path does send `session/cancel` (and waits 2.5 s) when its
own process receives SIGINT, SIGTERM or SIGHUP, but HFlow starts the client with
`CREATE_NEW_PROCESS_GROUP`, which disables Ctrl+C for that group on Windows, a Ctrl+Break reaches
Node as SIGBREAK, which acpx does not handle, and Windows has no external SIGTERM/SIGHUP. M0
observed exactly that: a CTRL_BREAK killed the client (exit `0xC000013A`) before any cancel
reached the agent. The rest is reasoning from Win32 and Node semantics, not a measured delivery.
So the only mechanism available is forced teardown of the boundary. That teardown has been
recorded as passing once, for one machine, one acpx/DSH version and one binding (M2 trial A,
with the client in `approve-reads` mode); it does not extend to another platform, to a process
that leaves the boundary, or to remote billing. Unattended production execution therefore stays
disabled.

An accepted cancellation cannot be overwritten by a late success: acceptance refuses while a
cancellation intent is recorded, in the same transaction that would have written the receipt.

## What is not safe yet

- **No sandbox.** A worker and `command` checks run as ordinary child processes with your
  user's rights. Scope is detected after the fact and the candidate is refused, but the write
  already happened. Nothing confines credentials either: an approved check gets an allowlisted
  environment, but runs with your rights and can read whatever you can read, so keep project
  checks free of anything that needs a secret.
- **The managed worktree is a workspace boundary, not a permission boundary.** With
  `"workspace": {"mode": "worktree", "base_commit": "<sha>"}` a run works in a detached
  worktree beside the repository, your HEAD/index/working files/stash/branches are left alone,
  and the frozen candidate is kept under `refs/hflow/candidates/...`. It still shares the
  source repository's Git objects and admin files, and a process running as this user can read
  and write outside it. With `mode: in_place` a run edits the project root directly.
- **The reviewer is not sandboxed.** It is a separate process, session and invocation and it
  never inherits the implementer's write permission, but it reviews the same checkout with
  `isolation=prompt_only` recorded. A prompt-level instruction is not an enforced boundary.
- **The implementer's harness permissions are broad.** Under `approve-all` every DSH permission
  request is approved, sandbox escalations included (documented, not observed; see "What the
  harness does that HFlow does not control").
- **A stop is local.** Closing the managed process boundary ends the processes it owns; it is
  not a cooperative protocol cancellation (unsupported by this launcher, see "Stopping a run"),
  it does not follow a descendant that leaves that boundary, it exists on Windows only, and it
  says nothing about a remote model request or remote billing having stopped. A `hflow cancel`
  from another process cannot stop a child at all; it reports `unknown`.
- **Repair is bounded, explicit and narrow.** One repair per run, only under a task's
  `repair_policy` (and, for a real driver, a root budget), only from a clean declared business
  check failure or a substantive reviewer rejection with at least one usable finding, and never
  for a timeout, a lifecycle defect, an undeclared exit code, a malformed review, an unknown
  outcome or a cancellation (see "When a repair may happen"). It is not a `hflow repair` command,
  and it cannot revive a historical `BLOCKED` run.
- **No billing observation.** Cost and token fields are `null`; do not read `null` as 0.
- **Authorization is trusted-local.** The artifact records a human decision and bounds its
  consumption, but its provenance is not authenticated and a fresh authorization id resets the
  allowance; the executor is trusted not to forge approvals or edit the ledger. Until that
  changes, unattended execution stays disabled.

## Recovering from a controller crash

The durable facts are the `runs`, `attempts` and `invocations` rows. What survives depends on how
the controller ended:

- **Interrupted (Ctrl+C / `KeyboardInterrupt`, `SystemExit`) while an implementer or reviewer
  invocation was starting or running.** Before the exception propagates, the controller writes,
  best effort and in this order: the run's block `outcome_unknown` ("controller interrupted
  during <role> invocation; its result was never observed"), the run's open ledger entries as
  `unknown` (a launch was reported) or `launch_unknown` (no launch recorded), and the attempt as an
  unknown outcome. The root stays blocked and nothing is refunded. `hflow resume <run_id>` then
  reconciles: it records what it observed (`reconcile_json`, outcome `unknown`) and does not
  re-dispatch. A second Ctrl+C during those writes can still interrupt them.
- **Killed hard** (process kill, power loss, a crash outside a driver start, or an interrupt
  during the checks or the freeze). Nothing is recorded at the time: the run stays `RUNNING` with
  its attempt row, reservation and recorded owner (token, pid, creation time, host, claim
  generation). `hflow resume <run_id>` then takes it over **only** if that owner is proven gone:
  its lock file `<ledger dir>/owners/<token>.lock` can be locked and its pid plus creation time no
  longer name a running process. The takeover blocks the run `owner_lost` (exit `3`) in one
  transaction - open ledger entries become `unknown`/`launch_unknown`, live attempts finish as
  unknown, the claim generation increments so the old owner's fenced writes fail - and records a
  probe of each child pid the run recorded ("may still be running; nothing was stopped", or "reads
  gone", never a confirmed stop). If the lock is held or the owner reads `matching` or `unknown`
  (access denied, another host, off Windows), `resume` prints "owner may be alive", changes
  nothing and exits `5`; run it again once the owner has exited. Nothing re-dispatches
  automatically, and there is no timeout: a lease expiry alone does not prove the worker stopped.
  A run recorded before storage v6 has no owner token. A controller pid its attempts recorded
  carries no host, so it reads `unknown` and such a run is never taken over: `hflow cancel` (which
  blocks it `outcome_unknown`) followed by `hflow resume` is the way out. One that recorded no
  controller process reads `not_recorded` and is taken over only when nothing was dispatched (see
  the README's "Owner lease" bullet for the exact rule and its limits). An `hflow run` interrupted
  in setup (no attempt, no invocation) needs no `resume`: once its owner is provably gone, resubmitting
  the same TaskSpec may adopt the run and continue it only under its original admission binding.

The internal `RunAdmissionBinding` is written with the run in its creation transaction. It fixes
the project root, Git common directory, managed workspace path and resolved base, project
contract, effective configuration and launch-content digests, drivers, deadline, and root
binding and limits. It is checked before claiming or adopting any never-dispatched run. An
explicitly absent effective configuration from an offline API caller is a recorded value, not
a missing historical binding. A fresh authorization may cover the same execution: its id is not
part of this binding.

A missing, unreadable or changed binding returns the existing run unchanged with exit `5`; no workspace is
created, no owner is claimed or adopted, no authorization is consumed and no dispatch is bought.
Restore the original configuration and inputs, or `hflow cancel <run_id>` and submit a new task
revision. A new revision still faces the root's unresolved entries, deadline and repair allowance.
HFlow never reconstructs a historical admission binding from notes, and does not replace an
unreadable binding with a fresh admission. `status`/`report` use exit `6` for unreadable structured
records; that presentation error does not widen the continuation path or grant a new allowance.

`resume` and `cancel` build their observer per role from the run's recorded configuration (see
"Stopping a run"), so their records name the recorded driver, never the offline fake for a
production run, and `--data-dir` is honoured for any scratch they write.
