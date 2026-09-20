# M2 controlled live acceptance — result

Baseline for this round: `2603a1e` (user-approved) with the minimal wiring recorded in
`988a531` and `7c8b051`. Execution binding: acpx 0.17.1 → official DSH ACP (local DSH
0.1.5-rc.1), `workspace.mode=worktree`, independent synthetic Git project.

**New live top-level tasks this round: 3** — A: 1 (of 1). B: 1 (of 2). No other dispatches, no
Codex, no planner, no subagent. Both authorizations are recorded with the user's own text.

## A — real forced stop: **PASS**

| Fact | Value |
|---|---|
| dispatch | 1 top-level task through the existing probe and helper |
| helper started by DSH | yes — `helper_ready.json` written by the process DSH launched |
| ownership proven by the OS | `IsProcessInJob(this invocation's job)` = **true** |
| boundary before the stop | **8** processes |
| stop mechanism | `forced` (protocol cancel remains `unsupported_for_selected_exec`) |
| stop latency | **2.03 s** |
| helper after the stop | gone; heartbeat stopped |
| client after the stop | gone; boundary empty |
| extra dispatches / late acceptance | 1 invocation / no `ACCEPTED` |
| evidence | `.probe/live-forced-stop/evidence-B.json` |

Consequence: `forced_local_stop_live = passed` **for this machine, this acpx/DSH version and
this binding**. It does not certify cooperative cancellation, a strong sandbox, or that remote
billing stopped.

Two corrections were needed to run A at all, both unchanged in the launch path:

- the probe counted attempts **globally**, so a second, separately approved trial could never
  run. It now counts per `--authorization-id`; history was not deleted.
- the run's note table was cleared by terminal transitions, so audit facts moved to their own
  `run_notes` table.

## B — real M2 small change: **blocked at the implementer step (local config defect)**

One top-level task was sent. The client **exited during startup, before creating a session**:

```text
Invalid config nonInteractivePermissions in ...\.acpx\config.json: expected deny or fail
```

Zero stdout from the client, no `session/new`, no `session/prompt` — so **no model work
occurred**, and the "unknown" outcome is a startup failure, not an interrupted agent. One of
the two authorized submissions is consumed; per the authorization there is no retry.

Root cause: the write-permission wiring added for B wrote `nonInteractivePermissions: "allow"`
and a `permissionPolicy`/`approveAll` pair. The installed client accepts only `deny`/`fail`
there, and expresses the read/write decision as `defaultPermissions` with
`approve-all` / `approve-reads` / `deny-all`. The keys were invented rather than read from the
client.

Fix (`7c8b051`): the driver now writes `nonInteractivePermissions: "deny"` plus
`defaultPermissions` (`approve-reads`, or `approve-all` only when `HFLOW_ALLOW_WRITES=1` and
the workspace is a disposable worktree). `tools/m0_probe/real_client_checks.py` gained a
**config check** that makes the installed client parse the generated file in both modes, so
this class of failure is caught offline:

```text
version:       PASS  (reports 0.17.1 through the production launcher)
config_reads:  PASS  (defaultPermissions=approve-reads)
config_writes: PASS  (defaultPermissions=approve-all)
mock:          PASS  (full one-shot exec against the mock agent)
```

The fix changes the driver's config writer, so this acceptance package **pauses here**: a new
live B run needs fresh authorization, and A's PASS stands for the launch/stop path it actually
exercised (A does not write files and the new config merely selects a permission default).

## What is still unknown

| Item | State |
|---|---|
| `forced_local_stop_live` | passed for this binding |
| `protocol_cancel` | `unsupported_for_selected_exec` (unchanged) |
| reviewer isolation | `prompt_only` / audit-only — not a sandbox |
| M2 real change delivered by DSH | **not achieved**: the implementer never started |
| billed usage / remote termination | unknown |
| unattended execution | disabled |
