# HFlow

A small deterministic controller for harness-agnostic agentic development tasks.
The controller owns admission, budget, evidence, and delivery accounting; a native
Harness (DSH first) owns reasoning and tools.

**Status: one thin production Driver plus an offline fake; offline vertical slice and the M2
offline delivery slice; the batch E1 root ledger and the batch E2 bounded repair, offline-tested;
the batch F hardening from the 2026-10-03 upstream survey (turn settlement, reviewer transcript,
stop confirmation, shared Git metadata, launch and context records), offline-tested;
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
Development dependency: `pytest>=9.0,<10`. Git 2.31 or later for worktree runs (`git rev-parse
--path-format=absolute`; `git config --show-scope` needs 2.26). The global attributes file is
located with `git var GIT_ATTR_GLOBAL` on Git 2.42+, and on older Git from `core.attributesFile`,
else `$XDG_CONFIG_HOME/git/attributes`, else `~/.config/git/attributes`.

```sh
python -m pytest -q            # see the recorded snapshot below
python -m pip install -e .     # optional: installs the `hflow` console script
```

Tests need no model, no network, and no DSH: they use the offline fake driver, a test-only
stand-in for the acpx client (`tests/fixtures/fake_acpx_client.py`) and stub ACP agents that
are real separate processes (`tests/fixtures/stub_acp_agent.py`). Every test runs with
`HFLOW_DATA_DIR` pointed at its own temp directory (`tests/conftest.py`), so the suite never
writes your real data directory, and with a stand-in `dsh` launcher first on `PATH` (an npm-style
`dsh.CMD` on Windows, an executable `dsh` elsewhere) that only exits 1 and is never run: a real
launch now needs `dsh` as an absolute file on `PATH`, so without it whether a test's launch
resolves would depend on - or find - a real DSH install. Test counts depend on the tree being
tested, so they are recorded rather than asserted:

| Snapshot | Command | Result |
|---|---|---|
| batch F on top of `f2796aa` (prompt errors and unknown stop reasons, stream order and after-response text, reviewer transcript by `messageId` and session, fail-safe exit checks, shared Git metadata, launch-surface and DSH-context records) | `python -m pytest -q` | `938 passed, 1 skipped in 610.03s` (exit 0; 939 collected) |

History, for orientation only: the suite grew from 225 tests (T02 admission) through 298 (batches
A/B/C), 353 (batch D configuration wiring), 377 (reviewer-cancel routing and the coordinated spawn
gate), 513 passed at `0ec289c` (E1 root ledger, E2 bounded repair; the figure its commit
message records) and 738 at `f2796aa` (the 2026-10-02 refinement), each with the one
directory-link skip on this machine.

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
hflow doctor   --json                     # read-only environment probe, no model calls
hflow doctor   --profile dsh-local        # resolve a profile per role; non-zero if unusable
hflow prepare  --task t.json --profile dsh-local   # zero-model preview: config, admission,
                                                   # scope, checks, budget, packet preview
hflow run      --task examples/task.json --project-root . --driver fake --json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json --json
hflow prepare  --task t.json --profile dsh-local --root-budget-file root-budget.json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json \
               --root-budget-file root-budget.json   # spend against a root ledger (batch E1)
hflow prepare  --task t.json --profile dsh-local --repair-policy-file repair.json
hflow run      --task t.json --profile dsh-local --authorization-file auth.json \
               --repair-policy-file repair.json      # opt in to ONE bounded repair (batch E2)
hflow status   R-xxxxxxxxxx               # pure SQLite read, zero model calls
hflow report   R-xxxxxxxxxx --json        # receipt + evidence + the config it ran under
hflow resume   R-xxxxxxxxxx               # reconcile an interrupted attempt; never re-dispatches
hflow cancel   R-xxxxxxxxxx               # for a run another process is executing: records
                                          # the stop, reports unknown, blocks outcome_unknown
hflow clean    R-xxxxxxxxxx               # preview releasing the run's worktree
hflow clean    R-xxxxxxxxxx --apply       # remove it; candidate ref and receipt (if any) kept
hflow schema                              # generated JSON Schema, MachineProfile included
```

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
finished (`DRAFT`/`READY`/`RUNNING`/`CHECKING`, e.g. `resume` on a run a hard kill left `RUNNING`).
`6`: `status`/`report` found the run but a stored record of it no longer validates; one line names
the record, nothing is rewritten, and `cancel`/`resume` still work from the run row.
A `CANCELLED` task state, which this build never writes, would exit `3`.

Without installing, run through the module path:

```sh
python -c "import sys; sys.path.insert(0,'src'); from hflow.cli import main; raise SystemExit(main())" run --task examples/task.json --project-root . --driver fake
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

- The controller generates `ResultReceipt`; a worker cannot report `ACCEPTED`.
- Budget is reserved in the same transaction that records the dispatch.
- A **root budget** (batch E1) is one ledger row for one `(project_id, repository, task_id)`,
  across revisions. `--root-budget-file` binds the run to it, the ledger path is part of the
  authorization binding - so pointing `--data-dir` somewhere else is refused rather than
  handing the same task a second unused allowance - and every top-level dispatch is reserved
  with its attempt row and counters in **one** transaction. That root runs **one task at a
  time**: a dispatch is refused while another run of the same root has not reached a terminal
  state, because a settled invocation is not a finished run. Every dispatch also has to leave
  enough allowance for the rest of that run's loop, so a revision that can afford its
  implementer but not its reviewer is refused instead of buying half a loop, and one attempt
  records **one review**: a second reviewer invocation id is refused whether or not the first
  review has settled. Four facts stay separate in the ledger - an allowance `reserved`, a launch
  `requested`, a launch that happened `started`, and a result `settled` - and the physical facts
  are separate again: `processes` counts invocations whose driver reported a pid, so an offline
  run that completes in-process is two settled dispatches and **zero** processes, while a launch
  that reports no child is recorded as such and never counted as one. Every unresolved state
  (`reserved`, `requested`, `started`, `unknown`, `launch_unknown`) blocks every later dispatch of
  that root, whatever the revision. `not_started` is recorded only on a driver's own word that
  nothing launched, or when no driver was ever asked (a driver that sends no spawn report and
  returns cancelled with no work counts as saying so only when the stop was recorded before it was
  handed the invocation): a launch that was requested and never reported back is
  `launch_unknown`, because an empty timestamp is not evidence that nothing ran - and settling
  that same unresolved launch twice (a reconcile followed by a confirmed stop) is idempotent, so a
  stop that really happened never fails to report itself. A spawn report that
  arrives after the entry was closed as `launch_unknown` never reopens it: a reported launch makes
  it `unknown` (its process facts recorded), a reported non-launch `not_started`.
  A run without `--root-budget-file` for a task that already has a root in the same ledger is
  refused (`budget_exhausted`) before its run row exists - and again in the dispatch transaction
  as a race backstop - so dropping the flag does not escape the root's unresolved invocations or
  its counters, and resubmitting the same TaskSpec with the root still dispatches.
  What the root may spend is recorded and enforced; `max_repairs` is a **ceiling, not a switch**.
  A repair happens only when the task carries an explicit `repair_policy` (batch E2; see "Not
  implemented"), it is bought only by a clean, declared business check failure or a substantive
  reviewer rejection with a usable finding, and one run can never buy a second one. With a real
  transport a repair policy **requires** `--root-budget-file`, and a root whose `max_repairs`
  cannot cover the repair is refused before the first dispatch and before the root is registered,
  so a corrected root file is then accepted; only a fully offline fake run may
  repair without a root. A root charge is always
  recorded with the artifact that bought it, so a root run needs one: a real driver needs your
  `--authorization-file`, while the offline fake driver - which reaches no model and has no
  approval to give - gets a CLI-computed record whose id starts `AUTH-offline`, whose `origin` is
  `cli_offline_synthetic` (a structural field, not just a sentence), and whose binding names
  `driver: fake`, so it can never authorize a real transport.
- **A repair is classified from a stored execution fact, never from prose.** `evidence.exit_reason`
  records *why* a check ended (`completed`, `nonzero_exit`, `timed_out`, `settlement_forced`,
  `output_capture_error`, ...), and an automatic repair needs that reason to say the check ran to
  completion, an exit code the task's own policy declares a business failure, and no ERROR mixed
  into the round. An empty reason (every row written before storage v5) never repairs, a timeout or
  a lifecycle defect is never rounded up to a business failure, and no classification reads the
  `verification_failed` string or a log keyword. Every decision, refusals included, is stored as a
  `RepairRecord`, the repair is its own `attempts` row marked `is_repair`, and the schema itself
  allows one first attempt and one repair attempt per run, revision and role - a third implementer
  cannot be inserted even by a bug.
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
- A reviewer's verdict is decoded from that reviewer invocation's own final message and
  validated against the canonical `ReviewOutput`; nothing else can supply it. The final
  message is the last message by ACP `messageId` (only a change of id starts a new one), and
  its answer is that message's `agent_message_chunk` text in stream order, with nothing
  inserted. A reasoning block (`agent_thought_chunk`) is never part of it, whatever its id: DSH
  sends a message's reasoning under the message's own id, so a reasoning block between two text
  blocks is skipped, not treated as the end of the message. A `messageId` that resumes after
  another message started blocks as `review_protocol_error`. Only message chunks for the session
  the turn's first `session/prompt` request named are read: a chunk for another session, or with
  no `sessionId` (counted as `agent_message_chunk_other_session=N`), a chunk seen before that
  request, or a request that names no session, blocks as `review_protocol_error`. A
  turn that produced no usable verdict blocks as `review_protocol_error` (a wire failure) instead
  of being reported as the reviewer requesting changes, a reviewer turn whose outcome is unknown
  blocks as `outcome_unknown` with its ledger entry settled as unknown (so `resume` can
  reconcile it and the root stays blocked), and a validated `changes_requested` stays a review
  rejection. A verdict is decoded only from a turn whose client output was read to its end and
  carried no `agent_message_chunk` for the prompt's session after the turn's own prompt response.
  Otherwise the final answer is not identified and the turn blocks as `review_protocol_error`
  (`review_ambiguous`), whatever either message says, and no repair is decided from it. The
  offline replay tool refuses such a stream too (a blocker, so `--finalize` writes nothing), and
  likewise a stream with excluded or pre-prompt message chunks, and a reviewer prompt answered
  with a JSON-RPC error or settled with a stop reason other than `end_turn`.
- What each role is told is rendered once, from stored facts, by the controller
  (`packet.py`) and transported verbatim; a driver may not rebuild or extend it. The prompt
  digest the transport reports is compared with the packet the controller rendered, so a
  result that arrived with different input is refused instead of attributed to this task.
- Review is a floor plus a request: a project that requires review cannot be waived by a task
  (refused at admission), and a task that asks for review gets one even when the project does
  not require it. Only both saying no may skip it.
- An unknown outcome blocks and never auto-retries.
- Verification is bound to a candidate fingerprint and a checks digest.
- Ignored bytecode never stands in for the committed source: every freeze refuses
  `scope_violation` on an ignored file the scoped fingerprint would hash (no commit holds it) and
  on a sourceless `.pyc` outside `__pycache__`, and in a worktree run every regular `.pyc`
  directly inside a real `__pycache__` of the worktree is deleted before each `command` check
  (whatever flags the check passes to Python, `-I` and `-E` included; never committed, never
  fingerprinted, so the candidate commit and fingerprint do not change). `.pyc` files the
  candidate commit tracks are kept; no link or junction is followed, and a `__pycache__` link or
  junction makes the check refuse to start. The evidence and a run note record the count. An
  in-place run deletes nothing in the user's checkout and does not get this protection.
- A `write_deny` entry is matched after normalization (a leading `./`, `.` segments and repeated
  `/` removed); an entry that is empty after that, absolute, drive-qualified or holds `..` is
  refused `scope_violation` at admission, the project's and the task's alike.
- The frozen candidate is the change that was checked: `write_allow` takes literal paths only (a
  glob, or an entry that is or passes through a symbolic link or junction, is refused at
  admission), a deleted listed file is committed as a deletion, a changed path under the task's or
  the project's `write_deny` or the built-in deny list (`.git`, `.hflow`, `.acpxrc.json`) blocks
  `scope_violation` before anything is staged - even inside an allowed directory - and a worktree
  that still shows a change after the commit blocks as an incomplete freeze. The worktree's HEAD
  must still be the commit the round started from (the original base, or the previous candidate
  in a repair): a worker that committed, amended, reset or checked out inside the worktree is
  refused `scope_violation`, because its commit never passed those checks. An index entry flagged
  assume-unchanged or skip-worktree (`git ls-files -v`), which would hide an edit from the commit
  while the checks read it, refuses the freeze the same way, and a repair before it is bought
  (`workspace_drift`); HFlow never clears the flag. The whole base-to-candidate diff is held to
  the scope and every deny list again after the freeze and at acceptance, and every path list is
  taken with `--no-renames`, so a moved file names both its old and its new path. HFlow's own Git
  commands run with hooks, `core.fsmonitor`, commit signing, `core.ignoreStat` and sparse
  checkout switched off - settings a caller's `git -c` cannot override - and without an inherited
  `GIT_DIR` or other repository-locating variable. The shared Git metadata those commands read
  from outside the worktree - every configuration key in your checkout and the run's worktree, the
  files it comes from, both `config.worktree` files, `.git/info/attributes` and the global
  attributes file - is snapshotted after `worktree add` and before the first dispatch, and
  compared before the freeze, before a repair round (`workspace_drift`) and at acceptance. Any
  change blocks `scope_violation` ("shared Git metadata changed ...") before HFlow's status, add
  or commit reads it again. A run that ends without a receipt for another reason is compared once
  more and the result is a note (`git_metadata: changed when the run ended`); it never relabels
  the block (see "Not verified" for what that does not cover). `hflow run` prints that note, and
  `hflow status` / `report` print every metadata warning as a `git metadata` line
  (`git_metadata_notes` in `report --json`). Git output that is not text - a config value is raw
  bytes - counts as unreadable: `scope_violation` after dispatch, `internal_error` before it.
- The implementer and the reviewer are resolved from the profile **independently** and
  dispatched through their own driver object; a role the profile does not bind is refused rather
  than inheriting the other's agent. A binding's declared **harness** must be one its driver
  actually launches, so a profile cannot record `codex` next to a driver whose every process is
  DSH.
- `prepare` and `run` derive everything - task overrides, project contract, role bindings,
  driver names, the resolved launch, write permission, admission - from one resolution
  (`prepare.resolve_run`), so a preview cannot describe a configuration the run does not execute.
- `prepare` exits non-zero both for a task admission refuses **and** for a dispatch precondition
  a run would refuse on (a write scope with no worktree or no write opt-in, a required review the
  budget cannot cover, a launch this machine cannot resolve, a starting workspace that already
  holds `.acpxrc.json`). Reporting "ready" for a task the
  run refuses would be answering a different question than the user asked.
- An authorization binds the **effective configuration**, not just the task: profile, per-role
  agents and drivers, model selections, limits, write permission, and the *resolved launch* -
  the client entry point, the interpreter that starts it, the launcher argv (the DSH batch shim
  wrapped in the absolute `%SystemRoot%\System32\cmd.exe`), the `--model` value when a profile
  names one, and the DSH home/profile. Changing any of them - including `HFLOW_ACPX_NODE` or
  `HFLOW_ACPX_CLI` - changes the digest and the old approval stops applying; artifacts issued
  before the 2026-10-02 launch hardening must be re-issued for that reason (the absolute `cmd.exe`,
  the bound `--model`, and `dsh`/`node`/`python` now always resolved to absolute paths where an
  earlier build could bind a bare or relative name). The task's base commit
  is bound as the **SHA** it resolved to, not as `HEAD` or a branch name, so an approval stops
  applying when the branch moves, and the run, every round and the receipt use that one commit. The launch is resolved once and then consumed
  by the driver, never re-selected after the check: a bound `DSH_HOME` is set on every child
  process, and a launch that bound none has it *removed* from the child environment rather than
  inherited. With none bound, DSH uses a per-invocation, empty home
  `<data-dir>/invocations/<id>/home/.dsh` - no stored credentials, patch files, AGENTS.md or
  skills (inferred from upstream source, not observed); `prepare`, `doctor` and each invocation's
  record name it. An artifact written before config binding still loads and still keys its own
  ledger row, but it cannot authorize a run that resolved a configuration. The binding also
  carries the project contract's digest and the roles the run will dispatch, so **every artifact
  issued before the project-contract binding must be re-issued**, whenever it was written (one
  without the contract digest is refused, naming why); copy a fresh binding from
  `hflow prepare --json`.
- The agent launch argv carries only the launcher path and fixed flags. A model id is the one
  value that goes on a command line from configuration: the acpx **client** gets `--model <value>`
  when the role's profile names one, which counts as a fixed flag under AGENTS.md rule 9 by the
  user's ruling of 2026-10-02 - it is fixed by the machine profile, validated when the profile
  loads (a token, or a `[provider, model]` pair; never starting with `-`) and covered by the
  authorization digest. Task text, nonce, user content and credentials still never reach a
  command line.
- No agent is started on a workspace that contains `.acpxrc.json` at its root, in any letter case
  (acpx would load it from `--cwd` and let it replace the bound launch, including the agent argv;
  on a case-insensitive filesystem its open finds `.ACPXRC.JSON` too). A run whose starting
  workspace already holds one - the project root for an in-place run, the base commit's tree for
  a worktree run - is refused before a run row, an authorization record, a reservation or a
  process exists (`prepare` reports it too, naming the spelling found), and the driver's spawn
  gate still refuses one that appears later. Every launch program is an absolute file: `dsh`,
  `node` and `python` are looked up only on absolute `PATH` entries and never fall back to a bare
  name, an explicitly named program must be absolute, and a launcher, client interpreter or acpx
  client entry inside the project root or the directory the run's worktrees are created in
  (`<repo>.hflow-worktrees`) is refused, as is a workspace inside the `node_modules` tree that
  entry loads from. With `HFLOW_ACPX_CLI` unset the entry may fall back to the HFlow checkout's own
  `.probe/acpx`, so a run whose project root is the HFlow repository itself reports the launch not
  resolvable; point `HFLOW_ACPX_CLI` at an acpx installed outside the project. The child gets
  `NoDefaultCurrentDirectoryInExePath=1` and no relative `PATH` entry (absolute entries are
  inherited as they are), and ambient `DSH_PERMISSION_MODE` / `DSH_TOOLS_MODE` are removed, so
  neither a repository file nor the operator's shell can change what was approved. See "Client
  launch hardening" in `docs/operations.md`.
- A turn's outcome is taken only from the response to its own observed prompt: a completion that
  answers another request is `outcome_unknown` (`unbound_completion`) for either role; a JSON-RPC
  error answering that prompt (ACP v1's failed-prompt shape; DSH sends `Internal error: turn
  failed: ...`) is recorded with its code and message as `outcome_unknown`
  (`prompt_error_response`), not `failed`; an error answering another request, or one carrying the
  prompt's id after a request from the agent reused it, is not attributed; a prompt answered with
  both an error and a stop reason is unknown; a stop reason outside ACP v1's set is
  `outcome_unknown` (`unknown_stop_reason`). A prompt response with no `stopReason` (for example
  the `{messageId}` insertion acknowledgement sketched in an unreleased ACP v2 RFD) settles
  nothing (`outcome_unknown`, `no_stop_reason`), and a later idle `state_update` is an update, not
  a settlement. Every bound result whose stream was read to its end records where its prompt
  response fell and how many updates for the prompt's session followed it (`stream_order`;
  `status` prints them). An implementer's trailing updates are recorded and never judged. A result
  of **either role** that arrives after a recorded stop - confirmed or not - is only a
  `late_result` note: an implementer's freezes nothing and creates no candidate ref, a reviewer's
  records no verdict and no review evidence, neither settles its ledger entry (after an
  unconfirmed stop it stays open and keeps the root blocked until `resume` marks it `unknown`),
  and the run keeps the stop's block. A stop that lands while the acceptance is being written
  leaves the run in the stop's state with no receipt.
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

**Automatic repair (batch E2) exists in exactly one bounded form.** What it does: when a task
carries a `repair_policy` (`--repair-policy-file` on `prepare`/`run`, or the field in the task
file), one failed round may be followed by **one** second implementer attempt, on the same
revision, starting from the frozen candidate, followed by fresh checks (never the first round's
rows) and a fresh independent review. It is bought by exactly two triggers - a check the policy
declares a *business* failure (`check_exit_codes`) whose recorded `exit_reason` says it ran to
completion, or a substantive `changes_requested` on the current candidate with at least one
usable finding when the policy allows it. Everything else stops the run: a mixed ERROR, an **undeclared exit
code** (a non-zero code the policy does not name never repairs), a timeout, a check that left
descendants, an incomplete capture, a launch that never happened, a malformed review, an unknown
outcome and a cancellation. A policy only ever classifies a failure it can see: a check id the
project contract never runs produces no failed row, so that entry can buy nothing, and a failure
whose id or exit code the policy does not declare is recorded as a `not_a_business_failure`
decision naming the check, its exit code and what the policy declared for it. An evidence row
written before storage v5 carries no observed reason and is therefore ineligible - the reason is
never back-filled from a stored exit code. The root's `max_repairs` stays a ceiling that the
dispatch transaction enforces; a task file that says `max_repair_cycles: 1` still opts into
nothing. A reviewer rejection buys the repair only with at least one **usable** finding - a
non-blank text value under a key other than `id`/`severity`/`status`/`location`/`target` - so
`[{}]` or metadata-only findings stop the run as `no_findings`; the repair packet carries every
key of every usable finding, cutting a value over 2048 bytes with an explicit marker. The repair
round's reviewer sees the whole change from the original base, not only the second round's patch.

What it still is **not**, and cannot be read as more than:

- **No `repair` command.** A repair is a decision taken inside one live run, never an action you
  invoke on a finished one.
- **No revival of a historical `BLOCKED` run.** A run ended by a verdict, a stop or a block is
  never reopened for a second attempt.
- **No retry of an environment or transport failure.** Only a declared business check failure or a
  substantive reviewer rejection - once - may buy the second attempt.
- **No rootless repair on a real driver.** A repair policy on a run whose roles use a real
  transport is refused unless `--root-budget-file` binds a root (`prepare` lists it under
  `dispatch_preconditions`); only a fully offline fake run may repair without one, charged to no
  root counter.
- **No typed finding schema.** Findings are still "a list of objects"; the reviewer is not told
  which keys count as content.
- **Message boundaries without `messageId` are inferred.** For a runtime that omits ACP's
  optional `messageId`, any update that is not a message chunk (a thought included) or a
  sequence gap ends a message, and only the last such segment is read as the answer. Every
  message chunk in the recorded live DSH streams carries a `messageId`, so no recorded
  production run took this path. The order "text, reasoning, text" inside one DSH message comes
  from DSH source (`dsh-v0.2.0-rc.2`, `packages/acp/acp/src/updates.ts`); HFlow handles it in
  offline tests only, and it has not been observed live.

Offline checks never look like a real execution: `kind=fake` launches no process, so its evidence
says `not_launched` and it can never trigger a repair. An offline test that wants the repair rule
exercised has to *declare* the clean process exit it is modelling
(`FakeCheckRunner(verdicts=..., exit_reasons={"unit": "nonzero_exit"})`). That is a deliberate
modelling choice inside the offline facility, not evidence about a live harness.

Not built: cooperative (protocol) cancellation on the selected launch path; a `hflow repair` or
`hflow integrate` command; integration/publish delivery; reuse-research automation; teams and
native subagents; real billing observation; metrics against a direct-DSH baseline; an operator
command that closes an `unknown` or `launch_unknown` ledger entry (`resume` records an
observation but settles nothing, so such a root stays blocked), or an entry left open on a run
that is not `outcome_unknown` - a confirmed stop whose ledger write failed
(`cancelled_by_operator`), or a driver that raised without reporting a spawn fact
(`internal_error`, or `review_protocol_error` for the reviewer) - which `resume` does not touch
because it reconciles only an `outcome_unknown` run, so that root stays blocked too (the run's
notes say so instead of promising a reconcile); the upgrade of the pinned acpx
from 0.17.1 to 0.19.x (surveyed, deliberately deferred); reading a prompt answered with a
JSON-RPC error as `failed` - it stays `outcome_unknown` with its code recorded, even for DSH's
pre-model forms, because telling them apart means reading DSH's message text, and that
reclassification needs a decision. A reviewer's answer is read as text and
decoded against the contract - the harness is not asked for structured output, and no model is
ever asked to repair a malformed verdict.

Why cooperative cancellation is still unsupported, precisely: the pinned acpx `exec` path does
send `session/cancel` (and waits 2.5 s) when its own process receives SIGINT, SIGTERM or SIGHUP.
HFlow starts the client with `CREATE_NEW_PROCESS_GROUP`, which disables Ctrl+C for that group on
Windows; Ctrl+Break arrives in Node as SIGBREAK, which acpx does not handle; and Windows has no
external SIGTERM/SIGHUP. M0 recorded the one observation behind this: a CTRL_BREAK killed the
client (exit `0xC000013A`) before any cancel reached the agent. The rest is reasoning, not a
measured delivery, so the capability stays `unsupported` until a protocol `cancelled` is
observed.

Not built around configuration either:

- **A profile's model is passed, but only offline-tested.** A `model_selection` other than
  `native_profile` goes to the acpx client as `--model <value>` (bound in the launch and the
  approval digest; `native_profile` passes no flag), acpx applies it with
  `session/set_config_option` before the prompt and fails closed on a value the agent does not
  advertise (`model_rejected_before_prompt`), and `status`/`report` show what each invocation's
  stream said about the model - `model_applied=passed` only while no change request was needed
  and the stream's last reported value is still the requested one, `unknown` whenever the stream
  does not show that. All of that is exercised with the pinned acpx against a mock agent
  only. No live DSH `set_config_option` round trip has been observed, so the capability stays
  `documented`, and concrete model values for real DSH wait for an approved zero-prompt catalog
  check - keep profiles on `native_profile` until then. `hflow doctor --profile <id>` prints the
  resolved launch per role, including the exact `--model` flag or "no --model flag".
- **No `hflow init`.** A project contract is still hand-written, and no `--format markdown`
  handoff renderer or `hflow repair` / `hflow integrate` command exists. `resume` still only
  reconciles.
- **A stop targets the role that is running, and the two sides of a handoff are coordinated, not
  merely ordered.** The controller resolves the live invocation from the run's recorded `phase`
  and routes the stop through that role's own driver, so a reviewer bound to its own driver is
  stopped through it and the `cancel_target` note records which role, driver and invocation
  were asked. Two writes carry the stop decision rather than following a read: registering a
  role's invocation is one statement with `WHERE cancel_intent_at IS NULL`, and every block is
  one statement with the same condition. The spawn is coordinated the same way and closer to the
  metal: a driver publishes the invocation's handle and creates the child **inside one gate**,
  and *both* stop entry points - `cancel_handle` and `cancel(invocation_id)` - take that same
  gate to record their request. "No handle yet" is therefore never an answer while a child is
  being created: a stop that takes the gate waits for that critical section to end - normally by
  waiting to acquire the gate, which the spawn releases only after publishing - and then
  terminates the process it finds, reported as `mechanism=forced`. A stop that takes the gate
  first means **no process is created at all** (`start_cancelled`, reported as a cancelled
  invocation with no child). `InvocationRequest.stop_requested` is the question the driver asks
  inside that gate, so the answer is taken at the instant of the spawn rather than when the
  request was built. That wait is bounded by the spawn and not by the invocation: the gate is
  released before the model answer is awaited, so a stop may wait for a process to be created but
  never for the call it is stopping to finish. A stop that is not confirmed never re-dispatches,
  and no later failure (`review_protocol_error`, a transport error, a rejected review) relabels
  either stop state. On a live run `cancel` writes its receipt and the terminal block in **one**
  statement, so a cancel that fails half-way leaves only the intent and a retried one still ends
  the run; a stop of a run that had already ended is recorded with `run_already_ended` and
  changes nothing about that run. Three limits stay stated. The command-line `cancel` cannot
  reach a child started by another controller process: it records the stop and an `unknown`
  receipt, names the
  driver recorded for the run (never the offline fake for a production run), and blocks the run
  `outcome_unknown` - for offline runs too, since the fake confirms only the stops of
  invocations its own instance started; `resume` reconciles once the owning controller has
  exited. The root stays blocked only while the stopped invocation's ledger entry is still open:
  such a cancel settles no entry, but one that lands during the checks or the review handoff
  (the implementer's entry already settled, no reviewer registered yet) leaves no open entry, and
  the run is no longer live once it is blocked, so the root accepts a new revision immediately -
  even while the original controller is still finishing its checks, in the same workspace for an
  in-place run. Keeping a root busy for as long as its run's controller still owns it (owner
  identity across processes) is not built. For the same reason two `hflow run` processes on the
  identical TaskSpec (both on the default `--controller-id`) can both pass the claim and both run
  setup (worktree, Git snapshot, packet). What is guarded: only one of them reserves the attempt -
  the other returns the existing run with a note instead of blocking it - no writer that sets
  `BLOCKED` relabels a run that already ended or carries a receipt, and a controller never records
  its own refusal as the block of a run whose attempt another controller reserved. A refusal that
  lands before either reserves (a setup failure in the second process) still blocks the run, and
  the first then returns that blocked run. A controller interrupted (Ctrl+C, `SystemExit`) while an invocation is
  starting or running leaves an `outcome_unknown` run that `resume` reconciles, but a hard kill,
  a power loss or an interrupt outside a driver start records nothing and leaves the run
  `RUNNING`, which `resume` does not touch. And this covers the one driver that creates processes
  here (`AcpxDshDriver`); another driver must coordinate its own spawn the same way.
- **An offline profile is all-or-nothing.** A profile may bind every role to the offline fake
  (for development) or every role to a real transport; mixing the two is refused rather than
  half-scripted.
- **The launch is bound by paths and argv, not by program content.** A run records *which*
  client entry point, interpreter and launcher it will start; it does not hash those programs, so
  replacing a file at the same path - upgrading `.probe/acpx` in place, or a `dsh` that later
  resolves to a different carrier - does not change the approval. doctor's probe notes and each
  invocation's launch-surface record carry the acpx and @agentclientprotocol/sdk versions from
  their package.json, and the dsh carrier (Desktop or npm) with the version its shim's manifest
  states; files are read, nothing is executed, and nothing is bound. Binding by content stays
  deferred and needs a ruling.
- **A repository that keeps an `.acpxrc.json` cannot be run on a real driver.** acpx offers no
  way to skip or pin that file (upstream issue #835, open), so a run whose starting workspace
  holds one is refused (`workspace_client_config`) before anything is recorded or charged - the
  project root for an in-place run, the base commit's tree for a worktree run (an uncommitted copy
  in your checkout is not in the worktree and does not refuse). Any spelling of the name at the
  workspace root counts, `.ACPXRC.JSON` included. Remove it and submit again; for a
  worktree run commit the removal, and a real run then needs an authorization issued for the new
  base. A file that appears only after admission (in the reviewer's candidate worktree, say) is
  refused at the driver's spawn gate with the dispatch already reserved: the run stays blocked,
  an identical TaskSpec returns that blocked run, and the next step is a new revision - whose
  first implementer, under a root budget, is charged as a repair, so the root needs one left
  (without one the revision is refused `budget_exhausted` before its run row exists).
- **A candidate that changes the files DSH loads as context is recorded, not refused.** The list
  is `AGENTS.md`, `CLAUDE.md`, `AGENTS.local.md` and `CLAUDE.local.md` at any depth, the root
  `.dsh/skills/**` and `.agents/skills/**`, and a root `.env`, in any letter case; it is read from
  upstream DSH source at dsh-v0.2.0-rc.2 (639ed015) and not observed in a DSH run. After each
  freeze the whole change from the task's original base (`--no-renames`, so a deletion counts) is
  classified against it and kept as that attempt's `dsh_context` record. `status` and `report`
  show it, the review evidence of that attempt is marked `dsh_context=changed` (joined by attempt
  id), and an accepted receipt carries a limitation naming the files. Nothing is refused and the
  reviewer packet is unchanged; it already lists every changed path. An ignored instruction file
  or `.env` the implementer writes already refuses the freeze. Not covered: such files already in
  the base commit; in-place runs; a link to a directory that holds one; an ignored `AGENTS.md`
  under an allowlisted cache directory (`__pycache__`, `.pytest_cache`); anything under the DSH
  home. Refusing these files, labelling them in the reviewer packet, or telling the repair
  implementer each needs a ruling.

Not verified, even where something works on one binding:

- **Stopping.** The forced stop is a *Windows* Job Object property, so it covers what the
  boundary owns. Descendants that stay inside the Job no longer outlive an invocation: when the
  client exits, collecting its result terminates what is left (and says so in the result's
  limitations), a Job that cannot be emptied makes the result `boundary_not_empty` /
  `outcome_unknown`, and an exited client is never reported stopped without asking the Job. It
  does not follow a descendant that leaves the job, does not exist on other platforms, and a stop
  that cannot be confirmed stays `still_running`/`unknown` - including one whose Job emptied but
  whose client exit neither the driver's own `Popen` handle nor a re-opened pid could show: only
  "no such process" (`ERROR_INVALID_PARAMETER`) or a signalled process counts as gone, and the
  access-denied answer has only been injected in offline tests, never observed on a real client.
  Off Windows the boundary degrades to `direct_child_only`, whose terminate does nothing, so there
  a forced stop or a deadline teardown (`completion_timeout`) does not kill even the direct child.
- **Shared Git metadata is compared, not confined.** Every Git command HFlow runs through
  its repository handle forces `core.hooksPath` to an empty HFlow-owned directory,
  `core.fsmonitor=false`, `commit.gpgsign=false`, `core.ignoreStat=false` and
  `core.sparseCheckout=false` - through `GIT_CONFIG_COUNT`, and appended to an inherited
  `GIT_CONFIG_PARAMETERS` as well, so a caller's `git -c` cannot override them - sets
  `GIT_CONFIG_NOSYSTEM` and `GIT_NO_REPLACE_OBJECTS`, and drops an inherited `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
  `GIT_COMMON_DIR` and `GIT_NAMESPACE`; the freeze commit also passes `--no-verify`. So no
  repository, user, worker or caller hook, monitor command or signer runs while a worktree is
  created, a candidate is frozen or a ref is written, HFlow's own `worktree add` never checks an
  entry out flagged assume-unchanged or skip-worktree (a flag set any other way refuses the
  freeze), and the user's global config is still read for everything else. But a worktree shares
  `.git/config` with the repository and a worker can write it: it can configure a clean, smudge or
  process filter there and select it through `.git/info/attributes` or `core.attributesFile`,
  with no `.gitattributes` in its scope. A status read can run such a filter, not only `git add` -
  including the acceptance-time status read of your checkout - and filters cannot be switched off
  generically without breaking legitimate ones such as LFS. So the metadata is snapshotted after
  `worktree add` and compared before the freeze, before a repair round and at acceptance (see
  "Non-negotiables"); a change blocks `scope_violation` before HFlow's status, add or commit reads
  it again. Offline tests only (fake driver plus test-issued git, verified on Windows); not
  observed against a real DSH worker. What stays unverified:
  - The comparison is point-in-time: a process still running as you (an escaped descendant,
    another process, you) can change the metadata between a comparison and HFlow's next git
    command.
  - Nothing is restored and there is no cross-run baseline. A later or concurrent run that starts
    after the change takes it as its baseline: its own status read of your checkout and its
    `worktree add` run before its snapshot. After a block, `hflow clean` (status, then `worktree
    remove`) and your own git read the changed metadata until you restore it. `hflow clean` does
    not check the `git metadata` warnings: even its preview runs `git status` in that worktree,
    which runs a clean filter the changed metadata names, so restore it before cleaning.
  - A config value that is not valid text in the locale's encoding (a legacy GBK value under a
    UTF-8 locale) already in your config blocks every worktree run `internal_error` before
    dispatch; HFlow does not decode it any other way.
  - False positives fail closed: your own `git config`, `push -u`, an IDE writing `branch.*`, a
    global-config edit or a check that runs `git config` during a run all block it. Each costs a
    new revision; under a root budget, that revision's first implementer is charged as a repair
    (a root with none left refuses it before its run row exists).
  - Not compared: ignore rules, system attributes (`GIT_ATTR_SYSTEM`, still read because
    `GIT_CONFIG_NOSYSTEM` does not drop it; they can only select a filter defined in the compared
    config), hooks (already neutralised), and object or ref metadata.
  - A FIFO, device or directory in a metadata path is recorded by its type, never read.
  - An in-scope `.gitattributes` is recorded as a receipt limitation, not refused: a filter your
    unchanged config defines, or a line-ending rule, can make the committed bytes differ from the
    checked ones.
  - In-place runs make no git call, so there is nothing to compare.
- **What the harness itself does** (documented upstream, not observed here): under the
  implementer's `approve-all`, DSH's permission escalations - up to unconfined commands - are
  auto-approved; DSH uploads session logs to DeepSeek by default; DSH reports blocked and aborted
  turns as `end_turn`; a failed DSH turn arrives as a JSON-RPC error rather than a stop reason,
  which HFlow records as `prompt_error_response` and leaves `outcome_unknown`; acpx's `--timeout`
  bounds each phase, not the call; and DSH loads `AGENTS.md`/`CLAUDE.md` (and their `.local`
  forms), project skills and the workspace `.env` into each role's context next to HFlow's packet
  (documented at dsh-v0.2.0-rc.2; see the `dsh_context` bullet under "Not implemented"). See "What
  the harness does that HFlow does not control" in `docs/operations.md`.
- **What DSH reads on its own at launch is recorded, not controlled.** Documented at
  dsh-v0.2.0-rc.2, not observed: `<workspace>/.env` and `$DSH_HOME/.env` (a name the child did not
  inherit enters DSH's environment; a DSH_*/XDG_*/NODE_OPTIONS/PATH/proxy or other bootstrap name
  in a workspace `.env` makes DSH exit before serving ACP; `DEEPSEEK_API_KEY` there is a
  credential fallback); the `$DSH_HOME` and `profiles/<profile>` `cordis.patch.yml` layers, which
  can replace the sandbox and approval rows; `$DSH_HOME/AGENTS.md` and the
  AGENTS.md/CLAUDE.md(.local) chain from the nearest `.git` marker; skills; and every DSH_*
  variable except the two HFlow removes. With DSH_HOME unbound, the home is the per-invocation
  empty one. Per invocation and role HFlow records the presence, size and SHA-256 of those fixed
  paths (a `.env` by presence and size only, never opened; the home's stored credentials are never
  read), the DSH_* variable names, whether `DEEPSEEK_API_KEY` is among the child's variable names,
  the client and carrier versions, and a note when the DSH home lies inside the workspace. It
  refuses none of it, binds none of it, and looks just before the spawn. Whether to parse or
  refuse a workspace `.env` is an open decision.
- **Nothing is sandboxed.** `command` checks and a real worker run with the current user's
  rights: no filesystem confinement, no credential confinement, and no protection against
  another process of the same user changing the workspace, the authorization artifact or the
  SQLite ledger.
- **Your checkout is compared by stat, not by content.** An **accepted** worktree run compares
  HEAD, the stash ref, the index entries and, for every `git status --ignored` entry, its path,
  status code, size and mtime, before dispatch and after acceptance; a difference records a
  WARNING, and the "left untouched" note is written only without one. A run that blocks or stops
  before acceptance records no after-comparison: no WARNING and no "left untouched" note, whatever
  happened to your checkout meanwhile. A rewrite that keeps both size and mtime, or a change
  inside an untracked or ignored directory git reports collapsed, is not detected.
- **A stop is local.** It stops a process on this machine; it does not prove that a remote
  model request stopped, that remote billing stopped, or that any usage figure is known.
- **Remote termination and billed usage remain unknown**, recorded as `null` and never as `0`.
  An agent-reported `usage` on the prompt response (an UNSTABLE ACP field, whose meaning is
  disputed in ACP #1860) and the optional `cost` of a `usage_update` are never read into
  `provider_billed_tokens`, `provider_cost` or `subscription_quota_remaining` (offline-tested).
  DSH 0.2.0-rc.2's source sends neither, and the five recorded live streams on this machine
  (agentInfo `deepseek-harness-acp` 0.0.1, not rc.2) carry neither.
- **Unattended execution is disabled**, and no new live task is authorized by anything in this
  repository - each live task needs its own explicit approval (`docs/operations.md`).

## Driver contract tests

```sh
python -m pytest -q tests/test_cancel_routing.py         # 25 tests: a stop reaches the live role, wins the handoff, and is never undone
python -m pytest -q tests/test_authorization.py          # 16 tests: the authorized real-run gate
python -m pytest -q tests/test_driver_acpx_dsh.py        # 91 tests, no model, no credential: launch hardening, model flag and observation, Job teardown
python -m pytest -q tests/test_winjob.py                 # 9 tests: which Windows answers prove a process gone (injected kernel32, plus two real-kernel pids)
python -m pytest -q tests/test_dsh_surfaces.py           # 10 tests: what DSH reads at launch, fixed paths, names never values, a .env never opened, nothing executed
python -m pytest -q tests/test_review.py                 # 52 tests: the review output grammar
python -m pytest -q tests/test_review_wire.py            # 56 tests: reviewer verdict -> receipt; completion bound to its own prompt
python -m pytest -q tests/test_driver_turn_settlement.py # 27 tests: what settles a turn, what follows its response, agent-reported usage never billed
python -m pytest -q tests/test_real_client_review.py     # 11 tests: installed acpx + mock agent, no model (hardened launch, --model)
python -m pytest -q tests/test_packet_wire.py            # 20 tests: role packets -> pinned acpx + input-sensitive agent, no model
python -m pytest -q tests/test_saved_review_replay.py    # 9 tests: the recorded live review, replayed offline
python -m pytest -q tests/test_local_finalization.py     # 25 tests: one later decision, recorded through the Store
python -m pytest -q tests/test_concurrency.py            # 8 deterministic thread/cancel-orderings tests
python -m pytest -q tests/test_m2_slice.py               # 70 tests: Git worktree -> frozen candidate, complete and deny-aware freeze
python -m pytest -q tests/test_cli_m2_cleanup.py         # 21 tests: the same flow through the CLI + guarded clean
python -m pytest -q tests/test_dispatch_gates.py         # 19 tests: pre-dispatch gates, loop allowance, packet bound
python -m pytest -q tests/test_check_resources.py        # 22 tests: bounded output, artifacts, minimal environment, reference round-trip
python -m pytest -q tests/test_batch_e_dispatch.py       # 52 tests: the one dispatch transaction, open-state settlement, stop/ledger races
python -m pytest -q tests/test_batch_e_repair.py         # 94 tests: the bounded repair, late results after a stop, base SHA, freeze scope
python tools/verify_b_recheck.py <temp-dir>              # the three re-checked B failure paths: failed capture, retention budget, last-line overflow
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

The per-file numbers were checked on 2026-10-03 with `python -m pytest --collect-only -q <file>`
(collection only, nothing executed); they change as tests are added.

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
them is evidence about real DSH.

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
