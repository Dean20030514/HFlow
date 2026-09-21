# M2 controlled live acceptance — result

Approved starting baseline: `2603a1e`. Each tested execution is a **different fact** from that
baseline, and the task's base commit is different again:

| Fact | Value |
|---|---|
| approved starting baseline | `2603a1e` |
| A tested execution runtime | `988a531` (binding-identical to the baseline for the launch/stop path) |
| B tested execution runtime | `7c8b051` (failed); fix landed in `27c54ae` |
| task base commit (synthetic project) | `5526040ee01672e321c10c4b98bff9d79c30cbe4` |
| runner commit vs task base commit | different things: the runner is HFlow, the base is the project under test |

**New live top-level tasks: 3** — A: 1 (of 1). B: 1 (of 2). No other dispatches, no Codex, no
planner, no subagent. Both authorizations are recorded with the user's own text.

## A — real forced stop: **PASS**

| Fact | Value |
|---|---|
| dispatch count | **total 1 top-level task** (the permitted one); **additional = 0** |
| helper started by DSH | yes — `helper_ready.json` written by the process DSH launched |
| ownership proven by the OS | `IsProcessInJob(this invocation's job)` = **true** |
| boundary before the stop | **8** processes |
| stop mechanism | `forced` (protocol cancel remains `unsupported_for_selected_exec`) |
| stop latency | **2.03 s** |
| helper after the stop | gone; heartbeat stopped |
| client after the stop | gone; boundary empty |
| late `ACCEPTED` | no — the run ended blocked, with no receipt |
| evidence | `.probe/live-forced-stop/evidence-B.json` |

The probe's `extra_dispatches` field reads `1`: it is the **total** number of attempts recorded
for that run, not a count of tasks beyond the allowance. There was no authorization violation.

A's scope, stated narrowly: it exercised the launcher, the Job boundary and the forced-stop path
with the client in `approve-reads` mode (the config it ran with carried an invented
`permissionPolicy` key that the client ignored, so the client default applied). It did **not**
test `approve-all`, and it did not test every kind of tool process.

Consequence: `forced_local_stop_live = passed` **for this machine, this acpx/DSH version and this
binding**. It does not certify cooperative cancellation, a strong sandbox, or that remote
billing stopped.

Two corrections were needed to run A at all, both outside the launch path:

- the probe counted attempts **globally**, so a second, separately approved trial could never
  run. It now counts per `--authorization-id`; history was not deleted.
- run notes were cleared by terminal transitions, so audit facts moved to their own `run_notes`
  table.

## B — real M2 small change: **blocked at the implementer step**

Five facts, kept apart:

1. **one authorization allowance consumed** under the policy in force at the time
   (`AUTH-m2-live-1`, used 1/2, `provided_by: user`, the user's text verbatim);
2. a **known client startup/config rejection** — `Invalid config nonInteractivePermissions ...
   expected deny or fail`;
3. **no DSH session or prompt dispatched**: the client's stdout was empty, and its record has no
   `session/new` and no `session/prompt`;
4. **no candidate, no formal verification, no review** — so the reviewer allowance was never
   touched;
5. **no authority to repurpose** the unused reviewer allowance, and no retry.

The stored run state is the coarse `outcome_unknown` and stays as history: the driver correctly
refuses to treat "client exited without a stop reason" as anything better. The known pre-prompt
failure is recorded in the run's notes rather than rewritten into the state, and it was **not**
routed into session recovery or an automatic retry.

Root cause: the write-permission wiring added for B wrote `nonInteractivePermissions: "allow"`
plus a `permissionPolicy`/`approveAll` pair. The installed client accepts only `deny`/`fail`
there, and expresses the read/write decision as `defaultPermissions`. The keys were invented
instead of read from the client.

Fix (`27c54ae` + the role/permission binding in `fix(auth)`): permission is derived from the
**request** (role + approved mode), so a reviewer never inherits the implementer's write
approval, and the generated key set is asserted against a strict allowlist. Model-free evidence
after the fix:

```text
version:           PASS  (reports 0.17.1 through the production launcher)
config_reads:      PASS  (defaultPermissions=approve-reads, no unsupported keys)
config_writes:     PASS  (defaultPermissions=approve-all,   no unsupported keys)
permission_reads:  PASS  (client answered reject-once)
permission_writes: PASS  (client answered allow-once)
mock:              PASS  (full one-shot exec against the mock agent)
```

The two `permission_*` checks prove the **client's** policy mapping, not DSH native tool
enforcement.

## B — second attempt (`3dbfeae`, authorization `AUTH-m2-live-2`): candidate valid, delivery blocked by a driver gap

Both allocated invocations were used, in order: implementer `I-xf3sez1lho`, then reviewer
`I-vc5pcccfog` (fresh session, `approve-reads`). The pipeline ran **end to end for the first
time**:

| Stage | Result |
|---|---|
| implementer invocation | `completed` (`stopReason=end_turn`), `approve-all` |
| its own work | ran the failing tests, edited `src/reportkit/__init__.py`, re-ran them |
| frozen candidate | `refs/hflow/candidates/R-gkb3ld97x8/A-7f2pbp4teu` → `499ece7043fe3267b4ff89f9a5b5bc1d70c42481`, 4 insertions, in scope, worktree clean |
| fixed program checks | `unit` passed, exit 0 (evidence `E-zgamyka8s3`) |
| reviewer invocation | `completed`, separate session, `approve-reads` |
| **reviewer verdict** | **`accepted`** — AC-1, AC-2, AC-3 all `pass` |
| controller outcome | **`BLOCKED` / `review_rejected`; no receipt** |
| allowance | `AUTH-m2-live-2` used 2/2 |

The candidate is a genuine, correct fix: `summarise(None)` raises `TypeError` mentioning a
sequence of strings, `average([])` raises `ValueError`, valid-input behaviour unchanged, tests
and configuration untouched.

**Why it was not accepted** — the reviewer found the cause itself, and it is in HFlow, not in the
candidate:

```text
AcpxDshDriver.collect() always sets review=None;
_review() therefore always returns changes_requested.
No real reviewer verdict can reach acceptance.
```

The reviewer *did* return the contract's structured object (a `verdict: "accepted"` block with
five findings) inside its final message, but the driver never extracts it: prose arrives as
event text, the driver reports `review=None`, and the controller correctly treats a missing
verdict as `changes_requested`. So a real reviewer turn deterministically blocks delivery —
exactly the "production reviewer result wiring not yet implemented" gap, and it should have been
closed and validated offline before a live reviewer was dispatched.

Per the authorization this stops the attempt: both allocations are consumed, there is no refund
or retry, and the candidate, the workspace `R-gkb3ld97x8`, the old failed run `R-0mh0gtrz9r` and
its workspace are all preserved for inspection.

**Next minimal task (bounded, offline):** implement the reviewer-result extraction required by
the existing contract — parse the reviewer's structured object from its final message, attach it
to the review evidence, and validate it against the mock (structured, malformed, and absent
shapes). Until that exists, no real review can produce `ACCEPTED / LOCAL_CANDIDATE`.

**Done offline, with no new model call:** see `docs/m2-review-wire-repair.md` — the verdict is
now decoded from the reviewer's own final message, verified/cancelled/unbound turns cannot
supply one, an unusable verdict blocks as `review_protocol_error` instead of being described as
a substantive rejection, and the recorded reviewer bytes replay through the acceptance
predicates in an isolated evaluation. The record above is unchanged: this run stays `BLOCKED` /
`review_rejected` with no receipt.

## Authorization trust model — stated honestly

**Trusted-local, user-attested operation.** A human creates the approval; the artifact records
that decision and bounds its consumption; the executor is trusted not to forge approvals or
modify the controller or its database.

Enforced: the binding (mode, driver, project, repository path, base commit, task digest, task
path), the per-id consumption cap (one guarded UPDATE + CHECK constraint), and `provided_by`
being the literal value `user`.

Not enforced, and not claimed: provenance is **not authenticated** — nothing distinguishes bytes
a user typed from the same bytes written by the executing agent, because there is no approval
issuer and no protected store outside the executor's reach; and a **fresh authorization id
resets the allowance**, so the cap bounds one id rather than a person or a day.
`tests/test_authorization.py` asserts both limits, so they are recorded facts rather than
unstated assumptions. Real anti-forgery would need a separate issuer or store and is a deliberate
design decision, not another string field.

## Effective permissions per role

| Role | `defaultPermissions` | Note |
|---|---|---|
| implementer, write-capable (`HFLOW_ALLOW_WRITES=1`, disposable worktree) | `approve-all` | auto-approves **all** tool permission requests, not only file writes |
| implementer, default | `approve-reads` | reads proceed, writes refused |
| reviewer | `approve-reads` | never inherits the implementer's write approval |

`HFLOW_ALLOW_WRITES` is a local request, not authority: honored only for a disposable worktree
created from a fixed base, disclosed in the run's notes, and never applied to the review
invocation. acpx permission mediation is not a sandbox — its filesystem checks do not confine
arbitrary shell commands — and a disposable worktree does not confine anything by itself.

## What is still unknown

| Item | State |
|---|---|
| `forced_local_stop_live` | passed for the A binding |
| `protocol_cancel` | `unsupported_for_selected_exec` (unchanged) |
| reviewer isolation | `prompt_only` / audit-only — not a sandbox |
| real DSH implementer produced a valid, in-scope candidate | **achieved** (attempt 2) |
| fixed program checks on that candidate | **passed** |
| independent real review | **ran and returned `accepted`** |
| controller-owned `ACCEPTED / LOCAL_CANDIDATE` | **not achieved** — the driver discards the reviewer's structured verdict |
| billed usage / remote termination | unknown |
| unattended execution | disabled |
