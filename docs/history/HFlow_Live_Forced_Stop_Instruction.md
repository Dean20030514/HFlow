# HFlow：一次真实运行中强停验证

**基线：** 实施者报告提交 `8446fa5`，替代 `3bbaaa9`；离线结果 `74 passed, 1 skipped`。  
**执行链路：** HFlow → acpx 0.17.1 one-shot exec → 官方 DSH ACP（本机 0.1.5-rc.1）。  
**任务：** 只验证已实现 Driver 对真实受管工具进程的强停，不重做 M0、不重新设计 Driver。  
**证据边界：** 本文件作者未读取该本地提交或复跑其测试。上列实现与结果来自实施者回报。

## 1. 授权门：本文件不是新增 live 授权

默认新增 live 额度仍为 0。只有用户明确给出本次授权后，才允许执行下述真实任务。收到文件、旧的凭据使用许可、前轮 M0 配额或新 experiment ID，都不能代替该授权。

供用户明确发送的授权文本：

> 批准基于 8446fa5 新增最多 1 次 live 顶层任务，仅通过现有 acpx-dsh Driver 验证真实运行中的本地强停。失败、超时、未知结果或未观察到有效停止窗口都不返还额度、不重试。不调用 Codex、Reviewer、Planner 或子代理；不修改全局环境；不扩大既有凭据访问范围；不开放无人值守执行。

这不是精确的底层模型请求数、费用或服务商计费上限。一个 ACP prompt turn 可包含多次模型与工具交互。[S1]

无授权时不要调用真实 Harness，不新增付费“准备检查”；现有离线工作已经可以收口。

## 2. 保留本轮成果，不扩大范围

继续使用 `8446fa5` 的已验证参数边界、任务 stdin、Job Object、流读取和取消意图/CAS 路径。核对本地 HEAD 与相关代码；若基线有实际变动，记录实际 SHA/相关 diff，不自动 reset、amend 或丢弃用户修改。

复用已有探针、helper、预算门和证据目录。只在缺少本试验必需的观测时增加小型探针胶水；不修改生产 Driver 的行为以使探针通过。探针新增代码本地检查一次即可，输入未变时不再重复完整离线回归。

不开发第二个传输、持久 session、协议代理、后台队列、计费系统、通用进程监控平台或更多 Agent。`session/list/resume`、模型/effort 设置和优雅取消都不属于本次目标。

## 3. 能力名称及待证命题

按本机已选 one-shot exec 路径分别记录：

```text
protocol_cancel_for_selected_exec = unsupported（依据已完成的本机代码检查）
forced_local_stop_offline         = passed（引用原有离线证据）
forced_local_stop_live            = not_tested（本次待证）
unattended_execution             = disabled
```

不要求精确使用这些新字段；更新已有表格即可。不要把路径限定的 unsupported 写成“DSH ACP 协议本身没有取消能力”。ACP 本身定义了 session/cancel 和原 prompt 的 cancelled 结束语义。[S1]

本次待证命题仅为：**通过选定真实 Driver 启动的受管工具确实在运行时，调用同一控制器/Driver 的停止入口，可以在截止时间内确认该 invocation 的本地受管进程已经结束，并且不会再派发任务或被接受为业务成功。**

## 4. 试验前一次性准备：零模型任务

在已经批准的 `.probe/` 独立环境和一次性工作区内准备一个固定、已检查的 helper。复用已有 fixture，尽量不新增依赖。helper 不联网、不访问凭据、不扫描用户目录，只在自己的临时目录写少量 READY/心跳信息；运行时有自行超时，不能无限等待。

READY 至少关联本次随机 nonce 和 helper PID。监测器通过操作系统核对该进程身份和本 invocation 的具体 Job 归属；helper 自报不能单独作为归属证据。已有进程句柄或 PID + 创建身份均可使用。`IsProcessInJob` 传具体 Job handle；传 NULL 只证明属于某个 Job，不能证明属于本次 Job。[S2]

沿用已有、有界的超时和日志限制，并在派发前记录；缺少时本探针可采用以下默认值（是试验参数，不是系统保证）：

- 等待真实任务启动 helper 的上限 180 秒；
- helper 自身寿命上限 60 秒，从 helper 启动计算；
- 发出强停后确认回收的上限 10 秒；
- 操作系统已确认退出后，最多再观察 2 秒辅助心跳/输出。

不要更改已经更严格且适用的界限。截止时间使用单调时钟。观测窗口不足或 helper 已自然结束，应记 inconclusive，不现场延长试验和重发任务。

安全收尾必须覆盖所有失败分支。确认无人值守开关保持关闭，使用仅限本次、已授权的手动探针入口，不全局翻转生产开关。

凭据只使用此前已批准的引用和受控内存注入方式；不扩大读取对象、不打印、不写文件、不复制登录态。依赖原样锁定，不全局升级、不安装新插件。

## 5. 唯一真实任务与触发顺序

### 5.1 先记录授权与预留，后派发

复用既有预算门，在真实提交前记录授权依据和唯一 invocation。最多放行一次，不进行“先无凭据试一下”。启动失败或无法确认是否提交时，本次授权保守视为已用，不自动返还。

普通 pytest、doctor、status、report、cancel 和 reconcile 不得因此产生模型任务。

### 5.2 任务只要求前台运行固定 helper

将简短任务经已有 stdin 路径发送：要求 DSH 通过原生工具以前台方式运行预先准备的固定 helper；不要修改 helper、补写源码、生成长文、进行额外研究或启动其他 Agent。

命令参数只含明确授权的 helper 与受控参数。不要使用宽泛 approve-all 解决权限问题。权限不满足时收尾、报告；不改全局权限重试。

### 5.3 以真实进程证据触发停止，不只看工具状态

保留与本次 invocation/session/tool call 对应的工具事件。ACP `in_progress` 是 Agent 报告的工具状态，不是操作系统进程归属证明。[S3]

有效触发至少需要：helper 的本次 READY；操作系统确认 helper 当前存活且属于本次 Job；仍处于可区分自然结束的运行窗口。工具 in_progress 存在时一并关联；未及时收到该事件，但进程证据完整时，不为等待一个投影事件耗尽停止窗口，如实记录该事件未观察到。

不能由探针在外层代为启动 helper，再声称验证了 DSH 工具后代。不能只杀客户端；不能调用直接 ACP 测试客户端或手工 taskkill 取代 Driver 的停止入口。

### 5.4 使用已有停止流程并收集证据

通过已有控制器入口记录 cancel_intent，再调用同一 Driver 的 cancel_handle/等价路径。当前模式预期走 forced，不重复尝试已确认不可达的协议入口。

从现有 Job/进程句柄与流收尾获得：停止请求时间、机制、受管进程与 helper 的退出确认、结束时间和必要的尾部输出状态。Windows Job 作用于关联的进程；默认进程继承存在特定例外，因此要记录本次真实工具的关联事实，不把文档能力代替本机证据。[S4]

不能只把 TerminateJobObject/CloseHandle 的成功返回作为全部退出证据；沿用已有等待与核对。TerminateJobObject 的语义类似对关联进程逐一 TerminateProcess，而外部 TerminateProcess 是异步终止，需要等待进程确认结束。[S5][S6]

心跳停止只是辅助证据，不替代退出确认。反过来，不要求强停后一定存在完整 ACP final、end_turn 或 cancelled 终态；这些缺失不否定有 OS 证据的本地强停，也不能被补写成协议/业务成功。

若失败需要额外安全清理，只针对确定属于本次 invocation 的进程；记录清理方式。外层手工清理成功不算 Driver 停止成功。无法确认停止时保留 BLOCKED/unknown、不继续执行、不删除现场。

## 6. 三类结果与放行范围

| 结果 | 判据 | 动作 |
|---|---|---|
| PASS | 真实 helper 由 DSH 启动、停止前活跃且确认 Job 归属；现有 Driver 在期限内确认本地受管进程结束；无额外派发、无迟到 ACCEPTED | 仅把本机/该版本/该 profile/该受管工具范围的 live forced stop 标为通过 |
| INCONCLUSIVE | 未启动 helper、自然完成太快、必要观测不足、权限/网络/凭据失败等，没有足够依据证明运行中强停 | 保留停止能力未认证；本次额度不返还，不再试一次 |
| FAIL | 在有效窗口发起停止但受管进程仍运行，或确认逃出预期边界、预算越界、产生后续写入者/迟到接受等必要条件违反 | 安全收尾并保留阻塞；根据证据决定一个局部修复，不自动重开选型或授权新 live |

结果必须把以下概念分开：

```text
probe_result                     = PASS / INCONCLUSIVE / FAIL
local_stop                       = confirmed_stopped / still_running / unknown
mechanism                        = forced
protocol_cancel                  = unsupported_for_selected_exec
business_outcome                 = not_accepted / unknown（按原契约，不造成功）
remote_request_or_billing_stopped = unknown
unattended_execution             = disabled
```

不要求为此另建数据库或协议 schema。现有 detail/结果表足够。强停不是文件回滚，不证明云端终止，也不授予强只读/恶意逃逸防护。Job 的进程管理与安全权限是不同边界。[S4]

本次不是普通业务 ACCEPTED 路径，不应为了制造业务收口额外启动 Reviewer；也不改普通业务 TaskSpec 的审查规则。

## 7. 收口与下一步

仅向既有 ADR/结果表增加一个经过脱敏的能力结论，必要时修改 operations 说明。原始日志、真实配置、会话与运行数据仍在忽略目录；不为材料整理增加模型调用。

回报只需：实际测试 SHA/相关版本，授权来源，attempted/admitted/blocked 与新增模型顶层提交数，helper 运行与归属证据，停止耗时和确认方式，最终能力等级，以及必要的剩余 unknown。

新增探针代码或版本化能力摘要可作一个本地提交，例如：

```text
test(driver): record bounded live forced-stop evidence
```

提交标题不能把未通过的实测描述为通过；不自动 push、release、amend 既有提交或升级依赖。

PASS 后停止本阶段的传输探索。下一项是原方案 M2：独立工作区/冻结候选/程序验证的一次真实小变更闭环，而不是 Team、第二 Harness 或无限停止压力测试。后续业务 live 额度另行明确，不从本次探针继承。本次无论结果如何，都不自动设置 unattended_execution=true。

## 来源与既有约束

[S1] ACP Prompt Turn：prompt 可包含多次模型/工具交互，协议取消的独立语义。  
https://agentclientprotocol.com/protocol/v1/prompt-turn

[S2] Microsoft IsProcessInJob：核对具体 Job；NULL 只测试是否属于任何 Job。  
https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob

[S3] ACP Tool Calls：工具状态由 Agent 报告。  
https://agentclientprotocol.com/protocol/v1/tool-calls

[S4] Microsoft Job Objects：关联/继承边界、KILL_ON_JOB_CLOSE、安全边界。  
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects

[S5] Microsoft TerminateJobObject：终止关联进程的语义。  
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject

[S6] Microsoft TerminateProcess：异步终止与等待要求。  
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess

既有文件：`HFlow_Driver_Next_Instruction.md` 第 9 节（新 live 必须用户明确批准）；总方案 M2/M3（真实小变更与日常可用性的后续验收）。外部文档用于界定契约，不代替本机实测。
