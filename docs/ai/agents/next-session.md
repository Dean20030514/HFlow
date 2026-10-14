# Next-session handoff — 2026-10-14 (after batch L)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch L delivered (offline only)

Batch L followed the seventh GitHub survey (ADR 0001 addendum 2026-10-14: acpx, ACP and DSH code
unchanged; new DSH community reports, chiefly #9626) and a three-part code audit.

- **Easier start and follow-up**: `hflow init project|profile|task` scaffolds (exclusive create,
  `command` checks only, the reuse fit test left for the human), `hflow runs [--needs-you]
  [--json]`, `hflow diff <run> [--stat]` (recorded commits only), `approve --plan P --run` (same
  terminal gate, then exactly `run --plan`), a stale plan names the changed field and the
  environment variable behind it, approve's `next:` omits the default authorization path, doctor
  prints the acpx install command, error texts name their fix.
- **DSH sandbox mode recorded**: every launch record notes the mode DSH would run under (bound
  home's patch layers, else DSH's default `workspace-write`), and under the Desktop shim the #9626
  warning; `prepare`/`report` show both as a `sandbox`/`warning` line. Records only.
- **Bugs fixed**: a range `git diff` without `--` (ENAMETOOLONG under a deep cwd), the reviewer
  packet stating a later deadline than the one sent, a TypeError reapplying a stop without an open
  attempt, an unreadable root id list silently reset to `[]`, an identical TaskSpec refused after
  its contract changed instead of answering from history, a report line printing `resume` without
  `--data-dir`.
- **Simplification**: the legacy store API (test-only) moved to `tests/store_helpers.py`; store,
  migrate, controller, admission, CLI, drivers deduplicated (roughly 1 000 fewer source lines
  before the new commands).

## Next step: the first live small task (needs its own explicit approval and budget)

The live budget is still **0**. Run this checklist only after the user approves this specific
task (AGENTS rule 10).

0. **DSH sandbox route (#9626) - decided 2026-10-14: the npm CLI.** Through the Desktop shim a
   `workspace-write` sandbox (DSH's default for a per-invocation home) is reported to make every
   shell tool call exit `0xC0000142`. The user chose the npm route: `@deepseek-ai/dsh@0.2.0-rc.2`
   is installed (`--ignore-scripts`) at `%LOCALAPPDATA%\HFlow\dsh\0.2.0-rc.2`; in the live shell put
   `%LOCALAPPDATA%\HFlow\dsh\0.2.0-rc.2\node_modules\.bin` first on `PATH` *instead of* the Desktop
   shim directory. `prepare`'s `client` line must then say `dsh=npm`, and no `warning ... DSH #9626`
   line may appear. If a native module turns out to need an install script, report it rather than
   re-installing with scripts.
1. Use a shell where the following holds:
   - The npm DSH bin directory from step 0 is prepended to `PATH` (not the Desktop shim
     directory `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin`).
   - `DEEPSEEK_API_KEY` is set in that shell, or `DSH_HOME` is bound to a home that holds
     `.credentials.yaml` and boots headless. Never create a home from nothing (DSH #7978).
   - `HFLOW_ALLOW_WRITES=1` is set.
   - acpx 0.17.1 is found via `HFLOW_ACPX_CLI` or `.probe/acpx`.
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
   sandbox ACE/Low-label leftovers (#8312). If `clean` refuses, record the refusal; never force it.

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
