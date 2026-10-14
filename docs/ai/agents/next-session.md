# Next-session handoff — 2026-10-14 (after batch K)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch K delivered (offline only)

Batch K made the operator loop shorter without relaxing a rule. It followed the 2026-10-13 GitHub
survey (ADR 0001 addendum: acpx v0.19.5 is dependency-only, the 0.17.1 pin stays; DSH
`dsh-v0.2.1-alpha.2` exists but this machine's Desktop still carries 0.2.0-rc.2; ACP
schema-v1.25.0 leaves the stable v1 `StopReason` set unchanged).

- **Plan flow** (`src/hflow/plan.py`): `prepare --plan-out P` saves prepare's inputs and preview;
  `approve --plan P` re-resolves it, refuses a stale plan, and at a terminal only takes the
  approval code (first 8 hex of the binding digest) plus the user's own words, then writes the
  authorization exclusively; `run --plan P` takes every input from the plan (an extra resolution
  flag is exit 4) and refuses a stale plan. The hand-written artifact still works.
- **Approvals bind the ledger**: a real-driver binding names `ledger_path`, so an approval spent in
  one data directory is not a second allowance in another.
- **`next:` lines** (`src/hflow/hints.py`) after `run`, `status`, `report`, `resume`,
  `ledger settle` and `integrate`: read from stored facts, never a re-run or a reused approval.
  Attestation placeholders pasted verbatim are refused.
- **Integrate**: the whole contract digest must match the run's (`project_mismatch`), and
  `--project` may be omitted when `<root>/.hflow/project.json` still is that contract.
- **DSH alpha.2 hardening**: the user-level agents file is recorded; an inherited
  `DSH_AGENTS_HOME` is removed from the child; a prompt rejected by a JSON-RPC error after partial
  work is pinned as `OUTCOME_UNKNOWN` (already the behaviour).
- **Simplification**: ~300 lines of dead code and legacy store paths removed, duplicated helpers
  merged, README 1218 -> ~670 lines, root planning notes moved to `docs/history/`, stale
  `tools/m2_live` scripts deleted.

## Next step: the first live small task (needs its own explicit approval and budget)

The live budget is still **0**. Run this checklist only after the user approves this specific
task (AGENTS rule 10).

1. Use a shell where the following holds:
   - The DSH Desktop shim directory
     `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin` is prepended to `PATH`.
   - `DEEPSEEK_API_KEY` is set in that shell, or `DSH_HOME` is bound to a home that holds
     `.credentials.yaml` and boots headless. Never create a home from nothing (DSH #7978).
   - `HFLOW_ALLOW_WRITES=1` is set.
   - acpx 0.17.1 is found via `HFLOW_ACPX_CLI` or `.probe/acpx`.
2. `python tools/m0_probe/real_client_checks.py all`. This is model-free.
3. `python -m hflow doctor --profile <id>`. Every readiness line must be usable, and credentials
   must not say "would be REFUSED".
4. `python -m hflow prepare --task <t.json> --profile <id> --project-root <repo> --plan-out
   <t.plan.json>` with a small task whose checks finish in seconds. Keep the turn well under DSH's
   reported 300 s stream timeout (and #9515: a lost tool-call start can hang; HFlow's wall-clock
   limit and the Job Object stop are the only bound).
5. The user runs `python -m hflow approve --plan <t.plan.json>` in their own terminal: it shows
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
