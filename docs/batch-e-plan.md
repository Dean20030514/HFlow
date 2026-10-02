# 批次 E：根预算与一次有界修复 — DSH 实施方案

日期：2026-09-25。代码基线：`8d68d9b`。状态：**E1、E2 均已实施并通过离线验收**。实现与验收证据见 `README.md` 的 root budget / repair 条目、`docs/operations.md` 与 `tests/test_batch_e_*.py`（E2 行为见 `tests/test_batch_e_repair.py` 与 `tests/test_batch_e_verify.py`）；本文保留原始范围与设计。E2 只实现"一次有界业务修复"：显式 `repair_policy` 才启用，且只由已声明的业务检查失败或实质性的 reviewer 拒绝触发；E3 及以后仍未实现。离线通过不构成 live 兼容性证明。2026-10-02 补注：§5.1 的根绑定要求与 §5.3 的修复前工作树复核如何落地，见各节注记；§9 为历史记录。

本轮用户授权是整理本机材料与推进下一批方案；不授权 live 派发。本文供后续 DSH 实施使用，不是 authorization，也不改变任何旧任务的“失败即停止”条款。新的 live 预算为 0。

## 1. 要解决什么

一个实现产出可用候选后，如果正式业务检查失败，或独立 reviewer 给出有效的修改意见，HFlow 可以在**事先明确批准**的总额度里修一次。无需重新研究需求、复制交接包，或由人手动拼接两轮记录。

必须先把派发记账统一：同一业务任务的 revision、run、实现与审查共享根额度，不能靠换 revision 或授权文件名获得一套重置的计数。

E 分两段，顺序固定：

| 段 | 可交付行为 | 明确不交付 |
|---|---|---|
| E1：根账本与原子派发 | root/run/authorization 额度、invocation 登记一次提交；跨 revision 保留根消费；prepare/report 解释预算 | 不触发修复，不增加模型派发 |
| E2：一次业务修复 | 首次正常失败后，原任务内至多增加一个实现 attempt，冻结新候选、重新检查及独立审查 | 不恢复历史 BLOCKED run，不重试环境/传输错误 |

先实施、验收 E1；通过后再实施 E2。两段均可完全离线验收，不要求购买模型调用来证明代码已接线。E1 通过但 E2 未做时，必须继续显示“自动修复未实现”。

这比旧总方案的“等待业务需求才开始开发 E”前进一步：当前明确推进设计，下一实施单元确定为 E1；并不把过去的 Unity 环境失败解释成需要模型修复的业务失败。

## 2. 已核对的代码起点

| 文件 / 接口 | 现状及本批要求 |
|---|---|
| `controller.py::_drive` | `_claim_submission` 与 `Store.dispatch_attempt` 分别提交；后者只把 run reservation 与 attempt 创建放在一起 |
| `controller.py::_review` | 授权扣数、review turn 预留、条件登记分开；须接入同一个派发事务 |
| `store.py::start_repair_cycle` | 有计数辅助接口，但 Controller 没有修复闭环；接口本身不足以守住停止态，不可直接套 while 循环 |
| `contracts.py::BudgetRequest` | `max_repair_cycles` 默认 1；这是历史数值，不是当前已启用修复的事实 |
| `prepare.py::_budget_plan` | 固定闭环预算，`repair_cycles=0`；不查询真实授权剩余额度 |
| `verify.py::verify_candidate` | 单项 evidence 有 failed/error 区分，但总结果都可能为 failed；不能仅按汇总状态决定修复 |
| `verify.py` 缓存 | 当前按 scoped fingerprint 与 checks digest 复用；修复后的正式检查本批强制重跑，不扩建缓存平台 |
| `gitworkspace.py::freeze_candidate` | base 来自当时 worktree HEAD；第二轮要另外保留任务原始 base 与上一候选，不能丢掉累计 diff |
| `authorization.py` | 已支持最多 4 次顶层提交，绑定实际配置；新字段需兼容旧摘要，不能使旧消费记录失效 |
| `store.py` 初始化 | 当前 schema bootstrap 不等于版本化迁移；新增表/列需最小显式迁移，不能直接重建历史库 |
| 取消协调 | 已有数据库条件写与 driver spawn 门；E 中的每个新派发仍须经过这两层 |

本批不声称新版 DSH live 兼容性已证明，不调查 Unity 遗留进程，不更改游戏仓库。

## 3. 范围冻结

沿用 `run / prepare / status / report / cancel / resume`。不新增 `repair` 命令：E2 是一次已授权 run 内的阶段，不是重开旧 run 的入口。`resume` 继续只协调，不能补发模型任务。

继续单 worker、零 native children、现有 acpx→DSH Driver、`LOCAL_CANDIDATE` 交付。Reviewer 每轮独立 invocation，仍如实记录 `prompt_only`。

本批不做：跨进程取消、第二 Harness、Team/DAG、根预算服务/跨机器同步、费用估算、动态充值、自动退款、自动换模型、协议格式修复、通用集成、UI、worker 磁盘硬上限、Unity 自动化修复、全输入缓存身份平台。

旧总方案提出的项目累计支出上限也不在 E 首版：保留既有 ProjectLimits 对单任务的准入上限，新增同根累计限额；这不能称为已实现“整个项目的消费总上限”。先覆盖一次修复必需的边界，避免扩成预算平台。

## 4. E1：最小账本设计

### 4.1 身份与授权

新增可选的根预算绑定契约（名称可按既有风格调整），由 contracts 定义，authorization 引用。至少包含：

- 根身份、project_id、规范化 repo 路径、task_id；同一任务的 revision 共用根身份。
- 绑定本地账本的规范化绝对路径；prepare 只计算路径，不为预览创建 SQLite。
- 根总顶层派发上限、根修复上限、总运行时限，以及根策略版本。
- 当前任务的 spec/config 绑定仍保留；根额度**不授权任意未来 revision**。

根身份可由固定的 `(project_id, canonical_repo_path, task_id)` 机械派生；数据库同时设唯一约束。禁止 worker 任意指定一个新 root id 覆盖这组映射。更换 task_id 是新任务，仍需新的明确批准；系统无法靠语义识别判断两个不同任务名其实是同一需求。

首个经批准的 E 任务登记根限额。后续同根 revision 只能在旧消费之上申请，不能重新初始化上限、使用量或修复次数；改变 scope/check/config/spec 仍须新的任务授权。E 不提供根充值或增加上限的入口。

授权 ID 重用时，不仅比较 binding，还要拒绝已登记额度、用户原话等不可变字段的变化；不覆盖旧授权行。不得由模型替用户填写批准文字。

旧授权、TaskSpec、receipt 保持可读和原摘要。根字段缺失的旧记录不推断归组、不清零消费；legacy 路径维持无修复。E 模式需新的完整绑定。新可选字段为空时必须按明确兼容规则省略于旧摘要计算，不能仅凭 Pydantic 默认值认为摘要不变。

账本路径绑定只能防止普通 `--data-dir` 换路径误用；复制、删库重建、改授权/改库仍属 trusted-local 边界，不宣称防恶意、跨库全局限额或抗回滚。既有旧 `.probe` 账本不合并、不迁移到默认目录。

### 4.2 数据增量

在现有 SQLite 内增加最少两类持久记录：

1. 根预算行：身份/绑定、不可变限额、已消费数、已用修复数、首次派发时间/截止时间、当前活动 run。
2. 每次顶层 invocation 的派发记录：唯一 invocation id、root/run/attempt、角色、authorization id、轮次、预留状态与可观察结果。

attempt/evidence/candidate 仍是原来的事实来源。invocation 表承担预算与派发关联，不再复制一套生命周期或完整事件流。旧 `attempts.invocation_id / review_invocation_id` 若保留作兼容视图，其写入必须与新记录同事务，不能成为独立事实源。

根允许最多一个活动 run；旧 invocation 未明确结束，尤其 `OUTCOME_UNKNOWN` 时，同根新 revision 也不得派发。根映射/占用/额度检查必须在数据库事务中决定，不能只在 CLI 先查。

首次实现消费不算修复；此后同根新增实现 attempt（包括换 revision）消费根修复次数。E 上限为 1，不通过“这是新 run 的第一次实现”重置修复数。后续 revision 只有单独批准且旧运行已明确结束才可进入；取消或未知不会自动得到继续资格。

### 4.3 唯一派发事务

用一个 Store API 同时服务 implementer/reviewer（例如 `reserve_dispatch`）。事务内：

```text
BEGIN IMMEDIATE
  校验 controller 所有权、当前 run/revision/attempt、角色与阶段
  校验 cancel intent 不存在、旧 invocation 没有未决状态、根活动 run 不冲突
  校验授权绑定及不可变额度，校验 root/run/authorization 的剩余量
  若是修复，实现 repair-count 的条件递增与 attempt 切换
  递增 root/run/authorization 计数
  插入 invocation intent（唯一 ID）并创建/关联 attempt、角色字段
COMMIT
事务外调用 Driver（仍传 stop_requested，仍经过 spawn 门）
```

全部成立才提交；任一步失败整笔回滚，不能留下“只扣授权、未记 attempt”的半笔账。不要嵌套调用会自行 `BEGIN` 的旧 Store 方法；抽出 transaction 内 helper。

相同 invocation id 的重复登记只返回已有事实，**不能让调用者再次启动 Driver**。API 必须区分“本次新预留，允许唯一启动者继续”与“已存在，只能协调”。新 invocation id 也不能绕过角色/阶段的唯一派发约束。

事务提交后到 spawn 前取消：沿用现有约定，已预留不退；记录 `not_started`/抑制启动事实，与真实启动数分开。提交前取消：事务拒绝，任何计数都不变。

崩溃时不尝试把数据库与远端请求做成原子事务。提交后结果不确定就保留消费、阻塞并协调；没有自动 TTL 退款、重试或 prompt 重放。E 不使用 `release_reservation` 作新派发的自动返还机制。

### 4.4 预览、报告与兼容迁移

prepare 继续零写入、不查询真实剩余额度。新增显示根绑定、配置上限、正常所需/最大可能派发数、修复开关、时限；是否还有额度由 run 的事务决定。

status/report 从已记录事实显示 root/run/authorization 的已用/上限、invocation 角色与启动状态、修复次数。明确 `reserved`、`started`、供应商模型请求不是同一个数量；未知 token/cost 仍为 null。旧 run 显示 legacy/not recorded，不能按新规则补造账目。

存储版本与公开 contract 版本分开。仅必要的标准库 SQL 迁移：检查版本→SQLite backup API 备份→事务迁移→重新读取旧记录。测试迁移中断回滚和重复打开幂等。未知未来版本在修改前拒绝。旧版程序缺少版本拒绝逻辑，不能假称已经能安全打开新库；操作说明必须禁止旧二进制写新库，回退时恢复迁移备份。

E1 验收时仍无自动修复；有预算字段不等于已执行功能。

## 5. E2：一次修复的行为

### 5.1 必须显式启用

新增可选 `repair_policy`（缺省 None/disabled），由 spec 与授权摘要覆盖。旧 `max_repair_cycles=1` **绝不能隐式启用**。启用时仅允许一轮、worktree 模式、必须独立 review、有效根绑定；`>1` 或不支持组合派发前拒绝。

策略列出允许触发修复的 check id 及该检查约定代表“业务断言失败”的非零退出码，以及是否允许有效 reviewer changes_requested。禁止把所有非零码默认为可修。它是项目/任务显式契约，HFlow 不宣称能从任意进程退出码识别根因。

> **2026-10-02 实施注记（根绑定要求的落地方式）。** E2 首版实际允许无根修复，与本节"有效根绑定"不一致（离线复核 F27）。2026-10-02 修订按以下方式落地：只要任一角色使用真实传输（非离线 fake driver），带 `repair_policy` 的任务必须同时传 `--root-budget-file`；`prepare` 在 `dispatch_preconditions` 下报告（`budget_exceeded`，location `root_budget`），`run` 在创建 run 行与授权记录之前拒绝。完全离线的 fake 运行仍可无根完成那一次有界修复，且不计入任何根修复计数。理由：真实修复的跨 revision 上限依赖根的修复计数与根时钟，无根时每个新 revision 都可再买一次修复；离线 fake 不触达模型、不需要批准，限制它只会让离线测试失去意义，而它仍受 `max_attempts=1`、`UNIQUE(run_id, task_revision, role, is_repair)` 与本 run 轮次上限约束。同时：已绑定根且启用策略时，若根的 `max_repairs` 不足以支付本次可能的修复（后续 revision 的首个实现也计为修复，故需 2），在登记根、创建 run 行与授权记录之前（因而也在 I1 之前）以 `budget_exhausted` 拒绝，改正根文件后重新提交即可被接受（`prepare` 只能报告 `max_repairs` 为 0 的情形）；无 `--root-budget-file` 的运行若同一任务已有根，在创建 run 行与授权记录之前以 `budget_exhausted` 拒绝，派发事务内再查一次作为竞态兜底。第 69 行"legacy 路径维持无修复"与第 7 节第 11 条"所有角色仍计入同一根账本"据此只对真实传输成立。

最坏闭环预算预检：有修复时需覆盖 4 次顶层派发；未启用修复仍为常规 1+1。根/run/授权均不足 4 时启用修复的初次 live run 在 implementer 前拒绝，避免半途才发现不能完成审查。项目 required-review 下限照旧。

| 路径 | 最大实际预留 |
|---|---:|
| 第一候选检查和审查均通过 | 2：I1 + R1 |
| 第一候选业务检查失败，修复后检查与审查通过 | 3：I1 + I2 + R2 |
| 第一候选检查通过、R1 要求修改，修复后通过 | 4：I1 + R1 + I2 + R2 |
| 修复检查仍失败 | 到此停止，不购买 R2 |

额度是上限，不强行花满。E1 的跨 revision 新授权另按该次已批准剩余闭环计算，不能用它绕过 E2 初次启用的 4 次完整预算门。

### 5.2 只有两种允许的触发

- 正常完成、进程边界干净且采集可信的业务检查失败：读当前 attempt 的结构化 outcome/evidence，所有失败检查均在批准策略内、退出码匹配。
- 当前候选上的有效 `changes_requested`：解析/输入绑定正确、有非空可用 findings。空 findings 的拒绝仍阻塞，不花钱猜修改目标。

混合了任一 ERROR/环境错误就停止。启动异常、超时、被强制清理、输出采集异常、候选漂移、越界、review 格式错误、Driver 错误、取消、未知结果都**不触发修复**。不得根据 `verification_failed` 字符串或日志关键词猜分类；将 `exit_reason` 等必需执行事实结构化持久化，旧 evidence 缺事实时不具备自动修复资格。

Unity 的两次历史失败都属于不允许触发的分支；旧失败授权、run 与证据全部保持原样。

### 5.3 attempt、候选与证据

同一运行保留原 TaskSpec/revision/config/检查定义及授权，增加新的 implementer attempt；不在内部悄悄改 revision。首轮失败记录保留，run 在决策时直接进入一次修复，**不先写 terminal BLOCKED 再复活**。旧 BLOCKED run 不参与此路径。

复用本 run 的隔离 worktree，以上一冻结候选为修复起点；重核 HEAD、完整 tree、scope 与工作树状态。dirty/漂移则拒绝，不 reset、不覆盖。所有既有工作区管理边界继续适用。

> **2026-10-02 实施注记（修复前的工作树复核）。** 复核在预留与派发之前进行（`_reconcile_repair_workspace`）：HEAD 与 tree 须仍是上一候选，`git status` 干净，且没有任何索引项带 assume-unchanged 或 skip-worktree 标志（`git ls-files -v`；带标志的项 status 看不出改动。冻结本身遇到这类标志也拒绝，HFlow 从不清除标志）。任一不满足即记 `workspace_drift`，以 `scope_violation` 阻塞，不购买修复。上一轮检查或审查留下的被忽略文件（首轮冻结已拒绝白名单外的被忽略路径，故只能是 HFlow 自己检查的副产物）分两类：在 scoped fingerprint 不哈希之处的，按字面路径带入修复轮冻结，从不暂存，worker 对它们的改动仍由 manifest 比对按越界拒绝；在 `write_allow` 目录内、fingerprint 会哈希的（例如 `write_allow: ["src"]` 下的 `src/run.log`），没有候选 commit 持有它，fingerprint 会与冻结的 commit 不一致，因此同样在购买前以 `workspace_drift` 拒绝，且不删除该文件。

保留三个独立身份：原始任务 base、上一候选 parent、新候选。最终 receipt 的候选路径与交付 diff 对应**原始 base→最终候选**的累计变化，不能只交第二轮补丁。每轮 candidate ref 保留；无内容变化时以 tree/内容身份判断，不能靠新 commit SHA 假装有进展。

修复后：冻结 C2 → 完整执行本任务所有正式检查（本批禁用跨 attempt 缓存复用）→ 通过才新派 reviewer。R2 只可接受 C2 的当前 evidence；C1 的 passed/verdict 不得用于接受 C2。

Controller 当前有按 run 取 evidence 的路径，实施时必须逐一改为当前 attempt/candidate 过滤（含 packet、acceptance、report）；历史失败可在历史区显示，不能混入新候选的判定。

无内容变化立即停，不购买新 reviewer。修复后仍出现同一失败可以作机械注记（候选 tree + check id/退出原因或规范化 finding）；不引入语义判重模型。无论是否“有所进步”，用完一次都停止。

### 5.4 修复输入与停止

沿用 implementer 角色及其既定 Driver，新增结构化 repair context：原目标/AC/范围、原始 base、上一候选 commit/tree/fingerprint/diff 引用、失败 check 的当前 evidence/artifact 或 reviewer findings、本轮剩余预算与期限。

不重发全部研究和历史对话，不把日志/审查建议当成修改权限来源。允许路径、检查定义、模型/权限配置不变。packet 仍为 32 KiB 上限，必需字段装不下就派发前拒绝，完整日志走可定位引用。

第二轮同样经过 E1 派发事务与 driver spawn 门；取消在渲染/预留/登记/spawn/验证/接受任何交接点都不得被修复覆盖。旧 attempt 的迟到成功、失败、review 结果只记录，不推进新 attempt。

根总时限从首次成功预留开始，不能每次修复/换 revision 重置。新 invocation/check 的超时取角色/检查上限与剩余时间的较小值；时间耗尽停止后续工作，活动模型沿既有取消边界终止。清理仍需有限结算宽限，故不是“远端账单或本地清理恰在截止点结束”的保证。重启不重新派发，持久截止时间用于拒绝未来继续。

## 6. 修改位置与实施顺序

| 顺序 | 文件 | 交付 |
|---|---|---|
| E1-1 | contracts / authorization / store | 根绑定、兼容摘要、最小迁移、根与 invocation 记录；旧授权消费不变 |
| E1-2 | controller / store | 两角色共同原子派发入口、停止条件、幂等与崩溃协调；移除生产路径分步扣数 |
| E1-3 | prepare / admission / report，必要的 CLI 参数传递 | 同一解析结果、零写预览、事实报告；不新增命令平台 |
| E2-1 | contracts / admission / verify | 显式策略、正常失败分类、结构化执行原因、预算/期限预检 |
| E2-2 | controller / packet / gitworkspace | 一次 attempt 循环、修复输入、候选累计差异、全新检查与审查 |
| E2-3 | 现有相邻测试与最多两个聚焦测试文件 | 集成反例和回归，文档按实际完成能力更新 |

不重写整个 Controller。可以把当前单次实现→验证→审查提取为返回明确阶段结果的内部函数，然后由至多两次迭代驱动；不可在宽泛 `except` 中递归 `_drive`。Driver 协议和 spawn 门原则上不变；新增“独立修复 Driver”没有必要。

## 7. 必须通过的离线验收

以外部可观察行为为断言，不能只 mock 最终布尔值或只钉私有方法名；并发用事件/栅栏，不能靠 sleep 猜顺序。

**E1：**

1. 两个独立 Store 连接竞争最后一份额度：至多一个派发，root/run/auth/invocation 计数同时一致；读者用单快照核验。
2. 在事务每个关键写后注入异常：全部回滚；相同 invocation 重放不再扣数、不再次 start。
3. 取消先提交：无新预留；预留先提交但 spawn 被抑制：保留消费，started=0；两角色都覆盖生产 Driver + 离线客户端路径。
4. 预留提交后控制器崩溃：重开 Store/resume 不生成新调用；unknown 不退款，同根另一 revision 不能开工。
5. 新 revision/新 run/新授权仍关联同根 used 与 repair_used；root 改限额、同 task 换 root、旧授权改 max 被拒；旧余额不重置。
6. 旧 synthetic authorization 摘要、旧 spec 摘要/查询、消费与 receipt 保持；迁移失败可回滚，迁移幂等，未知未来存储版本不被写入。
7. prepare 不创建 DB/run/授权，run 在真实剩余额度不足时拒绝；更换 E 授权绑定的 data-dir 被拒；status/report 不调用模型。

**E2：**

8. 历史 `max_repair_cycles=1` 但没有新 policy：仍只走旧闭环；新 policy 预算不足/范围不支持时在 I1 前拒绝。
9. I1 检查失败→I2→新检查→R2 接受：3 次；R1 有效拒绝→I2→新检查→R2 接受：4 次；首次成功：2 次。
10. ERROR/正常失败混合、未声明退出码、超时、残留后代、采集异常、格式错误、unknown、取消：不触发 I2。空 findings 亦停止。
11. 第二次失败或无内容变化：不发第三个 implementer；检查失败不买 reviewer；所有角色仍计入同一根账本。
12. 输入敏感离线 agent 从实际收到的 packet 断言上一候选与失败证据；删去 repair context 必须拒绝。长引用完整，超限前拒绝派发。
13. 第一轮只改 A，第二轮只改 B：最终候选从原始 base 的 diff 含 A+B；旧 ref 保留，旧 evidence/verdict 不能验收 C2；用户 dirty checkout 不变。
14. 首轮/修复的迟到结果、取消穿过各交接点、根到期与派发竞争：保持停止/未知，不接受、不重发、不增加额度；时钟可控。

优先扩展 `test_authorization.py`、`test_concurrency.py`、`test_cancel_routing.py`、`test_packet_wire.py` 和现有 prepare/CLI 测试；新增测试只为新行为。无需为旧业务包另建 verifier、manifest 或新交接目录。

每段实现稳定后跑相关测试，再按 AGENTS 跑一次 `python -m pytest -q`。失败则定位真实回归；通过后无代码变化不重复全量刷证据。报告实际输出及 skip，不预写测试总数，不以 fake/stub 结果宣称新版 DSH live 兼容。

## 8. 调研依据与取舍

2026-09-25 已先搜索 GitHub 并读取以下官方源码/资料。这些是设计参考，不是将外部项目当作 HFlow 的实现证明；链接的 main 会变化。

- [LangGraph 重试实现](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_retry.py)：参照将重试策略与执行结果分开处理的做法；HFlow 不照搬网络错误自动重试，未知提交必须停。
- [AutoGen 终止条件契约](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-agentchat/src/autogen_agentchat/base/_termination.py)：参考显式终止条件及组合方式；HFlow 的额度和停止事实继续由本地事务强制，不交给对话文本。
- [SQLite 事务说明](https://www.sqlite.org/lang_transaction.html)：采用现有 BEGIN IMMEDIATE，并按真实竞争处理事务失败。它只解决本地数据库一致性，不解决远端请求恰好执行一次。

选择是在现有标准库 SQLite、contracts、Controller 上增量实现。没有复用新框架的必要；本批的主要工作是接通现有事实与事务，不是重新造编排平台。

## 9. 交给 DSH 的下一条指令（历史记录，已失效）

> **历史记录。** 本节是 2026-09-25 写给实施方的原始指令，仅保留作来历说明，不再是待执行的指令：E1（`2edcaff`）与 E2（`0ec289c`）均已实施，2026-10-02 的修订另行修正了复核发现的缺陷。下文"不实现 E2"等约束只对当时的 E1 阶段有效。当前行为以 `README.md`、`docs/operations.md` 与测试为准。

> 按 `docs/batch-e-plan.md` 先实施 E1：根绑定、同库根预算、两角色统一原子派发及历史兼容。只做离线代码和测试，不实现 E2 自动修复，不派发真实模型，不改旧 handoffs/授权/游戏仓库，不提交或推送。重点证明多个计数与 invocation 登记同事务、取消不穿透、未知不重发、跨 revision 不重置根消费，以及旧摘要保持。复用现有测试设施，结束时报告改动、真实测试输出和限制。E1 完成后再进入已定义的 E2；不要在 E1 中提前声称修复循环已实现。

本方案的交付是一份可执行的开发范围。真实运行另需用户给出明确任务、配置和 live 预算；既有授权的未用余额不因本方案获得新用途。

本轮基线核验：未修改实现代码，`python -m pytest -q` 实际输出 `377 passed, 1 skipped in 216.41s (0:03:36)`，exit 0。该结果属于现有实现，不是 E1/E2 的验收结果。仅新增本方案；没有创建授权、live 派发、提交或推送。
