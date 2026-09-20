# 给 DSH 的下一轮指令：保存离线基线，完成有边界的 M0 传输探针

## 任务边界

工作目录是 `C:\Users\16097\Desktop\HFlow`。旧项目 `Dean20030514/dual-agent-workflow` 不在本任务范围内。

上一轮报告称已完成离线控制器纵切面，测试为 `49 passed, 1 skipped`。这些是上一轮实施者提供的结果，不是本文件作者复跑或审计源代码的结论。当前不扩展控制器，不实现团队、子代理、修复循环、发布、完整 Git 集成或第二个生产 Driver。

这次只做两件事：保存可追溯的离线基线；对首选 acpx → 官方 DSH ACP 做一次有退出条件的可行性试验。现有 `selected.py` 可以继续拒绝生产调用，探针成功不等于生产 Driver 已完成。

## 1. 保存离线基线

先确认当前 Git 根目录确实是 HFlow，检查已暂存的文件与 diff；排除凭据、真实配置、运行数据库、日志、依赖目录、临时产物。不要修改全局 Git 身份，不自动 push，不打生产 release tag。

首个本地提交信息：

```text
feat(core): add offline workflow vertical slice
```

提交前只核对以下两项，不据此展开整体重构：

- 报告中的 `reserved 2/4, invocations 1` 与“实现者、审查者各一次 invocation”的关系。若 `invocations` 仅统计实现者，就改名/分角色展示；若两次都实际执行，总数应为 2。若第二个 turn 只是预留未派发，必须如实区分。Fake Driver 的真实模型调用数仍为 0。复用现有记录，不另造计费系统。
- 同 TaskSpec 再次提交返回的是同一历史 run，不代表当前工作区重新验证通过。保留该幂等行为，但报告须避免把历史 ACCEPTED 误写成对当前文件的有效验收。当前范围内容指纹不能被当作 Git tree 或全仓验证缓存键。

如只是文案或计数口径不清，做最小修改。仅对实际变化重跑有关测试；全部修改收口后可跑一次离线回归。不为相同输入反复跑测试生成材料，不为清除 junction skip 修改系统权限。

## 2. 正确记录 M0 状态

上一轮首次指令本就禁止 live 模型试验，所以未执行 M0 不算偏离。

将 ADR 的事实与决策分开：

```text
preferred_transport = acpx-dsh-acp
production_transport = unselected
live_interop = not_run
observed_prerequisites = acpx not installed; acp profile directory not observed
```

以上只是建议表述，不要求新增这些程序字段。不能据此写成“DSH 不支持 ACP”或“ACP 方案已经失败”。

## 3. 仅在局部、可回滚环境准备依赖

本次允许为探针增加项目局部或专用临时目录中的 acpx 依赖，固定一个明确的发布版本并保存锁文件/完整性信息；不得 `npm install -g`、修改全局 PATH、升级现有 DSH、修改其插件组合或构建整个 DSH 源码仓。

选择版本只做必要的发布包/Node 兼容性检查，不展开多版本淘汰赛。npm 缓存与安装日志应使用/记录明确的位置；“没有全局安装”不等于没有任何缓存写入。

官方 DSH 仓库快照说明：内置 `acp` profile 可首次使用时初始化；DSH home 可由非空 `DSH_HOME` 指定。但本机报告的是 `0.1.5-rc.1`，必须先查本机安装包与 help，不能把远端快照等同于本机能力。

先检查本机实际 launcher 是否提供官方 acp 模板、启动方式和独立 home。若支持，使用仅传给探针子进程的独立、非空、绝对路径 `DSH_HOME`；profile、session 和缓存放入该探针目录。不要修改当前 DSH 对话进程的环境，不使用 `setx`，不触碰日常 `~/.dsh` 配置。

不得自行拼装大型 ACP profile。若安装包不包含必要官方组成，记录版本和具体缺失，停止 ACP 路线定位。不要把源码仓的 `pnpm dsh --profile acp` 直接当作 HFlow 仓内可用命令。

acpx 的进程、配置、缓存写入位置也应核对并限制到探针范围；不要凭空发明 `ACPX_HOME` 等未验证开关。

所有 acpx 调用显式选择 `hflow-mock` 或 `hflow-dsh` 这类自定义 Agent，不运行未指定 Agent 的顶层 `acpx exec`，以免进入默认 Codex 路由。

Windows 命令构造以选定发布包的实际契约为准。当前不同快照文档出现 `command/args` 与结构化 `argv` 差异；优先使用已支持的结构化参数边界，不照抄 Unix shell 字符串。路径含空格和中文属于探针输入，不是靠反复改引号修通的支线。

## 4. 探针分层，不能互相冒充

### A. 本地预检查：无模型任务

记录本机可执行文件版本/位置、选定 acpx 版本、官方 profile 组成、临时目录和实际命令的参数结构。只记录凭据是否就绪，不显示值。

可进行真实 DSH ACP 的启动、初始化、会话建立/关闭等非 prompt 检查，但必须确认所用最小 profile 不会因此自动开展模型任务；无法确认则不称其为“已验证零模型调用”。网络元数据请求也不要混称为付费推理。

### B. acpx → mock ACP：真实模型调用为 0

优先复用上游已有 fixture 或当前测试设施。只补本探针必要的最小 mock，不实现通用 ACP 服务器。

验证参数与工作目录、输入输出 framing、正常终止、确定性错误、超时/取消信号、受管测试进程退出。这些结果证明客户端及探针处理相应情况，不证明真实 DSH 具有相同语义。

mock 服务替换了 DSH，就必须写成 `acpx → mock ACP`，不能写成 `acpx → DSH ACP 互操作通过`。如果复用了真实 DSH 并仅替换 provider，则另行准确标注，不与前者混淆。

### C. acpx → 真实 DSH ACP：显式 live 模式，总上限 2 次顶层任务提交

普通 pytest/doctor 仍默认零真实模型调用。只有显式启用独立 live 探针才运行这一步。本任务的 live 上限是两次向真实 DSH 发送的顶层任务，包括失败、超时和结果未知的提交；不因失败返还额度，不额外调用 Codex、Reviewer、Planner 或子代理。

首次任务只在一次性工作区读取无敏感信息的小 fixture，返回可由程序核对的 nonce/内容。核验真实会话标识、工作目录、输出和退出结果；不让它修改 HFlow 源代码。

第二次仅用于一个事先选定的剩余必要行为，例如受控取消，或独立新会话的再次执行。不要让第二次变成“顺便测试全部特性”。若取消发生在任务自然结束之后，只能记为未观测到运行中取消，不能报取消能力通过。

若两次不足以覆盖某项能力，保留 `not_tested`。顶层任务次数不等于 API 请求数或账单 Token；内部重试、用量不可观察时如实标为 unknown，不能宣称两次调用就是完整费用硬上限。

凭据仅使用已支持且已具备的受控注入方式；不自动复制整个 home、凭据文件、session 或登录态，不执行新的登录流程。无法在允许边界内配置凭据时，记为 `blocked_credentials` 并停在这里，不重试模型。

## 5. 退出与选择规则

对一个具体失败只进行一次有明确假设的局部定位。缺依赖可用一次局部安装解决；缺官方模板、必要协议不兼容或无法满足生命周期边界，不转为修 acpx/DSH 上游、升级插件、换模型或反复安装多个版本。

- 只有 mock 通过：保留 ACP 为候选，真实 DSH 互操作仍未验证。
- ACP 所选最小运行范围的必要行为均有实际证据：在 ADR 选择它，并列出未认证能力；下一任务才实现薄生产 Driver。
- 已证实必要契约不能在本机和本轮边界内满足：在 ADR 明确改选官方 DSH headless。当前没有第一套生产 Driver，作此选择不是“开发第二套”；本轮只作选择，不同时实现两条生产路径。
- 只是凭据、网络或权限暂缺：记录阻塞，不谎称协议不兼容。不要靠换传输掩盖相同的凭据缺口。

尚未证明超时/取消之后真实受管进程不再执行时，不批准无人值守生产执行；最多记录为基本往返成功。进程范围观察不等于对恶意逃逸后代的强沙箱证明。

## 6. 本轮只交付这些

保持输出精简：首个提交 SHA；一个可重复运行的探针入口及最少 fixture；一份简短结果表；对既有 `docs/adr/0001-transport.md` 的增量修改。依赖目录、含敏感信息的日志、真实配置和运行数据不入库。

能力表至少区分：

```text
能力 | 文档声明 | 本机观察 | mock 验证 | 真实 DSH 验证 | 结论/范围
```

同时说明选定/未选定传输、live 顶层提交次数、Codex 调用次数、已知写入目录、剩余阻塞和下一项最小任务。不要再生成一份完整架构方案，不把探针状态复制成第二套业务状态库。

探针相关变更建议单独提交：

```text
chore(m0): add bounded local transport probes
```

结束标准不是“全部能力变绿”，而是“已经以有限投入得到首版传输选择所需的证据，或准确停在一个可解释的阻塞点”。

## 核对来源

以下来源用于核对设计，不代表本机已验证：

- 原始 `DSH_Bootstrap_HFlow.md`：第一轮仅离线，禁止 live 调用。
- 原始总方案第 7 节：ACP 必要契约不满足时明确改选 headless，替代而非叠加。
- DSH launcher，已读取快照 `ddefc45fbc7f8e46dd73185e68295696d1297887`：
  https://github.com/deepseek-ai/deepseek-harness/blob/ddefc45fbc7f8e46dd73185e68295696d1297887/apps/cli/README.md
- DSH home resolution，同快照：
  https://github.com/deepseek-ai/deepseek-harness/blob/ddefc45fbc7f8e46dd73185e68295696d1297887/packages/util/home-paths/README.md
- acpx CLI，动态文档，执行前对照选定发布包：
  https://github.com/openclaw/acpx/blob/main/docs/CLI.md
- acpx custom agents，动态文档：
  https://github.com/openclaw/acpx/blob/main/docs/custom-agents.md
- npm 局部安装、精确版本和锁文件：
  https://docs.npmjs.com/cli/v11/commands/npm-install/
