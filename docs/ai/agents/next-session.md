# Next-session handoff — 2026-10-16 (after batch N)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch N delivered (offline only)

Batch N followed the ninth GitHub read (ADR 0001 addendum "2026-10-16": acpx, ACP v1 and DSH's
ACP/sandbox/Windows-process source unchanged; DSH alpha.4 tagged; #9845, #9818, #9831, #9735 new)
and a prior-art survey (mergetrain, worktrunk, codex, vibe-kanban, ...). User rulings 2026-10-16
shaped the operator loop:

- **Shorter loop.** `hflow approve --task <t.json> --profile <id> --allow-writes --run` resolves,
  writes the plan, shows it, takes the typed code and approval text, and runs it. `hflow status`
  with no run id is the inbox (Running / Ready / Attention, one `next:` each); `runs --needs-you`
  and `status --last` are gone. `hflow integrate <run-id>` continues an integration from any state
  (prepare -> typed confirmation code -> apply or the hand-off -> reconcile) and cleans the run
  worktree once integrated (`--keep-worktree` opts out). HFlow still never merges into the user's
  checkout: a checked-out target is a hand-off the user runs (ruling 2026-10-16).
- **Write opt-in is recorded.** `prepare/approve --task --allow-writes` puts it in the plan and the
  binding; `approve/run --plan` no longer read `HFLOW_ALLOW_WRITES`.
- **Agent environment.** Secret-like variables other than `DEEPSEEK_API_KEY` are withheld from the
  DSH child (names recorded). The npm shim's `node` is now bound by content.
- **Fixes**: a branch checked out under another letter case was movable by `integrate apply`; the
  shared Git metadata is compared around integration checks and before `clean`; a superseded owner
  could still register a launch; acceptance did not bind the receipt to its run/attempt/revision; a
  failed COMMIT wedged the store; a git error after acceptance escaped as a traceback; reviewer and
  repair packets showed only references for a failed check and let check output start packet lines.
- **Simpler code**: `acpx_dsh.py` split into `drivers/launch.py` + `drivers/primitives.py` (3751 ->
  ~2640 lines), doctor in `doctor.py`, one git runner, `_attempt_cycle` in phase methods, five dead
  CLI flags removed, README and operations.md cut down.

## Next step: the first live small task (needs its own explicit approval and budget)

The live budget is still **0**. Run this checklist only after the user approves this specific
task (AGENTS rule 10).

0. **DSH route: the npm CLI** (`@deepseek-ai/dsh@0.2.0-rc.2`, installed `--ignore-scripts` at
   `%LOCALAPPDATA%\HFlow\dsh\0.2.0-rc.2`, found by itself when PATH has no `dsh`; keep the Desktop
   shim off PATH because of #9626). `doctor`'s `dsh launcher` line and `prepare`'s content note must
   say `dsh=npm (data dir)`, with no `warning ... DSH #9626` line. A plan prepared for the npm route
   before batch N must be prepared again (the node it runs is now part of the binding).
1. Use a shell where (`hflow` needs `python -m pip install -e .` once):
   - `DEEPSEEK_API_KEY` is set, or `DSH_HOME` is bound to a home that holds `.credentials.yaml` in
     the version-1 layout and boots headless. A flat pre-release file is rewritten by DSH at boot
     (#9845) - a write HFlow's stat-only check cannot see. Never create a home from nothing (#7978).
   - no proxy variable is set in both casings (#9830), and acpx 0.17.1 is found via
     `HFLOW_ACPX_CLI` or `.probe/acpx`.
   - The task: acceptance rests on HFlow's own approved checks, needs no DSH shell call (#9682/#8497
     no console, #9687 pwsh token, #9831 unelevated grant - all unconfirmed causes of failing shell
     calls that still end `end_turn`), reads no large file (one ACP line over 1 MiB voids the turn),
     and lives in a **small** repository (#9818: the first sandbox provisioning walks the whole tree
     silently). Its checks run an interpreter from outside the worktree (`python -m pytest ...`),
     because after a turn the tree may carry the Low label (#9666/#9819).
2. `python tools/m0_probe/real_client_checks.py all` (model-free). If SDK 1.8.1 has been published
   and resolved, add it to `CHECKED_SDK_VERSIONS` only after these checks pass.
3. `hflow doctor --profile <id>`: every readiness line usable (including `writes`), credentials not
   "would be REFUSED". In step 4's `prepare` preview, the `agent environment: ...` note must name
   nothing DSH needs (secret-like names other than `DEEPSEEK_API_KEY` are withheld).
4. (Once per repo/machine: `hflow init project`, `hflow init profile <id>`; per task
   `hflow init task <id> --goal ... --allow ...`, then answer the reuse fit test by hand.) Optionally
   `hflow prepare --task <t.json> --profile <id> --allow-writes` as a zero-model preview. Keep the
   turn well under DSH's 300 s stream timeout (#9515: a lost tool-call start can hang; HFlow's
   wall-clock limit and the Job Object stop are the only bound).
5. The user, in their own terminal: `hflow approve --task <t.json> --profile <id> --allow-writes
   --run`. It shows the plan and its approval code; the user types the code and their own approval
   text, then it runs in that terminal (Ctrl+C reaches the owner). An agent never types this.
6. Follow the `next:` lines (`hflow status` lists what needs you). For `ACCEPTED/LOCAL_CANDIDATE`:
   `hflow integrate <run-id>` at a terminal, type its code; when the target is checked out run the
   printed `git -C <repo> merge --ff-only <commit>` yourself, then `hflow integrate <run-id>` again.
   It cleans the run worktree once integrated; a refusal or `PARTIAL` is printed - record it, never
   force it.
7. If anything is interrupted: Ctrl+C in the owner's terminal, not `cancel` from another one. Then
   `resume` (exit 5 means the owner may still be alive; wait), then `ledger settle` with an
   attestation in your own words. Never re-run the same authorization.
8. After the run, check the worktree parent under the data dir with `icacls` (#8312/#9666 leftovers;
   #9831 needs it caller-owned), and confirm nothing outside the worktree changed (#9693: ACP
   sessions compose no permission presets) - `status`/`report` show the user-checkout fingerprint.

Settled: the ACP SDK is not pinned (2026-10-14); DSH's turn-time `git add` (#9382) is
Web/Desktop-only by source - keep a bound home's acp bundles at `[dsh-base, dsh-acp-app]`.

Settled 2026-10-16: the one-off M2 offline replay (`tools/m2_live/replay_review.py`,
`Store.finalize_offline_reprocessing`) wrote a receipt outside the controller; it was retired
rather than relaxing AGENTS rule 2 (the user left the choice to the recommendation). Its last
version is in `2ea0020`; `report` still prints the provenance of the receipt it wrote.

Only after that real evidence comes Team/DAG work. Before parallel tasks:

- DSH #1485/#9612/#9615: concurrent instances sharing one `DSH_HOME` corrupt session membership;
  use per-run homes seeded from a booting profile (HFlow keeps and prunes them), or serialize.
- `integrate` compares the shared Git metadata around its own checks, but not against the source
  run's snapshot before `integrate prepare`; with parallel runs another worker could plant a filter
  or merge driver in between.
- Prior art for that phase: mergetrain's train/bisect landing and named-Job-Object keeper (owner
  liveness without attestation), codex's spawn-slot reservations, per-task homes, synchronous
  fan-out/join.

Do not rewrite historical evidence or reactivate the archived `workflow` installation.
