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
read TaskSpec -> deterministic admission -> SQLite create-or-reuse run
  -> transactional budget reservation -> driver invocation (fake, or acpx -> DSH ACP)
  -> verification evidence over a frozen candidate -> independent review verdict
  -> controller-generated ResultReceipt -> status / report
```

Both drivers run through the same controller; `--driver fake` is the offline one, and the real
one needs an authorization artifact (see `docs/operations.md`). Admission now refuses a spec it
cannot honour - an unanswered or failed reuse/adapt fit test, or a delivery level this build
cannot reach - instead of warning and delivering less.

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
| the same tree after two annotation-only edits to the new test file | `python -m pytest -q tests/test_admission_dispatch_boundary.py tests/test_contracts.py` | `44 passed, 1 skipped` (exit 0) |
| current working tree (collection only, nothing executed) | `python -m pytest -q --collect-only` | `226 tests collected` |

The first row is a full-suite result, the second is a targeted re-check after the last edit, and
no full suite was re-run after that edit. The committed HEAD on its own has not been re-measured;
its recorded count was `197 passed, 1 skipped`, which predates the added tests.

Four tests skip themselves when their precondition is absent rather than pretending to pass:
the directory-link test in `test_contracts.py` (the skip seen above), the installed-acpx test in
`test_real_client_review.py` (needs `node` and the pinned client), and the two recorded-ledger
replays (`test_saved_review_replay.py`, `test_local_finalization.py`, which need this machine's
runtime database). A run that reports more than one skip is telling you which preconditions were
missing, not that the suite failed.

## Commands

```sh
hflow doctor   --json                     # read-only environment probe, no model calls
hflow run      --task examples/task.json --project-root . --driver fake --json
hflow status   R-xxxxxxxxxx               # pure SQLite read, zero model calls
hflow report   R-xxxxxxxxxx --json        # receipt + evidence, zero model calls
hflow resume   R-xxxxxxxxxx               # reconcile an interrupted attempt; never re-dispatches
hflow cancel   R-xxxxxxxxxx
hflow clean    R-xxxxxxxxxx               # preview releasing the run's worktree
hflow clean    R-xxxxxxxxxx --apply       # remove it; the candidate and receipt are kept
hflow schema                              # generated JSON Schema for every contract
```

An offline M2 candidate end to end, including cleanup, is runnable as a demo:

```sh
python examples/m2_cli_demo.py
```

Runtime data (SQLite, evidence) goes to `%LOCALAPPDATA%\HFlow` on Windows or
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
| Machine binding | not implemented yet | the schemas exist (`MachineProfile`, `AgentBinding`, `CapabilityReport`) and a driver is resolved from a binding, but no profile file is loaded from disk yet |
| Runtime data | platform data dir | SQLite plus evidence references; outside the repo |

`contracts.py` is the single definition of every structure; JSON Schema is generated
from it (`hflow schema`). There is no second hand-written schema to drift.

## Non-negotiables in the code

- The controller generates `ResultReceipt`; a worker cannot report `ACCEPTED`.
- Budget is reserved in the same transaction that records the dispatch.
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
- An unknown outcome blocks and never auto-retries.
- Verification is bound to a candidate fingerprint and a checks digest.
- A delivery can be recorded as a **later decision** about an execution that already ended (an
  offline reprocessing of recorded evidence). Such a receipt carries `provenance` naming the
  original decision, its build and the evidence it came from, and `report` prints that next to
  the delivery - so a recovered delivery never reads as the original run's own success. The
  write only ever moves a blocked run forward, never over a cancellation intent or a different
  decision, is idempotent for the same evidence, and consumes no allowance.
- `status`, `report` and `doctor` make no model calls.

## Not implemented (do not assume otherwise)

Not built: cooperative (protocol) cancellation on the selected launch path; a repair cycle;
integration/publish delivery; reuse-research automation; teams and native subagents; real
billing observation; metrics against a direct-DSH baseline. A reviewer's answer is read as
text and decoded against the contract - the harness is not asked for structured output, and no
model is ever asked to repair a malformed verdict.

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
python -m pytest -q tests/test_authorization.py          # 12 tests: the authorized real-run gate
python -m pytest -q tests/test_driver_acpx_dsh.py        # 24 tests, no model, no credential
python -m pytest -q tests/test_review.py                 # 37 tests: the review output grammar
python -m pytest -q tests/test_review_wire.py            # 21 tests: reviewer verdict -> receipt
python -m pytest -q tests/test_real_client_review.py     # 3 tests: installed acpx + mock agent, no model
python -m pytest -q tests/test_saved_review_replay.py    # 8 tests: the recorded live review, replayed offline
python -m pytest -q tests/test_local_finalization.py     # 13 tests: one later decision, recorded through the Store
python -m pytest -q tests/test_concurrency.py            # 8 deterministic thread/cancel-orderings tests
python -m pytest -q tests/test_m2_slice.py               # 5 tests: Git worktree -> frozen candidate
python -m pytest -q tests/test_cli_m2_cleanup.py         # 15 tests: the same flow through the CLI + guarded clean
python tools/m0_probe/check_process_boundary.py          # Job Object teardown, standalone (Windows)
python tools/m0_probe/real_client_checks.py all          # real acpx: version + mock-agent round trip
python tools/m2_live/prepare_m2_live.py                  # build the real M2 task package (dispatches nothing)
python tools/m2_live/replay_review.py R-gkb3ld97x8       # replay a recorded review; no model, no writes
python tools/m2_live/replay_review.py R-xxxxxxxxxx --finalize  # record one later decision (needs explicit approval)
```

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
