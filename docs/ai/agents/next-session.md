# Next-session handoff — 2026-10-14 (after batch M)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch M delivered (offline only)

Batch M followed the eighth GitHub survey (ADR 0001 addendum "2026-10-14 (evening)": acpx, ACP
and DSH code unchanged, #9626 unfixed; new reports #9693, #9682/#9687, #9711, #9666) and a
five-part code audit, built in five parallel lanes.

- **Ctrl+C works while HFlow waits.** On Windows `Popen.wait(timeout)` holds a console Ctrl+C
  until it returns (observed: 8 s for an 8 s wait). Every long wait - the agent turn, the force-stop
  grace, the read-only client check, each approved check - now waits in 0.1 s slices
  (`winjob.wait_interruptibly`), and an interrupted turn or check has its Job Object terminated at
  once (nothing recorded as a confirmed stop; `resume` reconciles as before).
- **No PATH edit for the npm DSH.** A launch looks for `dsh` on PATH, then in
  `<data-dir>/dsh/0.2.0-rc.2/node_modules/.bin`; `doctor`/`prepare` say which (`dsh=npm (data
  dir)`), and the content binding is the same as for a PATH-found shim. The Desktop shim is no
  longer recommended (#9626).
- **Launch hardening**: `cmd.exe /d /c` (no AutoRun), a batch launcher path with cmd
  metacharacters is refused, the agent no longer inherits `GIT_DIR`/`GIT_WORK_TREE`/...,
  `ResumeThread` failure is caught. The `sandbox` line now says it is the shell runner's mode, not
  a file-tool boundary (#9693).
- **Workspace and checks**: snapshots never walk into NTFS junctions (a junction loop hung
  `manifest`/`status`), deny rules also match the resolved spelling (`.git.`, 8.3 names), checks no
  longer inherit `PYTHONPATH`/`NODE_PATH`/`NODE_OPTIONS` (they get `USERNAME`), a timed-out check
  stays `timed_out`, acceptance blocks `evidence_stale` when a check changed a tracked file or HEAD.
- **Git**: the run worktree is created with `git worktree move` (the old `worktree repair`
  rewrote the user's other worktrees' `.git` files - rule 12), auto-maintenance is off, hooks point
  under a system file, output is decoded as UTF-8, a half-failed `clean` is reported `PARTIAL` with
  what is left (was "nothing was removed" and a stuck run), bare-repository worktrees can be
  cleaned, refusals print read-only inspect commands.
- **Controller/store**: a continued run passes the same dispatch preconditions and preflight as a
  new one (a resubmission without credentials used to spend a submission), an authorization-id
  conflict refuses before any write, typed store refusals replace substring matching, the
  migration re-reads its version under the write lock.
- **CLI**: one JSON loader (BOM accepted, UTF-16 and unreadable files refused in one line), ledger
  errors exit 5/6 instead of a traceback, lookups never create a ledger in a mistyped
  `--data-dir`, stale-plan refusals print the full `prepare` command, `integrate prepare --target`
  is prefilled from the task's base branch, `runs --needs-you` lists every live run (and "past
  its deadline"), `doctor` checks ledger health read-only, `status|report|diff --last`.

## Next step: the first live small task (needs its own explicit approval and budget)

The live budget is still **0**. Run this checklist only after the user approves this specific
task (AGENTS rule 10).

0. **DSH sandbox route (#9626) - decided 2026-10-14: the npm CLI.** Through the Desktop shim a
   `workspace-write` sandbox (DSH's default for a per-invocation home) is reported to make every
   shell tool call exit `0xC0000142`. The user chose the npm route: `@deepseek-ai/dsh@0.2.0-rc.2`
   is installed (`--ignore-scripts`) at `%LOCALAPPDATA%\HFlow\dsh\0.2.0-rc.2`, and since batch M a
   launch finds it there by itself when PATH has no `dsh` - keep the Desktop shim directory off
   PATH. `doctor`'s `dsh launcher` line and `prepare`'s content note must say `dsh=npm (data dir)`,
   and no `warning ... DSH #9626` line may appear. If a native module turns out to need an install
   script, report it rather than re-installing with scripts.
1. Use a shell where the following holds (`python -m hflow` needs `python -m pip install -e .` once,
   or `src` on `PYTHONPATH`):
   - `DEEPSEEK_API_KEY` is set in that shell, or `DSH_HOME` is bound to a home that holds
     `.credentials.yaml` and boots headless. Never create a home from nothing (DSH #7978).
   - `HFLOW_ALLOW_WRITES=1` is set.
   - acpx 0.17.1 is found via `HFLOW_ACPX_CLI` or `.probe/acpx`.
   - Pick a first task whose acceptance rests on HFlow's own approved checks and that needs no
     DSH shell call: #9682/#8497 (a host without a console - acpx starts DSH with
     `CREATE_NO_WINDOW`) and #9687 (pwsh under the restricted token) are two unconfirmed causes of
     failing shell calls that the npm route does not remove, and a failed shell call still ends
     `end_turn`. Keep it away from reading large files (one ACP line over 1 MiB voids the turn).
2. `python tools/m0_probe/real_client_checks.py all`. This is model-free.
3. `python -m hflow doctor --profile <id>`. Every readiness line must be usable, and credentials
   must not say "would be REFUSED".
4. (Once per repo/machine: `hflow init project` and `hflow init profile <id>`; per task
   `hflow init task <id> --goal ... --allow ...`, then answer the reuse fit test by hand.)
   `python -m hflow prepare --task <t.json> --profile <id> --project-root <repo> --plan-out
   <t.plan.json>` with a small task whose checks finish in seconds. Keep the turn well under DSH's
   reported 300 s stream timeout (and #9515: a lost tool-call start can hang; HFlow's wall-clock
   limit and the Job Object stop are the only bound).
5. The user runs `python -m hflow approve --plan <t.plan.json>` (or `... --run`, which then runs it in
   the same terminal, so Ctrl+C reaches the owner) in their own terminal: it shows
   the plan and its approval code; the user types the code and their own approval text. An agent
   never types this - a scripted approval is a hand-written artifact quoting the user verbatim.
6. `python -m hflow run --plan <t.plan.json>`. Then follow the `next:` lines: for
   `ACCEPTED/LOCAL_CANDIDATE`, `integrate prepare` -> `integrate apply` (or the hand-off plus
   `reconcile`) -> `clean` preview -> `clean --apply`; `status`/`report` at any point.
7. If anything is interrupted: press Ctrl+C in the owner's own terminal, not `cancel` from another
   one. Then `resume` (exit 5 means the owner may still be alive; wait), then `ledger settle` with
   an attestation in your own words (a pasted placeholder is refused). Never re-run the same
   authorization.
8. After the run, check the worktree root with `icacls`, because of the reported DSH Windows
   sandbox ACE/Low-label leftovers (#8312, #9666 reproduced on 10.0.26300, this machine's build).
   If `clean` refuses or reports `PARTIAL`, record it; never force it. Because ACP sessions do not
   compose DSH's permission presets (#9693), also confirm nothing outside the worktree changed:
   `status`/`report` show the user-checkout fingerprint comparison.

Settled 2026-10-14: the ACP SDK is not pinned (user ruling) - after any acpx reinstall run
`python tools/m0_probe/real_client_checks.py all --acpx-cli <entry>` and add a passing SDK version
to `CHECKED_SDK_VERSIONS`; DSH's turn-time `git add` (#9382) is Web/Desktop-only by source, so the
ACP path is unaffected unless a bound home's acp profile adds `dsh-web-app` (noted by
`dsh_surfaces`). A bound `DSH_HOME` for the live task should keep the acp profile's bundles at
`[dsh-base, dsh-acp-app]`.

Only after that real evidence comes Team/DAG work. Before parallel tasks:

- DSH #1485: concurrent instances sharing one `DSH_HOME` corrupt workspace session membership.
  Use per-run homes seeded from a booting profile, or serialize.
- Integrate does not re-compare the shared Git metadata against the source run's snapshot. Today
  the worker is gone by then, but with parallel runs another worker could plant a filter or
  merge driver before `integrate prepare`.
- Prior art collected 2026-10-07 for that phase: mergetrain's train/bisect landing, codex's
  spawn-slot reservations, per-task homes, and synchronous fan-out/join.

Do not rewrite historical evidence or reactivate the archived `workflow` installation.
