# Next-session handoff — 2026-10-06 (after batch I)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch I delivered (offline only)

Contract: `docs/batch-i-integration-plan.md`. Operator view: `docs/operations.md`,
"Integrating an accepted candidate".

- **I2 controlled integration** — `hflow integrate prepare | apply | reconcile | show`
  (`src/hflow/integrate.py`, storage v8 `integrations`, `IntegrationRecord`/`IntegrationReceipt`).
  One checked commit on an existing local branch (`squash` when the target has not moved,
  `replayed` via `merge-tree --merge-base` when it has), checks re-run in a worktree of its own
  (`integration-check` evidence), operator approval via `--expect-target`, intent before a
  compare-and-set `update-ref`, never a checked-out branch (hand-off + `reconcile` observes a
  hand merge), crash reconciliation without re-running the update or the checks. The run's own
  `ResultReceipt` stays `LOCAL_CANDIDATE`.
- **I1 ledger close-out** — `hflow resume` on an ended run closes ledger entries left
  `reserved`/`requested`/`started` once the owner is provably gone (from the run's own confirmed
  stop when that stop ended the run, otherwise `unknown`/`launch_unknown`), after which
  `hflow ledger settle` applies. Remaining gap: a pre-v6 ended run with a recorded controller pid.
- 2026-10-06 GitHub survey recorded in ADR 0001 (no transport change; acpx pin 0.17.1 kept).

## Next step

1. A small real task, end to end, under a **separately approved** live allowance (the new live
   budget is still **0**; each live task needs its own explicit approval): `prepare` → `run` with
   an authorization → `ACCEPTED/LOCAL_CANDIDATE` → `hflow integrate prepare` → `apply` (or the
   hand-off when the target branch is checked out) → `status`. Integration itself calls no model.
2. Only after that real evidence: Team/DAG work. Before parallel tasks, note DSH #1485 (concurrent
   instances sharing one `DSH_HOME` corrupt workspace session membership) — use per-run homes or
   serialize.

Do not rewrite historical evidence or reactivate the archived `workflow` installation.
