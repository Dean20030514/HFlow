# 批次 I：受控集成与可恢复的账本收口 — 实施契约

日期：2026-10-06。代码基线：`9d4fc3b`（离线全量 `1464 passed, 1 skipped`）。
来源：`docs/ai/agents/next-session.md` 的用户优先级决定（先打通"小任务从授权到集成、异常后可恢复"的闭环，再扩展 Team），
总方案 `HFlow_DSH实施总方案_2026-09-23.md` §10.2 的集成流程，以及 2026-10-06 的 GitHub 调研（见 ADR 0001 附录）。

本文是实现契约，不是 live 授权。新的 live 预算仍为 **0**。集成本身不调用模型，可完全离线验收。

## 1. 范围

| 段 | 交付 | 明确不交付 |
|---|---|---|
| I1：账本收口 | 已结束 run（`BLOCKED` 任意 code、`ACCEPTED`）留下的 OPEN 条目（`reserved`/`requested`/`started`），在 owner 可证明已退出后由 `hflow resume` 关闭为 `unknown`/`launch_unknown`，或按已记录的确认停止事实关闭；之后可用 `hflow ledger settle` | 不重新派发、不改 run 的 task state / block code / outcome、不退款 |
| I2：受控集成 | `hflow integrate prepare/apply/reconcile`：把一个 `ACCEPTED/LOCAL_CANDIDATE` 候选集成到一个本地分支；集成树在独立 worktree 上重跑已批准检查；目标 ref 以 compare-and-set 更新；崩溃后可对账 | push / PR / 发布；冲突自动修复 Agent；写用户的工作树或 index；多候选批量集成；Team |

## 2. I2 集成契约

### 2.1 操作

`git` 层面只用不碰工作树的原语：

1. 目标 tip `T` = `refs/heads/<target>` 当前值；候选 `C` = receipt 的 `candidate.git_commit`；任务原始 base `B` = receipt 的 `candidate.base_commit`。
2. 前提：`B` 是 `T` 的祖先（`git merge-base --is-ancestor B T`），否则拒绝（目标被改写过，不猜测语义）。`C` 已是 `T` 的祖先时拒绝（没有可集成的内容）。
3. `T == B`：`squash`，`M = git commit-tree <C 的树> -p T`：目标历史里只多一个提交，修复轮次中被拒的中间候选不会进入目标分支（调研结论：候选提交按轮次串联，直接快进会把它们全带进去）。
4. `T != B`：`replayed`，`git merge-tree --write-tree -z --name-only --merge-base=B T C`（等价于把 `B→C` 的累计改动三方合并到 `T` 上）。
   * 退出码 0：干净；退出码 1：冲突，记录冲突路径，状态 `conflict`，不建提交；其他退出码：Git 错误。
   * 干净且结果树等于 `T` 的树：拒绝（改动已在目标中）。
   * 否则 `M = git commit-tree <tree> -p T`，作者/提交者为 HFlow 的固定身份，消息由控制器生成（只含已校验的 id，不含模型文本）。
5. 范围复核：`git diff --name-only T M` 必须是 receipt `candidate_paths` 的子集，并通过任务 scope 与项目 `write_deny`（同 `_accept`）。
6. `M` 由 HFlow 自有 ref `refs/hflow/integrations/<run_id>/<integration_id>` 保持可达（create-if-absent）。
7. 在独立的 detached worktree（`<repo>.hflow-worktrees/<integration_id>`）检出 `M`，以 phase `integration-check` 重跑该 run 的全部 required checks（`force_refresh`，删除工作树里非提交的 bytecode），evidence 的 `kind = "integration-check"`、`attempt_id` = receipt 的 attempt、`candidate_fingerprint` = 集成工作树按任务 scope 计算的内容指纹、`checks_digest` = run 的 `checks_digest`。检查前后指纹必须一致。
8. 检查后以不带 `--force` 的 `git worktree remove` 移除集成工作树；被拒（检查留下未跟踪文件等）则保留并记录 `worktree_state = LEFT` 与路径，不影响集成结论。
9. 全部通过 → `ready`；任一失败/错误 → `checks_failed`。

Git 最低版本：`merge-tree --write-tree` 需要 2.38，`--merge-base` 需要 2.40。`integrate` 在 Git < 2.40 时拒绝。

### 2.2 授权与写入目标 ref

`hflow integrate apply <integration_id> --expect-target <T>` 是操作者的显式授权：`--expect-target` 必须逐字等于记录的 `T`（完整 SHA），证明操作者确认的是这个 tip。

1. 状态必须是 `ready`（`integrated` 幂等返回；`applying` 先做 2.3 的对账）。
2. 复核：run 仍 `ACCEPTED`；集成 ref 仍指向 `M`；evidence 仍是 passed 且 fingerprint / checks digest 一致。
3. 读取目标当前值：
   * 等于 `T`：继续；
   * 包含 `M`（`M` 是其祖先）：记为 `integrated`，basis `operator_merge_observed`；
   * 其他（含分支被删）：`stale`（终态，需重新 prepare）。
4. **目标分支被任何 worktree（含用户主 checkout）检出时不写 ref**（只改 ref 会让那个工作树的 index/工作区与 HEAD 错位）。状态保持 `ready`，输出交接说明：`git -C <checkout> merge --ff-only <M>`，之后 `hflow integrate reconcile <id>` 观察到目标包含 `M` 即记为 `integrated`（basis `operator_merge_observed`）。集成命令打印的每条后续 `hflow` 命令（`apply`、`reconcile`），在所用账本不是默认账本（`HFLOW_DATA_DIR`，否则平台目录）时都带 `--data-dir <绝对路径>`，路径按 Windows 上的 PowerShell（7 或 Windows PowerShell 5.1）、其他平台上的 `sh` 加引号，原样粘贴到这两种 shell 即可执行（不适用于 cmd.exe：它在双引号内仍展开 `%VAR%`，也不去掉 PowerShell 的单引号；根目录打印为 `C:\.`，使右引号前没有反斜杠）；否则命令会打开默认账本并以 `4`（未知 id）退出。
5. 意图先行：CAS `ready → applying`（记录 `apply_intent_at`、`applied_by` = OS 用户、本进程身份），事务提交后才写 ref。
6. `git update-ref -m <reason> refs/heads/<target> M T`（Git 的原子 compare-and-set）。
7. 成功 → CAS `applying → integrated`，basis `hflow_ref_update`，同一事务写 `IntegrationReceipt` 与 run note。失败 → 重读目标：等于 `M` → `integrated`；等于 `T` → 回到 `ready`（记录错误）；包含 `M` → `integrated`；其他 → `stale`。

原 run 的 `ResultReceipt`（`LOCAL_CANDIDATE`）**不改写**。集成事实只在 `integrations` 表与独立的 `IntegrationReceipt` 中；`status`/`report` 并列展示。

### 2.3 对账（崩溃恢复）

`hflow integrate reconcile <integration_id>`，只读 Git、只写本记录，不调模型、不重跑检查：

| 记录状态 | 前提 | 结果 |
|---|---|---|
| `applying` | 记录的进程身份 probe 为 `gone` | 目标包含 `M` → `integrated`（basis `observed_after_interruption`）；等于 `T` → `ready`；其他 → `stale` |
| `preparing` / `checking` | 同上 | `interrupted`（终态）；集成工作树仍在则尝试不带 force 移除，失败则 `LEFT` |
| `ready` | — | 目标包含 `M` → `integrated`（`operator_merge_observed`）；目标已离开 `T` 且不含 `M` → `stale`；否则不变 |
| 终态 | — | 不变，只报告 |

进程 probe 为 `matching` 时一律拒绝。为 `unknown` 时也拒绝，什么都不写；这种情况见于另一台主机、没有记录创建时间、或无权打开该 pid，此时操作者可以用 `--owner-gone --attest` 声明进程已退出。这份声明按声明记录，不当作观察结果。

`prepare` 另有两条前置规则。第一，`--target` 必须与分支名逐字一致，包括大小写，否则拒绝。原因是大小写不敏感的文件系统会让 `MAIN` 解析到 `main`，之后的"是否被检出"判断就会漏掉真正的分支。第二，同一 run 已经集成进同一分支时拒绝：已有 `integrated` 记录，或分支已包含该 run 的某个集成提交，都算在内。如果同 run 的 `ready` 记录的提交已经被分支包含（交接后手工合并），先把它记为 `integrated`，不会把它标为 `superseded`。

### 2.4 状态

`preparing → checking → ready → applying → integrated`；终态另有 `conflict`、`checks_failed`、`stale`、`interrupted`、`superseded`、`failed`。
同一 run 同时至多一个活动记录（`preparing`/`checking`/`applying`，部分唯一索引）。新的 prepare 把同 run 的 `ready` 记录标为 `superseded`。
同一仓库同一目标 ref 同时至多一个 `applying`。

### 2.5 存储 v8

新表 `integrations`（列见 `src/hflow/migrate.py`），只增不改旧表；迁移沿用"备份 → 单事务 → 版本号"。

### 2.6 退出码

子命令完成了所要求的事时为 0：`prepare` 得到 `ready`，`apply` 得到 `integrated`，`reconcile` 得到 `ready` 或 `integrated`。需要操作者决定时为 3：`conflict`、`checks_failed`、`stale`、`interrupted`、`superseded`、`failed`，目标被检出时的交接，以及 ref 更新失败后回到 `ready`。另一进程仍在处理、或记录的进程可能仍在运行时为 5。拒绝且未写入时为 2，Git 错误也算在内。未知 id 为 4，存储的记录无法校验为 6。

## 3. I1 账本收口

`hflow resume <run>` 对已结束 run（`ACCEPTED`，或 `BLOCKED` 且 block code 不是 `outcome_unknown`/`owner_lost`）：

1. 若没有 OPEN 条目：维持现有 no-op。
2. owner 必须可证明已退出（与 `operator_settle_refusal` 相同的判定；pre-v6 且记录了 controller pid 的 run 拒绝并说明）。否则什么也不写，并说明原因。
3. 对 run 已记录的确认停止（cancel receipt `confirmed_stopped`，invocation 与条目一致）：按停止事实关闭该条目（与 `_settle_cancelled_invocation` 相同）。
4. 其余 OPEN 条目：`started` 且有 `started_at` → `unknown`；其他 → `launch_unknown`（`mark_unsettled_invocations_unknown`）。写 run note，说明下一步是 `hflow ledger settle`。
5. 不改 task state、block code、outcome，不派发，不退款。原来说"本构建没有命令能关闭它"的 note 与 README 条目同步改写。

## 4. 验收

全部离线：真实临时 Git 仓库 + 本地替身进程。必须覆盖：fast-forward、replayed、冲突、检查失败、范围越界、目标移动（prepare 后、apply 前）、目标被检出时交接与事后 reconcile、`applying` 中断后三种目标状态的对账、`checking` 中断、幂等 apply、两个 run 依次集成同一目标（第二个先 stale 再重新 prepare 成功）、迁移 v7→v8；以及 I1 的三类 OPEN 条目（驱动抛错无 spawn 事实、reviewer 驱动抛错、确认停止后账本写失败）经 `resume` 收口再 `ledger settle`。

之后的真实小任务验证（实现 → 冻结 → 检查 → 审查 → 接受 → 集成）需要用户另行批准的 live 额度。
