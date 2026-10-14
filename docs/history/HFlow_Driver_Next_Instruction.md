# 给 DSH 的下一轮指令：实现唯一薄 Driver，验证停止边界

**项目：** `C:\Users\16097\Desktop\HFlow`  
**阶段：** M0 基本往返之后；受控 Driver 开发，不是生产认证  
**首选且已选传输：** `acpx 0.17.1 → official DSH ACP`，本机 DSH `0.1.5-rc.1`  
**本轮默认：** 离线开发与测试；新增 live 顶层任务额度为 **0**。原 M0 额度不得自动重置。

## 1. 起点与本轮结束条件

实施者报告已有两个本地提交：

```text
4eda98e feat(core): add offline workflow vertical slice
e1ff547 chore(m0): add bounded local transport probes
```

报告还包括 `51 passed, 1 skipped`、真实 DSH 读取 fixture/回显 nonce 成功，以及运行中协议取消未验证。上述是实施者回报，不是本指令作者读取 HFlow 源码或复跑测试的结论。开始时核对本地 Git 状态与相关文件即可，不重做整个 M0、不遍历旧仓库。

**本轮只把现有探针能力放进中立 Driver 契约，建立可测试的进程生命周期和停止行为。** 允许完成代码而保留 `live_cancel=not_tested`；不要求先耗费新的模型任务才能写代码。无人值守真实执行继续禁用。

不实现第二套 headless Driver、团队、原生子代理、修复 cycle、会话恢复平台、完整 Git 集成、发布、Web UI、计费平台或新一轮架构重写。不要求 `session/list/resume`、模型/effort 设置全部实测通过才能结束本轮。

## 2. 先澄清已有证据，禁止为澄清口径重跑模型

### 2.1 M0 调用计数

“3 次实际提交，其中第 3 次被计数器拒绝”需要以已有记录消除歧义：

```text
attempted_top_level_submissions = 3
admitted_top_level_submissions  = 2
blocked_before_dispatch        = 1
```

以上只在第 3 次确实于派发/发送前被拦截时成立。两个被放行的提交分别为无凭据失败与带凭据成功；失败不返还原 M0 的 live 名额。若第 3 次已经发送 `session/prompt` 才被拒绝，就是预算边界缺陷，不能靠改文字记成未发送。若日志不能证明是否发出，标为 unknown 并按已消耗处理，不重新试一次求证。

复用现有计数记录；这不是新增预算数据库或统计服务的理由。

### 2.2 保留已有两项修正

保留分角色 invocation 展示与历史 candidate drift 提示，不再次改造它们。仅把 `observed 1` 明确标为“实现者自报”，不把它显示为运行总数或可信账单。确定性程序记录的实际派发次数用于控制预算，模型自报不能归还预留。

Fake 路径可确定的真实模型调用数是 0；真实 Harness 无可靠计费数据时为 unknown。ACP `usage_update.used/size` 表达上下文使用情况，不能把 8476→8695 的差值当成账单 Token。[S1]

### 2.3 传输选择与放行等级分开

在已有 ADR/结果表中保留如下区分即可，不要求新配置体系：

```text
transport_selection: acpx-dsh-acp
basic_live_roundtrip: passed (reported evidence)
driver_implementation: in_progress
live_cooperative_cancel: not_tested
owned_process_stop: not_certified
unattended_execution: disabled
```

“没有阻止继续开发的阻塞”成立；“没有生产放行阻塞”不成立。不要为了未完成的取消认证立即重开选型；先验证所选执行方式真正可用的停止入口。

## 3. 单一实现、五个职责

沿用总方案的中立接口，不把 DSH 的字段写进核心状态机：

```python
probe(binding)          # 本地能力/配置检查，不提交模型任务
start(request)          # 启动已授权的一次 invocation，返回可观察 handle
observe(handle)         # 收取事件与终止结果；可实现为 start 返回的事件流
cancel(handle)          # 请求停止，并返回事实而非固定 True
reconcile(handle)       # 核对已有执行状态，禁止自行派发或重做任务
```

若现有契约用别名或事件流实现，不为凑这五个方法名重构；但不能让 `start()` 一直阻塞到结束、外部无从取消。

Driver 只做参数、进程、流、终止原因和中立事件映射。控制器继续独占准入、预算与 attempt、测试验收、审查策略、ResultReceipt 和 ACCEPTED 的生成。

复用 `tools/m0_probe/` 已验证的必要片段，不整包复制探针、mock、日志和临时路径到生产代码。`acp_probe_client.py` 是测试工具，不能悄悄变成绕过 acpx 的第二条生产通信链路。

## 4. Windows 启动与两层 stdin

### 4.1 参数边界

沿用本机已经验证的 `agents.<name>.argv` 配置；固定显式的 DSH/mock 名称。禁止裸 `acpx exec`，也不把 `--agent "命令串"` 当成 Windows 通用写法。[S2]

Microsoft 明确要求通过命令解释器启动批处理文件；使用已验证的 `.CMD` 包装方式可以，但包装命令仅由受信任的 launcher 路径和固定选项组成。[S3]

任务正文、nonce、源码、用户需求和凭据不拼进 `cmd.exe /c` 命令串。正文经选定 CLI 支持的 stdin 或受控任务文件传入；cwd 作为独立进程属性。不要把 `shell=True`、字符串拼接或 `list2cmdline` 当作任意 cmd 输入的安全保证。

把已支持的空格/中文路径固化成无模型测试。对未覆盖且有解释歧义的 launcher 路径应明确拒绝，不能靠多次改引号重试。无需开发万能 Windows shell 转义库。

### 4.2 不要把两根管道混为一谈

```text
HFlow ──任务输入──> acpx ──持续 ACP 协议连接──> DSH
```

HFlow→acpx 的一次性任务 stdin 可能需要 EOF 才算输入完成；是否关闭，按已选命令契约处理。acpx→DSH 的 ACP stdin 则由 acpx 管理，连接期间不能意外关闭。

不要为了“保持 DSH stdin 存活”而永久不关闭 HFlow 的任务输入，从而让客户端一直等待 prompt。也不要对长期协议连接调用一次会提前关 stdin 的快捷封装。

持续排空 stdout/stderr，设置合理的读取/输出边界和终止截止时间；保留必要的原始错误，不让日志增长或管道阻塞把进程挂死。正常完成也必须回收自有资源。

## 5. 先确认生产链路上的取消入口，不能用旁路替代

读取已选 acpx **0.17.1** 的实际代码/契约并做一次有边界的离线测试：所选启动模式如何让取消到达正在执行该任务的 DSH session？

本次外部核对发现：`cancelSessionPrompt` 尝试向运行中的 queue owner 转发；`exec` 则在临时 session 执行。这不能据此推出外部 `acpx cancel` 一定能取消任意 `exec`。[S2][S4]

该版本 `withInterrupt` 注册 SIGINT/SIGTERM/SIGHUP，没有在该函数注册 SIGBREAK；不能把 CTRL_BREAK 视为已验证的协议取消方法。[S5] 这不是对所有源码路径或本机实际信号行为的完整证明。

**要求验证的链路必须与 Driver 将使用的链路相同。** 用测试客户端直接连 DSH 发 cancel，可以证明 DSH 的对应能力，不能据此认证 HFlow→acpx→DSH 的取消能力。也不能向 acpx 的普通任务 stdin 写 JSON，假装它就是 ACP 输入端。

若当前一次性路径只能可靠强制停止，则明确报告 `cooperative_cancel=unsupported/not_tested`，保留受控开发状态。不能为获得“优雅取消”静默引入持久会话、自动重连、新模型 turn、后台常驻服务或另一套 ACP Driver。需要改变调用模式时，只提交一个有证据的局部决策，不在本轮展开平台重写。

## 6. 把协议取消、停止进程、任务结果分开

### 6.1 协议取消

ACP 的 `session/cancel` 是 notification，本身没有对应成功 RPC response。确认语义取消应看原始 `session/prompt` 最终返回 `stopReason=cancelled`，并核对本地执行确已停下；仅“发出 cancel”或收到取消命令的零退出码不够。[S1]

可在 cancel 之后、原 prompt 终止之前收到尾部事件；不要一律把它们误判为协议损坏。完成已先发生、取消后到的情况，也不能伪造成一次运行中取消成功。

### 6.2 受管进程停止

为一次 invocation 设置一个受控本地进程边界：acpx、DSH、其受管工具后代。协议取消不能完成时，在有界等待后按已授权策略强制停止该边界，并核对结果。

Windows Job Object 是应优先考虑的已有操作系统机制，而非自研全机进程扫描服务。它能按组管理进程，并可设置最后一个 Job handle 关闭时终止组内进程；是否选择该机制，要复用项目现有封装或维护中的绑定，记录一条净收益理由，不开发通用进程平台。[S6]

采用 Job 时，必须验证在目标开始运行/派生子进程前已纳入边界，Job handle 不被无意继承而阻止关闭，控制器异常退出也有相应行为。不能“先让进程跑起来，再尽力挂到 Job”却声称没有启动窗口。

不要 `taskkill /IM node.exe`，不要终止不属于本次 invocation 的 Node/DSH/编辑器进程。单个 PID 不足以在后续恢复时安全识别进程；保留可核对的创建身份或受管句柄。仅杀客户端不能证明 DSH 和工具后代已停止。

本轮边界针对可信本地程序及受管后代；Job 不是文件只读沙箱，也不证明恶意逃逸、远程服务副作用或上游计费已被消除。[S6]

### 6.3 结果返回与控制器状态

沿用原 CancellationReceipt 的三类事实，不额外创造庞大状态体系：

```text
confirmed_stopped
still_running
unknown
```

在现有 detail/reason 中区分 `cooperative` 与 `forced`。强停已确认可以记为“本地执行已停止”，不能记成“协议取消成功”或“业务结果已知”。

控制器先记录取消意图，再请求停止。`cancel()` 幂等，不重复扣模型预算、不额外发 prompt。任务终态与结果落库使用现有事务/CAS，同时核对取消意图：已受理的取消不能被迟到成功结果覆盖成 ACCEPTED；已完成接受后才来的取消不能改写历史。

若 still_running/unknown，则保留 BLOCKED 或现有等价阻塞状态、保留工作区与证据，不清理现场、不再派新写入者、不自动重跑。取消确认不是回滚；已有改动仍需隔离检查。

## 7. reconcile 必须是保守查询，不是隐藏重试

只读取已保存的 invocation/session 标识、已有输出、进程身份与终止证据；不得调用会自动恢复/建立 session 的路径，也不得为了“确认状态”发新 prompt。

至少区分：仍在运行；有可靠终止结果；已经停止但结果不明；进程/结果状态均不明。进程消失不能推导任务成功，文件指纹匹配也不能补造缺失的正式验收证据。

只有完整 Driver 结果进入控制器后，才沿用现有验证和审查策略。`rc=0`、`end_turn`、nonce 正确均不是 ACCEPTED 的充分条件；已要求审查的 TaskSpec 不为节省本轮 live 次数而偷偷关闭审查。

## 8. 无模型验收：复用现有设施，覆盖六组边界

| 测试组 | 需要证明 |
|---|---|
| 正常单次执行 | 显式 mock Agent、参数/工作目录正确、输入结束正确、事件与进程终止可观察；同一 TaskSpec 经现有控制器契约运行 |
| 预算与重复提交 | 耗尽时不向真实路径发 prompt；重启进程不重置 ledger；重复 start 不生成第二次工作；实现/审查分别计数 |
| 协议与输出故障 | 缺/冲突终态、畸形输出、输出超限、早期断线、超时都不能伪造成功；不因此重做任务 |
| 取消竞态 | 未开始/执行中/已完成时取消；重复取消；迟到结果；正确原 prompt 关联；无额外模型提交 |
| 进程清理 | mock 忽略协作取消时能强停受管后代；控制器异常退出；无关控制进程不受影响；无法确认时阻塞 |
| 对账查询 | 正常/缺失/迟到证据的 reconcile；status/report/probe/cancel/reconcile 不启动模型任务 |

mock 使用同一生产启动与停止路径，最多增加一个无敏感信息的受管测试子进程，用于 READY/退出确认和必要的本地心跳。它不是新 Agent，不需要模型。

不把每个组合都做成付费测试。不为消除环境限制下的合理 skip 修改系统权限；未覆盖的强保证不得放行。修改完成后运行一次必要的离线回归，不对相同输入重复验证以生成材料。

## 9. 新 live 取消验证：当前默认不执行

原 M0 的两次授权已用完；第三次本地拒绝不产生新额度。创建新探针文件、目录、进程或 experiment ID 不能绕过额度。

**没有用户新的明确 live 授权时，完成上述离线开发与测试即收口。** 代码交付和真实取消认证可以分开。新增真实任务的许可必须来自用户，不能把本指令、旧凭据使用许可或传输选择决定当成新增额度。

如后续用户明确批准，建议独立安排至多 **1 次** 顶层任务，仅用于所选 Driver 的真实运行中停止验证，不重复已通过的读取/nonce 基本往返：

- 先让无模型测试确认实际取消/强停路径可达，再使用真实模型。普通 pytest 不运行 live。
- 工作区为一次性测试目录；任务最多启动一个固定、有自行超时的无害测试 helper。观察到 helper READY/工具 in_progress 后才请求停止；不要要求模型“多想一会儿”或生成长文制造延时。
- 必须走本轮 Driver 的实际执行与停止链路；分别记录协议终止、本地工具停止、受管进程回收和剩余 unknown。
- 若任务自然完成太快、未启动 helper、权限拒绝、超时或结果未知，该次仍消耗授权；标记 inconclusive，不自动第二次尝试。
- 有可用且已获准的凭据引用才使用；只在必要子进程内注入，不打印、不写日志、不写 git、不复制登录态，也不扩张旧授权的读取范围。
- 不调用 Codex、Reviewer、Planner 或原生子代理。该单次探针不是完整业务任务的 ACCEPTED/生产验收。

账单请求和成本不可观察时继续 unknown；结束本地进程不能承诺云端已经停止计费。[S1][S6]

## 10. 交付和停止规则

最少交付：选定 Driver 的实现及必要局部 helper、上述契约测试、已有 ADR/操作说明的增量。保留 `.probe/` 忽略规则；不把运行时配置、真实日志、凭据或含敏感信息的 probe JSON 当作测试 fixture 提交。

原 `tools/m0_probe/results/...json` 若确实要入库，只保留经过检查的无敏感能力摘要；不复制一份原始运行日志以制造更多材料。

建议提交信息：

```text
feat(driver): add bounded acpx-dsh execution lifecycle
```

如只完成生命周期/取消探针而未完成 Driver，则使用准确的 `test(driver)` 或 `chore(driver)` 信息，不虚报交付。

结束回报只需：提交 SHA、实际离线测试结果、新增 live 次数与授权来源、取消/强停/无人值守能力等级、一个剩余最小缺口。不得把本地开发阶段的“可以继续编码”描述为生产无阻塞。

对于一个失败只允许一次有明确假设的局部定位。发现需要修 acpx/DSH 上游、增加常驻架构或扩大环境权限时停止该分支，保留证据；不升级全局工具、不生成第二份总设计、不要求所有能力变绿。

**成功标准：同一个 Driver 能受预算控制地启动、观察并收口一次执行；不能确认停止时会明确阻塞，而不是继续烧额度或谎报完成。**

## 来源与证据边界

用户本轮回报提供本地版本、提交、测试及探针结果。本文件未审计这些提交；以下外部来源用于界定协议和操作系统契约，不替代 Windows 本机测试。

- [S1] ACP Prompt Turn：usage_update、prompt completion、cancel notification 和原 prompt 的 cancelled 响应：
  https://agentclientprotocol.com/protocol/v1/prompt-turn
- [S2] acpx v0.17.1 CLI：exec 临时 session、显式 Agent 和默认路由：
  https://github.com/openclaw/acpx/blob/v0.17.1/docs/CLI.md
- [S3] Microsoft CreateProcessW：批处理通过 cmd.exe /c 启动：
  https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
- [S4] acpx v0.17.1：cancelSessionPrompt → tryCancelOnRunningOwner：
  https://github.com/openclaw/acpx/blob/v0.17.1/src/session/execution/session-control.ts
- [S5] acpx v0.17.1：withInterrupt 信号注册：
  https://github.com/openclaw/acpx/blob/v0.17.1/src/async-control.ts
- [S6] Microsoft Job Objects：进程组管理、继承边界、KILL_ON_JOB_CLOSE 与安全边界：
  https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- 原总方案第 16.3 节：probe/start/observe/cancel/reconcile 与保守对账。
- 原 `HFlow_M0_Next_Instruction.md`：M0 两次 live 上限、失败不退额度、未认证停止时禁止无人值守。
