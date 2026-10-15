# HFlow

A small deterministic controller for harness-agnostic agentic development tasks.
The controller owns admission, budget, evidence, and delivery accounting; a native
Harness (DSH first) owns reasoning and tools.

**Status: one thin production Driver plus an offline fake; offline vertical slice and the M2
offline delivery slice; the batch E1 root ledger and the batch E2 bounded repair, offline-tested;
the batch F hardening from the 2026-10-03 upstream survey (turn settlement, reviewer transcript,
stop confirmation, shared Git metadata, launch and context records), offline-tested;
immutable admission and workspace records, guarded cleanup and bounded wire metadata;
batch I: controlled integration of an accepted candidate into a local branch (`hflow integrate`)
and the close-out of ledger entries an ended run left open, offline-tested;
three recorded live top-level M2 tasks (one stop trial, two attempts at one small change) under
explicit one-time authorizations.**

```text
transport_selection      = acpx-dsh-acp        (decided in M0, with bounded live evidence;
                                                acpx pinned at 0.17.1)
driver_implementation    = implemented         (src/hflow/drivers/acpx_dsh.py, selected in
                                                src/hflow/drivers/selected.py)
basic_live_roundtrip     = passed              (recorded M0 evidence: one prompt turn, real DSH)
live_cooperative_cancel  = unsupported         (acpx exec sends session/cancel only on its own
                                                SIGINT/SIGTERM/SIGHUP, which HFlow cannot deliver
                                                to its CREATE_NEW_PROCESS_GROUP child on Windows;
                                                never exercised, so also unproven)
model_selection          = documented          (a profile's model is passed as acpx --model;
                                                offline-tested against a mock agent only, no live
                                                DSH round trip)
forced_local_stop_offline = passed             (stubborn stub + Job Object teardown)
forced_local_stop_live   = passed              (M2 trial A, recorded for one machine + one
                                                acpx/DSH version + one binding; not a sandbox,
                                                cancellation or remote-billing claim)
m2_offline_delivery      = passed              (Git worktree -> frozen candidate -> checks -> local receipt)
m2_live_original_run     = BLOCKED / review_rejected on build 3dbfeae; no receipt of its own
m2_live_later_decision   = ACCEPTED / LOCAL_CANDIDATE, recorded offline during a later
                           reprocessing of that run's own evidence; the original decision is
                           preserved, not overwritten or re-run
local_integration        = implemented, offline-tested (hflow integrate: one checked commit on a
                           local branch, compare-and-set, never a checked-out branch; no model,
                           no push; never exercised after a live run yet)
unattended_execution     = disabled
new_live_budget          = 0                   (each live task needs its own explicit approval)
```

The driver can launch, observe, force-stop and reconcile one invocation through a managed
process boundary; the controller still owns admission, budget, attempts, evidence and the
receipt. `docs/adr/0001-transport.md` and `docs/m0-results.md` hold the M0 transport evidence,
`docs/m2-live-acceptance-result.md` the live M2 executions, and
`docs/m2-review-wire-repair.md` the offline recovery. That recovery is **not** an uninterrupted
live run: it re-runs nothing and adds no model submission.

## How to read a claim in these documents

Four kinds of statement appear below, and they are not interchangeable:

| Kind | Means | Where the backing is |
|---|---|---|
| implemented | the code path exists in this tree | the module named next to the claim |
| offline-tested | a test or an offline tool covers it, with no model call | `tests/`, `tools/m0_probe/real_client_checks.py` |
| live evidence, one binding | a recorded real execution on a specific machine, client and build | `docs/m2-live-acceptance-result.md`, `docs/m0-results.md` |
| unsupported / unverified | not implemented, or never exercised - never to be inferred from a neighbouring row | the "Not implemented" section, a driver capability record, or a run's own notes |

A recorded live result is scoped to the artifacts named beside it. It does not become a
property of "the controller", and upgrading a dependency invalidates it until re-checked.

## What works today

```text
MachineProfile (per role) ─┐
TaskSpec ──────────────────┼─> one resolution ─┬─> hflow prepare   (zero model, no state)
ProjectConfig ─────────────┘                   └─> hflow run       (same config, same binding)
                                                   -> deterministic admission
                                                   -> SQLite create-or-reuse run
                                                   -> transactional budget reservation
                                                   -> driver invocation per role
                                                   -> verification evidence over a frozen candidate
                                                   -> independent review verdict
                                                   -> controller-generated ResultReceipt
                                                   -> status / report

ACCEPTED / LOCAL_CANDIDATE ──> hflow integrate prepare   (one commit on the target's tip, checked
                                                          again in a worktree of its own)
                           ──> hflow integrate apply     (operator approval: compare-and-set of a
                                                          branch nobody has checked out)
                           ──> IntegrationReceipt        (INTEGRATED; the run's receipt is kept)
```

Admission refuses a spec it cannot honour - an unanswered or failed reuse/adapt fit test, a
delivery level this build cannot reach, a non-empty `dependencies` list this build cannot
schedule, a real delivery whose approved checks are `kind=fake`, a glob in `write_allow` or a
`write_allow` entry under `.git`/`.hflow`/`.acpxrc.json`, a write task with no isolated worktree
or no write opt-in, a task that needs a review but reserves only one turn, and a real-driver
repair policy without a root budget - instead of warning and delivering less.

A machine profile (`<data-dir>/profiles/<id>.json`) binds `implementer` and `reviewer`
independently: agent, transport, model selection and capability record per role. An unknown
profile, an unreadable one, a role the profile does not bind, a driver name this build does not
implement, a harness no implemented driver launches, or a profile that mixes the offline fake
with a real transport all **refuse** - there is no default binding and no fallback. `hflow
prepare` resolves exactly what `run` will use and prints it before anything is dispatched,
including the **resolved launch** (client entry point, interpreter, launcher argv, the `--model`
value when the profile names one, DSH home/profile) and the base commit resolved to a SHA.

Each invocation also receives a complete **role input packet** rendered by the controller from
recorded facts (goal, acceptance criteria, write scope, candidate identity for the reviewer,
program evidence, output contract), bounded at 32 KiB and bound to its dispatch by a prompt
digest. The bound is checked against the packet the run would actually send, and a run that
cannot cover its own fixed loop - in budget or in remaining authorization - is refused before the
first invocation. See "What each role is actually told" in `docs/operations.md`, which also keeps
the three kinds of transport evidence apart (offline fake driver / production driver with a Python
stand-in for acpx / the installed pinned acpx).

## Install and test

Python 3.12+ (developed on 3.14.7). Runtime dependency: `pydantic>=2.12,<3`.
Development dependencies: `pytest>=9.0,<10` and `pytest-xdist` (`python -m pytest -q -n 8`; on the
development machine `-n 16` exhausted the Windows page file, WinError 1455). Git 2.31 or later for worktree runs (`git rev-parse
--path-format=absolute`; `git config --show-scope` needs 2.26). The global attributes file is
located with `git var GIT_ATTR_GLOBAL` on Git 2.42+, and on older Git from `core.attributesFile`,
else `$XDG_CONFIG_HOME/git/attributes`, else `~/.config/git/attributes`. `hflow integrate` needs Git
2.40 or later (`merge-tree --write-tree --merge-base`) and refuses an older one.

```sh
python -m pytest -q
python -m pip install -e .     # optional: installs the `hflow` console script
```

Tests need no model, no network, and no DSH: they use the offline fake driver, a test-only
stand-in for the acpx client (`tests/fixtures/fake_acpx_client.py`) and stub ACP agents that
are real separate processes (`tests/fixtures/stub_acp_agent.py`). Every test runs with
`HFLOW_DATA_DIR` pointed at its own temp directory (`tests/conftest.py`), so the suite never
writes your real data directory, and with a stand-in `dsh` launcher first on `PATH` (an npm-style
`dsh.CMD` on Windows, an executable `dsh` elsewhere) that only exits 1 and is never run: a real
launch now needs `dsh` as an absolute file on `PATH`, so without it whether a test's launch
resolves would depend on - or find - a real DSH install.

Some tests skip themselves when their precondition is absent rather than pretending to pass. On
this machine one does - the directory-link test - and a run that reports more is telling you which
preconditions were missing, not that the suite failed:

| Precondition | Tests | Where |
|---|---:|---|
| the OS lets this user create a directory link | 1 | `test_contracts.py` |
| `node` on `PATH` and the pinned acpx at `.probe/acpx` (gitignored) | 13 | all 11 in `test_real_client_review.py`, 2 `real_acpx` cases in `test_packet_wire.py` |
| the recorded M2 ledger at `.probe/m2-live/attempt-2-data` (gitignored) | 20 | all 9 in `test_saved_review_replay.py`, 11 in `test_local_finalization.py` |
| Windows (a Job Object, or the batch-shim launch) | 26 | all 9 in `test_winjob.py`, 5 in `test_check_process_lifecycle.py`, 9 in `test_driver_acpx_dsh.py`, 2 in `test_batch_e_repair.py`, 1 in `test_batch_e_verify.py` |
| Git 2.48+ (`worktree.useRelativePaths` records `extensions.relativeWorktrees`) | 1 | `test_m2_slice.py` |
| a locale whose encoding cannot decode every byte (UTF-8 or GBK; not a single-byte code page) | 4 | 1 in `test_m2_slice.py`, 3 in `test_batch_e_repair.py` |
| the temp directory does not lie inside a Git checkout | 1 | `test_dsh_surfaces.py` |

The acpx and ledger counts were taken on 2026-10-03 by running the affected files with each
`.probe/` directory moved aside. A fresh clone on another platform, without `.probe/`, therefore
skips up to 60 tests, one more with a Git older than 2.48, up to four more in a single-byte
locale, and one more when the temp directory lies inside a Git checkout.

## Commands

```sh
hflow init project                        # writes .hflow/project.json: `command` checks inferred
                                          # from pyproject/package.json (or --check ID=COMMAND)
hflow init profile dsh-local              # writes <data-dir>/profiles/dsh-local.json (both roles)
hflow init task T-1 --goal "..." --allow src/x.py   # writes T-1.task.json; the reuse fit test is
                                          # left for you to answer, so admission refuses until then
hflow doctor   --json                     # read-only environment probe, no model calls; never
                                          # runs dsh; integrate/acpx/dsh/credential readiness
hflow doctor   --profile dsh-local        # resolve a profile per role; non-zero if unusable
hflow prepare  --task t.json --profile dsh-local   # zero-model preview: config, admission,
                                                   # scope, checks, budget, packet preview
hflow prepare  --task t.json --profile dsh-local --plan-out t.plan.json
                                          # the same preview, saved as a plan
hflow approve  --plan t.plan.json         # at a terminal: shows the plan and its approval code,
                                          # you type the code and your own approval text
hflow approve  --plan t.plan.json --run   # the same approval, then `run --plan` in this terminal
hflow run      --plan t.plan.json         # every input from the plan; a stale plan is refused
hflow run      --task examples/task.json --project examples/project.json --project-root . --driver fake --json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json --json
hflow prepare  --task t.json --profile dsh-local --root-budget-file root-budget.json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json \
               --root-budget-file root-budget.json   # spend against a root ledger (batch E1)
hflow prepare  --task t.json --profile dsh-local --repair-policy-file repair.json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json \
               --repair-policy-file repair.json      # opt in to ONE bounded repair (batch E2)
hflow runs     [--needs-you] [--json]     # every run, newest first, with its first next: line;
                                          # --needs-you adds live runs (owner, passed deadline)
hflow status   R-xxxxxxxxxx               # pure SQLite read, zero model calls
hflow status   --last                     # the newest run (status, report and diff only)
hflow diff     R-xxxxxxxxxx [--stat]      # the recorded base..candidate commits (Git identity,
                                          # not the content fingerprint); never a working tree
hflow report   R-xxxxxxxxxx --json        # receipt + evidence + the config it ran under
hflow resume   R-xxxxxxxxxx               # reconcile an interrupted attempt, or close the
                                          # open ledger entries of an ended run - both once
                                          # its owner is gone; never re-dispatches
hflow resume   R-xxxxxxxxxx --legacy-owner-gone --attest "<what you know and why>"
                                          # a pre-v6 ended run: close its open entries on
                                          # your attestation that its controller exited
hflow cancel   R-xxxxxxxxxx               # for a run another process is executing: records
                                          # the stop, reports unknown, blocks outcome_unknown
hflow clean    R-xxxxxxxxxx               # preview releasing the run's worktree
hflow clean    R-xxxxxxxxxx --apply       # remove it; candidate ref and receipt (if any) kept
hflow integrate prepare R-xxxxxxxxxx --target main
                                          # build + check one integration commit; moves nothing
                                          # (--project <file> unless .hflow/project.json is
                                          # still the contract the run was admitted under)
hflow integrate apply G-xxxxxxxxxx --expect-target <tip>
                                          # your approval: compare-and-set of the branch
hflow integrate reconcile G-xxxxxxxxxx    # settle an interrupted or hand-merged integration
hflow schema                              # generated JSON Schema, MachineProfile included
```

**The short path for one task:** `init project` / `init profile` / `init task` once, then
`prepare --plan-out` → `approve --plan --run` (or `approve --plan`, then `run --plan`), then
follow the `next:` lines each command prints (`integrate prepare` → `integrate apply` → `clean`;
after an interruption `resume` → `ledger settle`). A printed `next:` line is read from stored facts
only and never re-runs a task or reuses an approval. The plan saves `prepare`'s inputs, so `run`
no longer has to repeat every flag; `approve` and `run` re-resolve it and refuse it once anything
it covers has changed. Writing the authorization file by hand and repeating the flags still works
(`docs/operations.md`, "From preview to run").

`prepare` is the intended first step for any task: it answers "what would this run actually do?"
without a model, without a run row, without a workspace and without an authorization. It exits
`2` when admission **or a dispatch precondition** would refuse, and still prints the whole
preview. It never mints an approval - the binding it prints is what an approval would have to
cover (`creates_authorization` is pinned to `false`).

`--profile` (or `HFLOW_PROFILE`) is the only source of per-role bindings, and `--driver` must
agree with it when both are given. Neither present means `--driver fake`, the offline default.
Precedence is defined once, in `profiles.requested_profile_id`.

`--repair-policy-file PATH` (on `prepare` and `run`) is the explicit repair opt-in: a bare
`RepairPolicy` document that becomes part of the effective TaskSpec - and therefore of
`spec_digest` and the authorization binding - so the repair it arms is the repair that was
approved. A task that already names a different policy is refused rather than one silently
winning (the same policy written twice is fine). A missing file, a non-object or a
contract-invalid policy is refused **before the store is opened**, so no run row, no worktree and
no authorization is created. With a real driver the repair policy also needs `--root-budget-file`
(and a root whose `max_repairs` covers the repair); `prepare` reports a missing root, and a
root file with `max_repairs` 0, under `dispatch_preconditions` and exits `2`.

Runtime data (SQLite, evidence, profiles) goes to `%LOCALAPPDATA%\HFlow` on Windows or
`$XDG_DATA_HOME/hflow` elsewhere; override with `--data-dir` or `HFLOW_DATA_DIR`.
It never lands inside a project checkout.

Exit codes: `0` accepted, `2` refused at admission (no run state is created), `3` blocked after
dispatch, `4` usage (including every argument-parsing error), `5` the run exists and is not
finished (`DRAFT`/`READY`/`RUNNING`/`CHECKING`, e.g. a submission that found the run claimed by
another owner process, `resume` refusing a takeover because that owner may be alive, or an
undispatched run whose recorded admission binding is missing or differs from this submission).
`resume` also exits `5`, whatever the run's own state, whenever it refused on the owner rule and
wrote nothing: an `outcome_unknown` run whose owner may still be alive, or an ended run whose open
ledger entries it may not close yet (an owner that may be alive, or a pre-v6 owner nobody attested
gone) - never the `3` or `0` that would read as "done". It also exits `5` when one of its store
writes failed (`database is locked`, for one): stderr says so, that transaction was rolled back
whole (an ended run's entry closure and the attestation it records commit together), and `resume`
can be run again.
`6`: `status`/`report` found the run but a stored record it needs no longer validates
(`StoredRecordUnreadable`); one line names the record and no replacement record is inferred.
`cancel`/`resume` can still work from the run row without those presentation records. Any
command that hits a ledger error it does not map itself prints one line instead of a traceback:
`5` when the ledger is locked by another process (run it again), `6` otherwise (a storage version
newer than this build, a file that is not a ledger, a write the ledger refused). A lookup
(`status`, `report`, `diff`, `cancel`, `resume`, `clean`, `integrate`, `ledger settle`) never
creates a ledger: an unknown id exits `4`, naming the ledger file it looked in and `--data-dir`.
A `CANCELLED` task state, which this build never writes, would exit `3`. `hflow integrate` uses the
same codes for its own record: `0` when the subcommand did what it was asked (`prepare` ready,
`apply` integrated, `reconcile` ready or integrated), `2` refused (nothing written), `3` a state
that needs your decision (conflict, failed checks, stale, interrupted, a hand-off because the
branch is checked out, or a ref update that failed), `4` unknown id, `5` another process is or may
still be working on it, `6` an unreadable stored record (see `docs/operations.md`, "Integrating an
accepted candidate").

`130`: interrupted - a Ctrl+C reached the command (it was `4` before). The process stopped where it
was and retried nothing; what it recorded before the interrupt is authoritative, so read it with
`hflow status <run-id>` (or `hflow integrate show <integration-id>`). `hflow doctor` exits `0`, or
`2` when a named profile cannot be used; its readiness lines - whether `hflow integrate` can run
with the Git on PATH (it needs Git 2.40), where the acpx entry resolves from (never PATH:
`HFLOW_ACPX_CLI`, then `<data-dir>/m0/acpx`, then the checkout's `.probe/acpx`; a profile cannot
name it), where `dsh` is (path only, never run: PATH, else the npm CLI `@deepseek-ai/dsh@0.2.0-rc.2`
under `<data-dir>/dsh/0.2.0-rc.2`; with neither, the `npm install --prefix ... --ignore-scripts`
command that puts it there - a DSH Desktop shim is named but not recommended, because of DSH #9626)
and the credential sources a child could have (`DEEPSEEK_API_KEY` by name,
a bound `DSH_HOME`'s `.credentials.yaml` and `.env` by stat), saying plainly when a real launch
would be refused for having none (`no_credential_source`) - never change it.

Text output of `run` refusals and `resume` is `key: value` lines (a list as `- item` lines, a
nested record as one compact JSON line); `--json` is unchanged canonical JSON.

`python -m hflow` is the same entry point as the `hflow` command (`src/hflow/__main__.py`): after
`python -m pip install -e .` it works from anywhere; without installing, put `src` on the module
path from the checkout root:

```sh
PYTHONPATH=src python -m hflow run --task examples/task.json --project examples/project.json --project-root . --driver fake
```

```powershell
$env:PYTHONPATH = "src"; python -m hflow run --task examples/task.json --project examples/project.json --project-root . --driver fake
```

## Data ownership (three kinds, kept apart)

| Kind | Location | Notes |
|---|---|---|
| Project contract | `<repo>/.hflow/project.json` | approved checks, deny paths, limits; versioned with the project |
| Machine binding | `<data-dir>/profiles/<id>.json` | `MachineProfile`: per-role agent, transport, model selection, limits. Loaded by `profiles.py`; `hflow prepare` / `run --profile` / `doctor --profile` all resolve it. Never versioned with a project, and never read from inside a checkout |
| Runtime data | platform data dir | SQLite plus evidence references; outside the repo |

`contracts.py` is the single definition of every structure except the authorization artifact
(`AuthorizationRecord` / `AuthorizationBinding`), which lives in `authorization.py`. JSON Schema is
generated from both (`hflow schema`), including every document a user writes by hand - the task,
the project contract, the machine profile, the authorization artifact, the root budget file
(`RootBudgetPlan`) and the repair policy file (`RepairPolicy`). The JSON examples in
`docs/operations.md` are illustrations; the generated schema is the shape.

## Non-negotiables in the code

Each item states the rule; the detail lives in the `docs/operations.md` section it names.

- The controller generates `ResultReceipt`; a worker cannot report `ACCEPTED`.
- Budget is reserved in the same transaction that records the dispatch.
- A **root budget** (batch E1) is one ledger row per `(project_id, repository, task_id)`, across
  revisions. Its ledger path is part of the authorization binding (pointing `--data-dir` elsewhere
  is refused, not a second allowance), and every top-level dispatch is reserved with its attempt
  row and counters in **one** transaction. A root runs one task at a time, a dispatch must leave
  allowance for the rest of its run's loop, one attempt records one review, and every unresolved
  ledger entry (`reserved`, `requested`, `started`, `unknown`, `launch_unknown`) blocks every later
  dispatch of the root - also a run started without `--root-budget-file`. A launch requested and
  never reported back is `launch_unknown`, never `not_started`, and settling one twice (a
  reconcile, then a confirmed stop) is idempotent. `max_repairs` is a **ceiling, not a switch**.
  An offline root run without `--authorization-file` is charged to a labelled `AUTH-offline...`
  record (`origin: cli_offline_synthetic`, binding `driver: fake`) that can never authorize a real
  transport. See "Root budgets".
- **Open and unknown ledger entries close only through `resume` or an operator attestation,
  never through a re-dispatch or a refund.** `hflow resume` closes an ended run's open entries
  once its owner is provably gone (batch I1); `hflow ledger settle` (ruling 2026-10-03) closes one
  `unknown`/`launch_unknown` entry as `operator_settled` (`void` only for `launch_unknown`);
  `--legacy-owner-gone --attest` (batch J) attests a pre-v6 owner HFlow can never probe. None of
  them changes the run's state, block, receipt or outcome, an attestation is never recorded as an
  observation, and there is no time-based expiry. See "Root budgets".
- **`resume` reconciles an `outcome_unknown` run only once its owner is gone** (batch J): a
  cross-process `hflow cancel` blocks the run while its controller and agent may keep running, so
  until then `resume` writes nothing and exits `5`. See "Stopping a run" and "Owner lease" below.
- **A repair is classified from a stored execution fact, never from prose** (`evidence.exit_reason`
  says the check ran to completion, the policy declares the exit code, no ERROR in the round). An
  empty pre-v5 reason never repairs, every decision is a stored `RepairRecord`, and the schema
  allows one first and one repair attempt per run, revision and role. See "When a repair may happen".
- Identical TaskSpec does not buy a second worker turn. Note what that means: the reply
  is the **historical** run, and `status`/`report` print a `candidate` line saying whether
  the scoped files still match the fingerprint that run was accepted at. A historical
  `ACCEPTED` is never presented as verification of the current working tree.
- Implementer and reviewer are separate driver processes with separate reserved turns;
  `run` reports `implementer_invocations` and `reviewer_invocations` separately. Neither
  number is a model-request count, and no field claims to know billed usage. Separation is
  process-, session- and allowance-level, not a sandbox: see "Not implemented" below.
- Admission refuses a spec it cannot honour before anything exists - including a reuse/adapt
  choice whose fit test is `pending`/`failed`, a `not_required` claim with no stated reason,
  and a delivery level this build cannot reach. A refused admission (`exit 2`) creates no run
  state, consumes no allowance and dispatches nothing; it is not the same event as a run that
  dispatches and then blocks (`exit 3`).
- A reviewer's verdict comes only from that reviewer invocation's own final message (by ACP
  `messageId`, for the session its first `session/prompt` named, from a stream read to its end),
  validated against the canonical `ReviewOutput`; a reasoning block is never part of it. No usable
  or no unambiguous verdict blocks as `review_protocol_error` - a wire failure, never a request for
  changes - and the offline replay tool refuses the same streams, as well as a reviewer prompt
  answered with a JSON-RPC error or settled with a stop reason other than `end_turn`. See "A turn
  is bound to its own prompt" in `docs/architecture.md`.
- What each role is told is rendered once, from stored facts, by the controller
  (`packet.py`) and transported verbatim; a driver may not rebuild or extend it. The prompt
  digest the transport reports is compared with the packet the controller rendered, so a
  result that arrived with different input is refused instead of attributed to this task.
- Review is a floor plus a request: a project that requires review cannot be waived by a task
  (refused at admission), and a task that asks for review gets one even when the project does
  not require it. Only both saying no may skip it.
- An unknown outcome blocks and never auto-retries.
- Verification is bound to a candidate fingerprint and a checks digest.
  File contents are hashed in 64 KiB chunks with the same digest, size and path-order contract,
  so a large scoped file does not require an equally large in-memory byte string.
- **The frozen candidate is the change that was checked.** Literal `write_allow` paths only, every
  deny list enforced before anything is staged, no worker-moved HEAD or flagged index entry, no
  ignored bytecode standing in for source, the whole base-to-candidate diff held to the scope again
  after the freeze and at acceptance, HFlow's own Git calls un-hookable, and the shared Git metadata
  compared before the freeze, a repair round and acceptance. See "M2 runs through the CLI", and
  "Not verified" below.
- The implementer and the reviewer are resolved from the profile **independently** and
  dispatched through their own driver object; a role the profile does not bind is refused rather
  than inheriting the other's agent. A binding's declared **harness** must be one its driver
  actually launches, so a profile cannot record `codex` next to a driver whose every process is
  DSH.
- `prepare` and `run` derive everything - task overrides, project contract, role bindings,
  driver names, the resolved launch, write permission, admission - from one resolution
  (`prepare.resolve_run`), so a preview cannot describe a configuration the run does not execute,
  and `prepare` exits non-zero for a dispatch precondition the run would refuse on, too.
- **An authorization binds the effective configuration**, not just the task: profile, per-role
  agents, drivers and model selections, limits, write permission, the resolved launch and the
  content digests of its entry files, the project contract, the roles the run dispatches, and the
  base commit as a **SHA**. Any change stops the old approval applying; copy a fresh binding from
  `hflow prepare --json`. See "What an approval now covers".
- The agent launch argv carries only the launcher path and fixed flags. The one configured value
  on a command line is the acpx **client**'s `--model <value>`, a fixed flag under AGENTS.md rule
  9 by the user's ruling of 2026-10-02 (fixed by the machine profile, validated, digest-bound).
  Task text, nonce, user content and credentials never reach a command line.
- **No launch starts on what a repository or a shell could change**: a starting workspace with
  `.acpxrc.json` or a root `.env`, a bound `DSH_HOME` in or around the workspace, or no visible
  credential source (user ruling 2026-10-07) is refused before anything is recorded, and again at
  the spawn gate; a launch program that is not an absolute file outside the workspace makes the
  launch not resolvable. Neither the agent's child process nor the `hflow` process itself searches
  its working directory for a program (an approved check's own environment does not carry that
  setting), and ambient `DSH_PERMISSION_MODE`/`DSH_TOOLS_MODE` are removed. See "Client launch
  hardening".
- **A turn's outcome comes only from the response to its own observed prompt**; anything else -
  `unbound_completion`, `prompt_error_response`, `unknown_stop_reason`, `no_stop_reason` - is
  `outcome_unknown` for either role. A result that arrives after a recorded stop is only a
  `late_result` note. See "Reading a blocked run" and "Stopping a run".
- Model observations belong to the first valid created session; a failed protocol read
  (`reader_failed`) yields no verdict, model observation or stream ordering from its prefix; a
  check artifact error is verification ERROR evidence that ends the run `BLOCKED`.
- A run records the configuration it executed under, once; `status` and `report` read it back,
  and a run that predates it says "not recorded" instead of being back-filled from whatever is
  configured now.
- A delivery can be recorded as a **later decision** about an execution that already ended (an
  offline reprocessing of recorded evidence). Such a receipt carries `provenance` naming the
  original decision, its build and the evidence it came from, and `report` prints that next to
  the delivery - so a recovered delivery never reads as the original run's own success. The
  write only ever moves a blocked run forward, never over a cancellation intent or a different
  decision, is idempotent for the same evidence, and consumes no allowance.
- `status`, `report` and `doctor` make no model calls.

## Not implemented (do not assume otherwise)

Each item below is absent from this tree, not merely untested:

- **No sandbox.** Workers and `command` checks run with the current user's rights: no filesystem
  or credential confinement, and no protection against another process of the same user changing
  the workspace, the authorization artifact or the SQLite ledger. Scope is detected after the
  fact and refused, not prevented.
- **No cooperative (protocol) cancellation** on the selected launch path: a stop is a forced
  teardown of the Windows Job Object ("Stopping a run" says why), and off Windows a forced stop
  does not kill even the direct child.
- **No unattended execution.** Nothing in this repository authorizes a live task; each one needs
  its own explicit approval.
- **No Team, DAG or parallel tasks, and no native subagents.** A non-empty `dependencies` list is
  refused at admission, and one root runs one task at a time. The rest of what is absent by design
  is listed under "What is deliberately absent" in `docs/architecture.md`.
- **No publish delivery and no remote integration**: no push, no pull request, no remote of any
  kind, and a TaskSpec asking for `integrated` or `published` delivery is refused. `hflow
  integrate` moves only a local branch nobody has checked out (a checked-out one is handed to you
  as a `git merge --ff-only`), does not review a `replayed` tree again (it is checked), and does
  not resolve a conflict.
- **No billing observation or remote reconciliation.** Billed tokens, cost and quota stay `null`,
  never `0`, and a stop proves nothing about a remote request or bill.
- **No `hflow repair` command** and no revival of a historical `BLOCKED` run: the one bounded
  repair is a decision inside a live run under an explicit `repair_policy`, never a retry of an
  environment or transport failure.
- **`hflow init` only scaffolds**: it never answers the reuse/adapt fit test, never writes a root
  budget, plan or authorization, never overwrites a file, and infers checks only from
  `pyproject.toml`/`pytest.ini`/`setup.cfg`/`tox.ini` (pytest) and a real `package.json` test
  script. No `--format markdown` handoff renderer.
- **No reuse-research automation and no metrics against a direct-DSH baseline.**
- **No project-declared check environment.** A check sees an allowlist plus HFlow's fixed
  variables; `PYTHONPATH`, `NODE_PATH` and `NODE_OPTIONS` are not inherited, and there is no
  project or check field to declare them, so a check that needs one sets it inside its own argv.
- **No operator attestation for an owner this build recorded that reads `unknown`** (another host,
  elevated or another user, every owner off Windows): its run's open entries and an
  `outcome_unknown` reconcile wait for an owner HFlow can never prove gone; `--legacy-owner-gone`
  covers only a pre-v6 owner. Neither the owner noticing a stop another process recorded nor
  keeping a root busy for as long as its run's controller process is alive is built.
- **No upgrade of the pinned acpx** from 0.17.1 to 0.19.x (surveyed, deliberately deferred).
- **No reading of a prompt answered with a JSON-RPC error as `failed`**: it stays
  `outcome_unknown` with its code recorded, even for DSH's pre-model forms, because telling them
  apart means reading DSH's message text, and that reclassification needs a decision.
- **No structured-output request and no verdict repair**: a reviewer's answer is read as text and
  decoded against the contract, and no model is ever asked to repair a malformed verdict.
- **No per-role binding of DSH's permission or tools mode** (the ambient variables are removed;
  binding a value is an open decision), and no parsing of a `.env` (it is refused, never opened).
- **Launch files are not held open during the spawn** (no write/delete-sharing lock; Windows has
  no exec-by-handle), and the acpx, @agentclientprotocol/sdk and dsh carrier versions are only
  recorded in the launch-surface record, not bound.

## Limits and refusals

Most of what used to be listed under "Not implemented" is implemented behaviour with a stated
boundary. Its detailed home is `docs/operations.md`:

| Topic | Section of `docs/operations.md` |
|---|---|
| Commands, exit codes, `doctor` readiness | "Daily commands" |
| What `prepare` reports; what stops a run before it dispatches | "Before you run anything: `hflow prepare`" |
| What an approval covers; launch entry files bound by content, and what that leaves out | "What an approval now covers" |
| Root budgets; `resume` closing open entries; `ledger settle` | "Root budgets: `--root-budget-file` (batch E1)" |
| The bounded repair, typed findings, offline checks | "When a repair may happen (batch E2)" |
| Block codes and driver error codes | "Reading a blocked run" |
| Freeze, scope, ignored bytecode, shared Git metadata | "M2 runs through the CLI" |
| Guarded worktree release, recorded workspace identity | "Releasing a workspace (`clean`)", "Known limits of `clean`" |
| Integration into a local branch | "Integrating an accepted candidate" |
| Raw output and wire-state bounds | "Check output", "Worker log retention" |
| What DSH does and reads on its own | "What the harness does that HFlow does not control", "What DSH reads on its own" |
| `.acpxrc.json`, workspace `.env`, `DSH_HOME`, credential source, absolute launch programs | "Refusals that now happen before the first invocation", "Client launch hardening" |
| Model selection (offline-tested only: keep profiles on `native_profile`) | "Model selection" |
| Stop routing, cross-process `cancel`, the spawn gate | "Stopping a run" |
| Crash recovery, adoption, the admission binding | "Recovering from a controller crash" |

Recorded only here:

- **Owner lease (user ruling 2026-10-03).** A run is owned by a controller *process*: a random
  token, its pid, creation time (`GetProcessTimes`) and host (storage v6), plus an exclusive OS
  file lock on `<ledger dir>/owners/<token>.lock` held for the controller's lifetime;
  `--controller-id` is only a label, and a new run is created with its owner in the same insert.
  `resume` takes a live run (`DRAFT`/`READY`/`RUNNING`/`CHECKING`) over only when its owner is
  **proven** gone - its lock can be taken **and** its pid plus creation time no longer name a
  running process (exited, no such pid, or a reused pid) - in one transaction: the successor takes
  the claim and the generation increments, open ledger entries become `unknown`/`launch_unknown`,
  live attempts finish as unknown, and the run is `BLOCKED` `owner_lost` (exit `3`). Nothing is
  re-dispatched or reported `confirmed_stopped`: each recorded child pid is probed and recorded as
  an observation ("may still be running; nothing was stopped", or "reads gone"). A held lock, a
  running owner or one that reads `unknown` refuses with "owner may be alive" and changes nothing
  (exit `5`). After a takeover the old owner's dispatch reservation, result application, check
  transition, repair reopen and acceptance carry its token and generation and are refused
  (`owner_lost`). A pre-v6 run has no token: a controller pid it recorded carries no host, reads
  `unknown` and is never taken over; one that recorded no controller process reads `not_recorded`
  (not proof of death) and is taken over (block reason "no owner was recorded") only when it has
  no attempt and no invocation. `status`/`report` print the owner and a read-only liveness probe
  (`owner`, `liveness ... (lock ...)`) - this command's observation, never stored.
- **What the owner lease does not cover.** File locks on network shares or cloud-synced folders
  (a ledger under a OneDrive-synced Desktop, say) may not exclude anything, and such a location
  is not detected. An owner running elevated or as another user, on another host, or off Windows
  (no creation time is read there - documented, not observed) reads `unknown`: its run is never
  taken over, and its `outcome_unknown` block is not reconciled either. A hard-killed owner's lock
  is released promptly but not instantly, so a takeover right after a kill can be refused once (run
  `resume` again); sleep or hibernation keeps the owner alive. A pre-v6 run with a recorded
  controller pid (no host; a local lookup of another host's pid would read "no such process") can
  never be taken over - `hflow cancel`, then `hflow resume`, is its way out; one with no recorded
  controller process that dispatched nothing can be taken over as `not_recorded`, which is wrong
  only for a pre-v6 controller that opened the ledger before it was migrated and is still in setup.
  A takeover stops nothing (a child that "may still be running" keeps running until it exits or is
  ended by hand), no takeover is automatic, and a new `hflow run` on a run another owner holds only
  reports it (or adopts a provably gone owner's never-dispatched run). `cancel` does not use the
  lease, and writes other than the fenced ones (notes, evidence rows, spawn reports) are guarded
  only by the run's state and ledger rules, which a takeover sets to `BLOCKED`/`unknown`.
- **Concurrent submissions of one TaskSpec.** The claim is a compare-and-set on the owner token:
  a second `hflow run` returns the run as it stands with a note naming the owner (pid, host,
  label, generation) - exit `5`, no setup, no block. Adopting a provably gone owner's
  never-dispatched run is one compare-and-set (old token and generation to its own, generation
  + 1) that keeps it `DRAFT`/`READY` and records "adopted from a controller that is provably gone;
  nothing had been dispatched". No writer that sets `BLOCKED` relabels a run that already ended or
  carries a receipt, and a controller never records its own refusal as the block of a run whose
  attempt another controller reserved. The spawn coordination behind a stop covers the one driver
  that creates processes here (`AcpxDshDriver`); another driver must coordinate its own spawn the
  same way.
- **Launch content binding.** A launch built by hand rather than by `resolve_launch_config`
  carries no digests, so the spawn gate has nothing to compare it with. A Node or npm upgrade
  changes the digests and needs a new `prepare` and a new approval.
- **DSH context files: what the refusal does not cover.** A declared change to a file DSH loads as
  instructions is shown to the reviewer as untrusted data, but DSH still loads the candidate's
  version in the reviewer's and the repair implementer's worktree - a label, not enforcement;
  running the reviewer from a tree holding the base versions would need its own ruling. Also not
  covered: such files already in the base commit; in-place runs; a link to a directory that holds
  one; an ignored `AGENTS.md` under an allowlisted cache directory (`__pycache__`,
  `.pytest_cache`); a `.env` below the root (not on DSH's list); anything under the DSH home.
- **Typed findings do not prove a finding true.** The schema checks shape only: a `location` is
  not checked against the candidate (the path may not exist, the lines may be invented), and
  `severity` is the reviewer's own label (it gates nothing; there is no confidence field). The
  offline replay (`tools/m2_live/replay_review.py`) reads a saved answer under the untyped
  contract only when the recorded reviewer prompt did not show typed findings, and says so:
  `review_contract` is in its output and, for a replay finalized by this build or later, in the
  receipt's provenance and the review evidence detail. The one recorded finalization (run
  `R-gkb3ld97x8`, evidence `E-wln314qoor`) predates that and carries none; a repeated `--finalize`
  reports it as "not recorded" and rewrites nothing.

### Not verified

Even where something works on one binding:

- **Stopping** covers what the Windows Job Object owns and nothing more: not a descendant that
  leaves the job, not another platform, and the access-denied answer that keeps a stop `unknown`
  has only been injected in offline tests, never observed on a real client ("Stopping a run").
- **Shared Git metadata is compared, not confined**, in offline tests only (fake driver plus
  test-issued git on Windows; never against a real DSH worker; mechanism in "M2 runs through the
  CLI"). The comparison is point-in-time: a process running as you (an escaped descendant, another
  process, you) can change the metadata between a comparison and HFlow's next git command. Nothing
  is restored and there is no cross-run baseline: a later or concurrent run takes the changed
  state as its baseline, since its status read of your checkout and its `worktree add` run before
  its snapshot. False positives fail closed (your own `git config`, `push -u`, an IDE writing
  `branch.*`, a global-config edit, a check that runs `git config`), each costing a new revision -
  under a root budget, one charged as a repair. Not compared: ignore rules, system attributes
  (`GIT_ATTR_SYSTEM`, still read because `GIT_CONFIG_NOSYSTEM` does not drop it; they can only
  select a filter defined in the compared config), hooks (already neutralised), and object or ref
  metadata. A FIFO, device or directory in a metadata path is recorded by its type, never read.
  In-place runs make no git call, so there is nothing to compare.
- **Your checkout is compared by stat, not by content.** An **accepted** worktree run compares
  HEAD, the stash ref, the index entries and, for every `git status --ignored` entry, its path,
  status code, size and mtime, before dispatch and after acceptance; a difference records a
  WARNING, and the "left untouched" note is written only without one. A run that blocks or stops
  before acceptance records no after-comparison: no WARNING and no "left untouched" note, whatever
  happened to your checkout meanwhile. A rewrite that keeps both size and mtime, or a change
  inside an untracked or ignored directory git reports collapsed, is not detected.
- **Billed usage stays unknown.** An agent-reported `usage` on the prompt response (an UNSTABLE
  ACP field, its meaning disputed in ACP #1860) and the optional `cost` of a `usage_update` are
  never read into `provider_billed_tokens`, `provider_cost` or `subscription_quota_remaining`
  (offline-tested). DSH 0.2.0-rc.2's source sends neither, and the five recorded live streams on
  this machine (agentInfo `deepseek-harness-acp` 0.0.1, not rc.2) carry neither.
- **Model selection** is exercised with the pinned acpx against a mock agent only; no live DSH
  `set_config_option` round trip has been observed, so the capability stays `documented`.
- **Message boundaries without `messageId` are inferred.** For a runtime that omits ACP's
  optional `messageId`, any update that is not a message chunk (a thought included) or a sequence
  gap ends a message, and only the last such segment is read as the answer. Every message chunk
  in the recorded live DSH streams carries a `messageId`, so no recorded production run took this
  path. The order "text, reasoning, text" inside one DSH message comes from DSH source
  (`dsh-v0.2.0-rc.2`, `packages/acp/acp/src/updates.ts`); HFlow handles it in offline tests only.
- **What the harness and DSH do on their own** (permission escalations under `approve-all`,
  session-log upload, `end_turn` for blocked turns, the files and variables DSH reads at launch)
  is documented upstream, not observed here, and recorded rather than controlled; whether DSH
  expands a leading `~` in `DSH_HOME` is not verified either (HFlow refuses one).

## Driver contract tests

```sh
python -m pytest -q tests/test_cancel_routing.py         # a stop reaches the live role, wins the handoff, and is never undone
python -m pytest -q tests/test_authorization.py          # the authorized real-run gate
python -m pytest -q tests/test_driver_acpx_dsh.py        # no model, no credential: launch hardening, model flag and observation, Job teardown
python -m pytest -q tests/test_winjob.py                 # which Windows answers prove a process gone (injected kernel32, plus two real-kernel pids)
python -m pytest -q tests/test_dsh_surfaces.py           # what DSH reads at launch, fixed paths, names never values, a .env never opened, nothing executed
python -m pytest -q tests/test_review.py                 # the review output grammar
python -m pytest -q tests/test_review_wire.py            # reviewer verdict -> receipt; completion bound to its own prompt
python -m pytest -q tests/test_driver_turn_settlement.py # what settles a turn, what follows its response, agent-reported usage never billed
python -m pytest -q tests/test_real_client_review.py     # installed acpx + mock agent, no model (hardened launch, --model)
python -m pytest -q tests/test_packet_wire.py            # role packets -> pinned acpx + input-sensitive agent, no model
python -m pytest -q tests/test_saved_review_replay.py    # the recorded live review, replayed offline
python -m pytest -q tests/test_local_finalization.py     # one later decision, recorded through the Store
python -m pytest -q tests/test_concurrency.py            # deterministic thread/cancel orderings
python -m pytest -q tests/test_m2_slice.py               # Git worktree -> frozen candidate, complete and deny-aware freeze
python -m pytest -q tests/test_cli_m2_cleanup.py         # the same flow through the CLI + guarded clean
python -m pytest -q tests/test_dispatch_gates.py         # pre-dispatch gates, loop allowance, packet bound
python -m pytest -q tests/test_check_resources.py        # bounded output, artifacts, minimal environment, reference round-trip
python -m pytest -q tests/test_batch_e_dispatch.py       # the one dispatch transaction, open-state settlement, stop/ledger races
python -m pytest -q tests/test_batch_e_repair.py         # the bounded repair, late results after a stop, base SHA, freeze scope
python -m pytest -q tests/test_launch_refusals.py        # a workspace .env (never opened) and a DSH_HOME in the workspace refused on a real driver
python -m pytest -q tests/test_credential_source.py      # no visible credential source refused at prepare, the run gate and the spawn gate; names and stat only
python -m pytest -q tests/test_owner_lease.py            # owner identity and lock, adoption and takeover only from a provably gone owner, fenced writes
python -m pytest -q tests/test_ledger_settle.py          # operator settlement of unknown/launch_unknown entries, never shown as observed
python -m pytest -q tests/test_typed_findings.py         # the typed reviewer finding, strict, no text fallback
python -m pytest -q tests/test_context_files.py          # undeclared DSH context-file changes refused, declared ones shown as untrusted data
python -m pytest -q tests/test_launch_content_binding.py # launch entry files bound by SHA-256, re-checked at the spawn gate
python -m pytest -q tests/test_concurrent_submission.py  # two submissions of one TaskSpec never relabel each other's run
python -m pytest -q tests/test_bytecode_isolation.py     # worker-left bytecode removed before each check, sourceless .pyc refused
python -m pytest -q tests/test_scope_rules.py            # write_deny spellings normalized, unmatchable entries refused
python tools/m0_probe/check_process_boundary.py          # Job Object teardown, standalone (Windows)
python tools/m0_probe/real_client_checks.py all          # real acpx: version + mock-agent round trip
python tools/m2_live/prepare_m2_live.py                  # build the real M2 task package (dispatches nothing)
python tools/m2_live/replay_review.py R-gkb3ld97x8       # replay a recorded review; no model, no writes
python tools/m2_live/replay_review.py R-xxxxxxxxxx --finalize  # record one later decision (needs explicit approval)
```

Business-task scripts that only mean something next to a local handoff package (a package verifier, an
authorization writer, an environment probe for one specific task) are kept on this machine and are
deliberately not listed here: they are not part of the committed tree, and their inputs live under
`handoffs/`, which is local too.

The driver tests run the real launch/observe/stop code against a test-only stand-in for the
acpx CLI plus stub agents that are **separate processes** - including one that ignores
cancellation and holds a helper child, so a decorative process boundary would fail the test
rather than pass it. The stand-in client mirrors acpx's `--model` behaviour (`STUB_MODEL_CATALOG`,
`STUB_CLIENT_FAIL_BEFORE_PROMPT`), and the project's mock agent has a `dsh-catalog` scenario that
copies the shape of DSH's grouped model catalog with placeholder ids. The stub agent can also
answer the prompt with a JSON-RPC error or reuse its id (`STUB_TERMINAL_RESPONSES=2:!<code>` /
`2:?<method>`), answer without a stop reason (`STUB_TERMINAL_RESPONSES=2:v2:<stopReason>`), send
updates after its response (`STUB_TRAILING_UPDATES` / `STUB_REVIEWER_TRAILING_UPDATES`) and report
usage and cost (`STUB_REPORTED_USAGE`); the mock agent has matching `prompt-error` and
`trailing-update` scenarios. `real_client_checks.py` and
`test_real_client_review.py` are the ones that exercise the *installed* acpx: a metadata probe and
full one-shot `exec` runs against the project's mock agent, all with zero model calls. None of
them is evidence about real DSH. Every test runs with a stand-in `DEEPSEEK_API_KEY` that replaces
any real one (`tests/conftest.py`; `tools/m0_probe/real_client_checks.py` sets its own), because a
real launch with no credential source visible is refused (`no_credential_source`); the tests of
that refusal remove it.

Two of the `tools/` commands in this README need a warning label, because both can write
something: `python tools/m0_probe/write_results_doc.py` regenerates `docs/m0-results.md` - a
**generated M0 snapshot**, whose "live forced stop still `not_tested`" conclusion was written
before the later M2 trial A and has not been regenerated since (editing that file by hand will
be overwritten; the current status is the table at the top of this README) - and
`python tools/m2_live/replay_review.py <run> --finalize` writes a delivery decision through the
controller/Store path, which needs its own explicit approval and is not part of any offline
test run (the replay refuses a reviewer stream with message text after its prompt response).

## M0 transport probe

```sh
cd .probe/acpx && npm install --no-fund --no-audit acpx@0.17.1   # project-local, once
python tools/m0_probe/run_probe.py --phase a --phase b           # zero real model calls
python tools/m0_probe/run_probe.py --phase c --live \
    --live-max-submissions 2 --live-credential-ref DEEPSEEK_API_KEY   # explicit opt-in
python tools/m0_probe/write_results_doc.py                       # regenerate docs/m0-results.md
```

The probe never touches `~/.dsh`, `~/.acpx`, PATH, or any global install: each child gets a
probe-private `USERPROFILE`/`HOME`/`DSH_HOME` under the gitignored `.probe/` directory. The
live phase is bounded by a persisted submission counter, and `--live-credential-ref` is the
only path that reads a credential (one named reference, in memory, never written or logged).

`docs/m0-results.md` is the M0 record and is regenerated from recorded artifacts only - no live
task is re-run to produce it. Its per-row conclusion is scoped to what had been observed when it
was generated; `docs/m2-live-acceptance-result.md` supersedes its forced-stop row for one
machine, one acpx/DSH version and one binding, and nothing else in it is upgraded by that.
