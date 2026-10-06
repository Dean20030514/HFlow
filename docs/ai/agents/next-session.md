# Next-session handoff — 2026-10-06

## User decision

The user confirmed this implementation priority:

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

This session records the decision only. Implementation is deferred to a new session.

## Starting point

- Last implementation commit: `e0fd7bf8046739623630458553e50c6e20d2d553`
  (`Fix budget recovery, session attribution, and evidence I/O`).
- Its recorded full offline validation: **1464 passed, 1 skipped in 843.65s**.
  Scoped Ruff and diff checks passed. These are historical results, not future-session validation.
- HFlow implements a controlled single-task loop through `LOCAL_CANDIDATE`;
  integration/publish delivery and Team scheduling are not implemented.
- The original live M2 run was blocked; its later acceptance came from offline
  reprocessing of recorded evidence. It does not prove uninterrupted live acceptance
  on the current build.

## Next implementation direction

1. Start with GitHub investigation, the current checkout and existing contracts.
   Recheck these handoff facts before choosing a concrete implementation plan.
2. Prioritize a small-task path from bound authorization through implementation,
   frozen candidate, checks, independent review, acceptance and controlled integration.
   Define the integration contract before implementing it.
3. Include recoverable failure handling, particularly currently unclosable OPEN
   invocation entries. Preserve evidence, budget consumption and operator decisions;
   unknown outcomes must not trigger blind re-dispatch.
4. Prove ordered failure/recovery paths offline, then validate the resulting complete
   path with a small real task under separately approved live allowance.
5. Defer DAG scheduling, parallel Workers, native subagents and broader Team features
   until this small-task loop has adequate implementation and real evidence.

This is a priority decision, not a detailed approved design or a new live authorization.
The new live budget remains **0**. Each live task still requires its own explicit approval.
Do not rewrite historical evidence or reactivate the archived `workflow` installation.
