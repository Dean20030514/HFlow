# HFlow

A small deterministic controller for harness-agnostic agentic development tasks.
The controller owns admission, budget, evidence, and delivery accounting; a native
Harness (DSH first) owns reasoning and tools.

**Status: one thin production Driver plus an offline fake; offline vertical slice and the M2
offline delivery slice; three recorded live top-level M2 tasks (one stop trial, two attempts at
one small change) under explicit one-time authorizations.**

```text
transport_selection      = acpx-dsh-acp        (decided in M0, with bounded live evidence)
driver_implementation    = implemented         (src/hflow/drivers/acpx_dsh.py, selected in
                                                src/hflow/drivers/selected.py)
basic_live_roundtrip     = passed              (recorded M0 evidence: one prompt turn, real DSH)
live_cooperative_cancel  = unsupported         (the one-shot exec path has no session queue owner;
                                                never exercised, so also unproven)
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
schedule, a real delivery whose approved checks are `kind=fake`, a write task with no isolated
worktree or no write opt-in, and a task that needs a review but reserves only one turn -
instead of warning and delivering less.

A machine profile (`<data-dir>/profiles/<id>.json`) binds `implementer` and `reviewer`
independently: agent, transport, model selection and capability record per role. An unknown
profile, an unreadable one, a role the profile does not bind, a driver name this build does not
implement, a harness no implemented driver launches, or a profile that mixes the offline fake
with a real transport all **refuse** - there is no default binding and no fallback. `hflow
prepare` resolves exactly what `run` will use and prints it before anything is dispatched,
including the **resolved launch** (client entry point, interpreter, launcher argv, DSH
home/profile).

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
Development dependency: `pytest>=9.0,<10`.

```sh
python -m pytest -q            # see the recorded snapshot below
python -m pip install -e .     # optional: installs the `hflow` console script
```

Tests need no model, no network, and no DSH: they use the offline fake driver, a test-only
stand-in for the acpx client (`tests/fixtures/fake_acpx_client.py`) and stub ACP agents that
are real separate processes (`tests/fixtures/stub_acp_agent.py`). Test counts depend on the tree
being tested, so they are recorded rather than asserted:

| Snapshot | Command | Result |
|---|---|---|
| `9483a84` with the uncommitted T02 admission patch and its new tests | `python -m pytest -q` | `225 passed, 1 skipped in 131.85s` (exit 0) |
| the batch A/B/C working tree (uncommitted) | `python -m pytest -q` | `298 passed, 1 skipped in 190.18s` (exit 0) |
| current working tree with the batch D configuration wiring and its corrections (uncommitted) | `python -m pytest -q` | `353 passed, 1 skipped in 192.25s` (exit 0) |
| the same tree plus the reviewer-cancel-routing fix, its race regressions and the coordinated spawn gate (uncommitted) | `python -m pytest -q` | `377 passed, 1 skipped in 190.05s` (exit 0) |

The last row is this working tree, not a commit; the committed HEAD (`5382470`) was not
re-measured on its own. Before this batch's tests were added the same tree measured
`299 passed, 1 skipped`, so the batch adds 54: machine-profile loading and its refusals, the
zero-model `prepare` preview, per-role dispatch through two configured drivers, the
effective-configuration and resolved-launch halves of the authorization binding, the dispatch
preconditions `prepare` shares with the run's own gate, and the two checks that the child
process really receives the launch that was bound. The reviewer-cancel-routing change adds the
24 tests in `tests/test_cancel_routing.py` (378 collected in the tree, 1 skipped): a stop
reaching the reviewer's own invocation through its own driver, an unconfirmed stop staying
`unknown`, a late `accepted` verdict or a late failure not overriding either stop state, no
reviewer started or bought after a stop (including the windows inside the packet render, the
turn reservation and the check step), the same windows decided by thread interleaving rather
than by ordering, reconciliation following the same routing, and - against the **production
`AcpxDshDriver` over the offline client stand-in and a stub agent** - that a stop which wins the
spawn gate creates **no child process at all**, that a stop inside the spawn critical section
**waits for publication and then terminates the child it finds**, and that a stop which loses
the race terminates the published one. Four of those regressions came from external
counterexamples; they were kept in the repository rather than in someone's scratch directory.

Four tests skip themselves when their precondition is absent rather than pretending to pass:
the directory-link test in `test_contracts.py` (the skip seen above), the installed-acpx test in
`test_real_client_review.py` (needs `node` and the pinned client), and the two recorded-ledger
replays (`test_saved_review_replay.py`, `test_local_finalization.py`, which need this machine's
runtime database). A run that reports more than one skip is telling you which preconditions were
missing, not that the suite failed.

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
hflow cancel   R-xxxxxxxxxx
hflow clean    R-xxxxxxxxxx               # preview releasing the run's worktree
hflow clean    R-xxxxxxxxxx --apply       # remove it; the candidate and receipt are kept
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
no authorization is created.

Runtime data (SQLite, evidence, profiles) goes to `%LOCALAPPDATA%\HFlow` on Windows or
`$XDG_DATA_HOME/hflow` elsewhere; override with `--data-dir` or `HFLOW_DATA_DIR`.
It never lands inside a project checkout.

Exit codes: `0` accepted, `2` refused at admission, `3` blocked after dispatch, `4` usage.

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

`contracts.py` is the single definition of every structure; JSON Schema is generated
from it (`hflow schema`). There is no second hand-written schema to drift.

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
  that root, whatever the revision. `not_started` is recorded only when no driver was ever asked:
  a launch that was requested and never reported back is `launch_unknown`, because an empty
  timestamp is not evidence that nothing ran - and settling that same unresolved launch twice (a
  reconcile followed by a confirmed stop) is idempotent, so a stop that really happened never
  fails to report itself.
  What the root may spend is recorded and enforced; `max_repairs` is a **ceiling, not a switch**.
  A repair happens only when the task carries an explicit `repair_policy` (batch E2; see "Not
  implemented"), it is bought only by a clean, declared business check failure or a substantive
  reviewer rejection, and one run can never buy a second one. A root charge is always
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
  validated against the canonical `ReviewOutput`; nothing else can supply it. A turn that
  produced no usable verdict blocks as `review_protocol_error` (a wire failure) instead of
  being reported as the reviewer requesting changes, and a validated `changes_requested`
  stays a review rejection.
- What each role is told is rendered once, from stored facts, by the controller
  (`packet.py`) and transported verbatim; a driver may not rebuild or extend it. The prompt
  digest the transport reports is compared with the packet the controller rendered, so a
  result that arrived with different input is refused instead of attributed to this task.
- Review is a floor plus a request: a project that requires review cannot be waived by a task
  (refused at admission), and a task that asks for review gets one even when the project does
  not require it. Only both saying no may skip it.
- An unknown outcome blocks and never auto-retries.
- Verification is bound to a candidate fingerprint and a checks digest.
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
  budget cannot cover, a launch this machine cannot resolve). Reporting "ready" for a task the
  run refuses would be answering a different question than the user asked.
- An authorization binds the **effective configuration**, not just the task: profile, per-role
  agents and drivers, model selections, limits, write permission, and the *resolved launch* -
  the client entry point, the interpreter that starts it, the launcher argv and the DSH
  home/profile. Changing any of them - including `HFLOW_ACPX_NODE` or `HFLOW_ACPX_CLI` - changes
  the digest and the old approval stops applying. The launch is resolved once and then consumed
  by the driver, never re-selected after the check: a bound `DSH_HOME` is set on every child
  process, and a launch that bound none has it *removed* from the child environment rather than
  inherited. An artifact written before config binding still loads and still keys its own ledger
  row, but it cannot authorize a run that resolved a configuration.
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
completion, or a substantive `changes_requested` on the current candidate with non-empty findings
when the policy allows it. Everything else stops the run: a mixed ERROR, an **undeclared exit
code** (a non-zero code the policy does not name never repairs), a timeout, a check that left
descendants, an incomplete capture, a launch that never happened, a malformed review, an unknown
outcome and a cancellation. A policy only ever classifies a failure it can see: a check id the
project contract never runs produces no failed row, so that entry can buy nothing, and a failure
whose id or exit code the policy does not declare is recorded as a `not_a_business_failure`
decision naming the check, its exit code and what the policy declared for it. An evidence row
written before storage v5 carries no observed reason and is therefore ineligible - the reason is
never back-filled from a stored exit code. The root's `max_repairs` stays a ceiling that the
dispatch transaction enforces; a task file that says `max_repair_cycles: 1` still opts into
nothing.

What it still is **not**, and cannot be read as more than:

- **No `repair` command.** A repair is a decision taken inside one live run, never an action you
  invoke on a finished one.
- **No revival of a historical `BLOCKED` run.** A run ended by a verdict, a stop or a block is
  never reopened for a second attempt.
- **No retry of an environment or transport failure.** Only a declared business check failure or a
  substantive reviewer rejection - once - may buy the second attempt.

Offline checks never look like a real execution: `kind=fake` launches no process, so its evidence
says `not_launched` and it can never trigger a repair. An offline test that wants the repair rule
exercised has to *declare* the clean process exit it is modelling
(`FakeCheckRunner(verdicts=..., exit_reasons={"unit": "nonzero_exit"})`). That is a deliberate
modelling choice inside the offline facility, not evidence about a live harness.

Not built: cooperative (protocol) cancellation on the selected launch path; a `hflow repair` or
`hflow integrate` command; integration/publish delivery; reuse-research automation; teams and
native subagents; real billing observation; metrics against a direct-DSH baseline. A reviewer's
answer is read as text and decoded against the contract - the harness is not asked for structured
output, and no model is ever asked to repair a malformed verdict.

Not built around configuration either:

- **A profile selects which agent, transport and permission each role uses. It does not yet
  select the model inside a DSH launch.** The launcher still starts the DSH profile its driver
  was built with; `model_selection` is recorded, reported and bound by an approval, but it is
  not passed as a launcher flag. `hflow doctor --profile <id>` prints the exact resolved argv.
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
  either stop state. Two limits stay stated: the command-line `cancel` cannot reach a child
  started by another controller process, and this covers the one driver that creates processes
  here (`AcpxDshDriver`); another driver must coordinate its own spawn the same way.
- **An offline profile is all-or-nothing.** A profile may bind every role to the offline fake
  (for development) or every role to a real transport; mixing the two is refused rather than
  half-scripted.
- **The launch is bound by paths and argv, not by program content.** A run records *which*
  client entry point, interpreter and launcher it will start; it does not hash those programs, so
  replacing a file at the same path does not change the approval. Version or content identity of
  the client remains part of the capability record, not of the binding.

Not verified, even where something works on one binding:

- **Stopping.** The forced stop is a *Windows* Job Object property, so it covers what the
  boundary owns. It does not follow a descendant that leaves the job, does not exist on other
  platforms, and a stop that cannot be confirmed stays `still_running`/`unknown`.
- **Nothing is sandboxed.** `command` checks and a real worker run with the current user's
  rights: no filesystem confinement, no credential confinement, and no protection against
  another process of the same user changing the workspace, the authorization artifact or the
  SQLite ledger.
- **A stop is local.** It stops a process on this machine; it does not prove that a remote
  model request stopped, that remote billing stopped, or that any usage figure is known.
- **Remote termination and billed usage remain unknown**, recorded as `null` and never as `0`.
- **Unattended execution is disabled**, and no new live task is authorized by anything in this
  repository - each live task needs its own explicit approval (`docs/operations.md`).

## Driver contract tests

```sh
python -m pytest -q tests/test_cancel_routing.py         # 24 tests: a stop reaches the live role, wins the handoff, and is never undone
python -m pytest -q tests/test_authorization.py          # 15 tests: the authorized real-run gate
python -m pytest -q tests/test_driver_acpx_dsh.py        # 30 tests, no model, no credential
python -m pytest -q tests/test_review.py                 # 37 tests: the review output grammar
python -m pytest -q tests/test_review_wire.py            # 21 tests: reviewer verdict -> receipt
python -m pytest -q tests/test_real_client_review.py     # 3 tests: installed acpx + mock agent, no model
python -m pytest -q tests/test_packet_wire.py            # 18 tests: role packets -> pinned acpx + input-sensitive agent, no model
python -m pytest -q tests/test_saved_review_replay.py    # 8 tests: the recorded live review, replayed offline
python -m pytest -q tests/test_local_finalization.py     # 13 tests: one later decision, recorded through the Store
python -m pytest -q tests/test_concurrency.py            # 8 deterministic thread/cancel-orderings tests
python -m pytest -q tests/test_m2_slice.py               # 5 tests: Git worktree -> frozen candidate
python -m pytest -q tests/test_cli_m2_cleanup.py         # 15 tests: the same flow through the CLI + guarded clean
python -m pytest -q tests/test_dispatch_gates.py         # 19 tests: pre-dispatch gates, loop allowance, packet bound
python -m pytest -q tests/test_check_resources.py        # 21 tests: bounded output, artifacts, minimal environment, reference round-trip
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

The per-file numbers were checked against the current tree with `python -m pytest --collect-only`
(collection only, nothing executed); they change as tests are added.

The driver tests run the real launch/observe/stop code against a test-only stand-in for the
acpx CLI plus stub agents that are **separate processes** - including one that ignores
cancellation and holds a helper child, so a decorative process boundary would fail the test
rather than pass it. `real_client_checks.py` is the one that exercises the *installed* acpx:
a metadata probe and a full one-shot `exec` against the project's mock agent, both with zero
model calls.

Two of the `tools/` commands in this README need a warning label, because both can write
something: `python tools/m0_probe/write_results_doc.py` regenerates `docs/m0-results.md` - a
**generated M0 snapshot**, whose "live forced stop still `not_tested`" conclusion was written
before the later M2 trial A and has not been regenerated since (editing that file by hand will
be overwritten; the current status is the table at the top of this README) - and
`python tools/m2_live/replay_review.py <run> --finalize` writes a delivery decision through the
controller/Store path, which needs its own explicit approval and is not part of any offline
test run.

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
