# 旧 workflow + HFlow 提炼，与 Claude Code 桌面版对照（2026-10-18）

范围：`C:\Users\16097\Desktop\workflow`（189 个提交，含 Team 模式）和 HFlow（54 个提交，批次 A–N）。
四路只读调研：旧 workflow 规则/配置、旧 workflow Team 模式、HFlow、Claude Code 官方文档。另有一手观察：本报告写于 Claude Code 桌面版的 Code 标签页会话中。

## 结论先行

1. 两个仓库共提炼出约 120 条，去重后约 30 个主题。**有价值的几乎全是“纪律”和“踩坑知识”，几乎没有“机制”。**
   - 机制部分（账本、状态机、收据、审批摘要、多轮审查协议）没有一项在真实使用中证明过净收益。
2. 对照 Claude Code 桌面版：
   - **约一半已经内置**：worktree 会话、Plan 模式、权限规则、独立代码审查、PR/CI 自动修复、定时任务、子代理与 Workflows、记忆。
   - **约四分之一有机制但要你配置**：hooks、deny/ask 规则、子代理只读。
   - **其余是纪律和知识，Claude Code 没有**，但只要写成二三十行 CLAUDE.md 和一个知识文件就能带走。
3. Claude Code 确实缺、你也确实在意的只有两项：
   - **单任务花费硬上限**：桌面版没有；`--max-budget-usd` 只在 CLI 的 `-p` 模式可用。
   - **跨模型审查**：原生没有；OpenAI 官方出了 Codex 插件可补，但它会消耗 Codex 额度，正是当年的痛点。

   两项都不值得为它们自建系统。

## 实际战绩（为什么说机制没证明价值）

- **旧 workflow 的纪律部分**：
  - 4 个真实项目的 Critical 任务全部 “stopped, NOT converged”。
  - 8 月起每个任务的证据产物膨胀了 5–70 倍，单项目月吞吐从约 100 降到 18。
  - SeedLink 一个 3 行改动：4 轮审查、8 份全部“不通过”的 verdict、产品缺陷为 0，耗时 2 天、11 次 Codex。
  - 335 条 blocking 中，用户能感知的只占 14%。
  - 99 次 Codex 运行累计 17.1 小时，单轮中位数 554 秒。
- **旧 Team 模式**：
  - 在 workflow 自身上真实运行 5 次，0 次完成，墙钟合计约 11 小时，费用从未计量（全部 `unknown_usage`）。
  - 收尾时唯一的真实小任务是一个 CSV 汇总脚本，耗时 7 分 37 秒。
- **HFlow**：
  - 规模：`src` 约 3.95 万行、`tests` 约 5.85 万行、9 轮上游调研。
  - 通过它交付的真实项目改动为 **0**。
  - 唯一一次端到端 live 运行在合成项目上，因适配层丢了 verdict 被阻塞。
  - 2026-09-20 以后再没有 live 运行。

## 一、对照总表

标记说明：
- ✅ 已内置，直接用；
- ⚙️ 有机制，需要配置；
- 📝 Claude Code 没有，写成纪律或知识就够；
- ❌ 没有，也不值得做。

ID 前缀：W = 旧 workflow 规则/配置，T = 旧 Team 模式，H = HFlow。

### A. 工作方式与纪律

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 1 | 默认轻流程，按风险升级（W1、H4、T2） | Critical 任务全部不收敛，证据膨胀 5–70 倍 | Plan 模式按需切换；默认行为本就是轻流程 | ✅ 加 📝 一行 |
| 2 | 高风险面先停：auth/密钥、资金、迁移、部署、公共 API（W2、T22） | 设计裁决 | 权限提示和 Auto 模式分类器只拦危险**动作**，不拦业务风险面 | 📝 |
| 3 | 系统级操作先问：UAC、注册表、代理、杀 VPN；一种方法失败两次就停（W26） | 一次 `winget upgrade` 演变成 143 次调用、4 次 UAC、杀 VPN、留下 3.3 GB 垃圾 | 部分（权限提示、Auto 分类器） | 📝 |
| 4 | 同一修复连续失败两次就停；卡住就换新上下文（W5、T12、T13） | 补丁越滚越多，停止条件失效 | 无内置规则；`/goal` 可加 “stop after N turns”，但由模型判断 | 📝 |
| 5 | 任务要小：单次生产 diff 超过约 4000 行就拆（W7、T1） | 4556 / 5738 / 2820 行的三个任务全部不收敛；一个 Worker 接 7 个子系统，失败 4 次 | 无 | 📝 |
| 6 | 不留暗债：“暂时/先这样”要当场修或当面说（W8） | 无具体事故 | 系统提示已要求如实报告 | 📝 一行 |
| 7 | 新增流程默认说“不”：先说清它防住了哪个真实失败（W3、W4、H1） | H5A 任务：3 处 15 分钟的漂移，被流程长成 81 个测试、4 道批准门，最终一处没修；两个仓库本身就是反例 | 不是工具问题 | 📝 个人原则 |
| 8 | 先找轮子，结论一句话（W35、T29、H5） | 严格的复用协议本身耗了约 3.7 小时；记忆里已有“先上 GitHub 调研” | context7 插件、`gh`、WebSearch 都已具备 | ✅ 加记忆 |
| 9 | 教训要落成 hook 或测试，不要写成文字（W34、W32） | 靠 agent 自觉调用的流程会被绕过：27 个 Critical 会话里 20 个一个阶段命令都没加载 | hooks 已具备 | ⚙️ 原则 |

### B. 验证与验收

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 10 | 不信自报、exit 0、`end_turn`；验收看 agent 改不到的检查（H6、H7、T7、T8、W9） | acpx 维护者确认“过载文本 + end_turn”时退出码为 0；DSH 把失败的 shell 调用报成成功 | 系统提示要求如实报告；可确定执行的门禁需自写 **Stop hook**（hooks 在桌面版运行）；`/goal` 的评估器不执行命令；CI 加分支保护 | ⚙️ 按项目可选 |
| 11 | 测试结果只对跑测时的代码有效，改了就重跑（H8、T15） | HFlow 的证据指纹机制 | 无 | 📝 一行（机制 ❌） |
| 12 | 负向对照：守护测试要先证明会变红、真被执行（W10、W11、W13、T5） | `"types": []` 号称能拦 Node 内置模块，7 个探针里 5 个本该被拦的全部通过；4 个空守护测试；turbo 缓存造成假通过；检查器带过期 skip-list | 无 | 📝 高价值 |
| 13 | 系统化调试：先复现、一次改一个变量、枚举所有生产者和消费者（W14） | “修好 A 又破坏 B”反复出现 | 模型基本会做；可做成 `/debug` skill | 📝 或可选 skill |
| 14 | 测试成本分层：任务内只跑相关测试，集成时全量一次（T15，你的记忆） | 全量 43 分钟；一次运行撞上 1800 秒硬上限 | 无 | 📝（记忆里已有） |
| 15 | 输出有界、截断不能当依据（H12、T27） | — | harness 自己截断工具输出 | ❌ |

### C. 审查

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 16 | 独立审查：新上下文，只给需求和 diff，不给作者自述（W19、T9、H11） | Codex memories 把旧经验带进了“全新”审查；盲审拿到了历史 verdict | `/code-review [low..max] [--fix]`、`/security-review`、`/code-review ultra`（云端，Pro/Max 免费 3 次，之后约 $5–25 一次）；子代理天然是新上下文 | ✅ |
| 17 | 跨模型审查（旧 workflow 用 Codex） | 同模型审查盲点相关 | 原生没有；OpenAI 官方插件 `openai/codex-plugin-cc` 提供 `/codex:review`，未经 Anthropic 文档背书，桌面版未验证，会耗 Codex 额度 | ⚙️ 可选，不推荐默认 |
| 18 | blocking 只收有具体后果的缺陷，证据缺口单列（W16、T10） | 335 条 blocking 里只有 14% 是用户能感知的 | `/code-review` 本来就要求写出具体失败场景 | ✅ |
| 19 | 最多 3 轮；某轮问题全由上一轮修复引起就停；单次 verdict 只是抽样，是否阻断由人定（W17、W18、T11） | 实测 7/6/5/3 轮全部不收敛；同一 diff 重放 10 次，归类按 6:4 分裂 | 无 | 📝 |
| 20 | 审查依据冻结在起点：diff 里改了规则、测试或 `.claude/` 配置，只算提案（T28、H20） | 防止 agent 改规则让自己过关；acpx `.acpxrc.json` 能替换启动参数 | worktree 会话的 settings/hooks/agents 从主检出读取，起部分作用；仓库里的 `.claude/settings.json`、`.mcp.json`、hooks 改动仍需人看 | 📝 一行 |
| 21 | 审查者轻量：不重装依赖、不重跑全量（W15） | Codex 自己 `git archive` + 重装依赖，一天烧光配额 | Claude Code 审查不这样做 | ❌（仅用 Codex 时适用） |

### D. 成本与配额

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 22 | 单任务花费硬上限，派发前预留（H13、T21） | 两个系统都没能计量真实费用 | **桌面版没有硬上限**：`--max-budget-usd` / `--max-turns` 只在 CLI `-p` 模式可用，Agent SDK 有 `maxBudgetUsd`（客户端估算）；桌面版有 `/usage` 估算和额度重置后自动继续 | ❌ 自建；看 `/usage` 和套餐上限 |
| 23 | 并行子代理数量上限，按批分组（W21） | 三次运行分别开了 97 / 69 / 59 个 agent，每个 25–30 万 token，实际只需 4–10 个 | Workflows 默认并发 16、上限 1000；`/config` 里有 “Dynamic workflow size”，本会话默认指引是 10 个以内 | ✅ 加 📝 一行 |
| 24 | 子代理不降模型（W22） | 偏好：便宜模型多花 2–3 倍轮次 | 子代理默认 inherit | ✅ |
| 25 | 计数诚实：未知写未知（H14） | — | `/usage` 明示是估算 | ❌ |

### E. 隔离与 git

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 26 | 每个任务一个 worktree，固定起点（T4、H24） | — | 新建会话可选 worktree（`.claude/worktrees/`，可设分支前缀）；子代理 `isolation: worktree`；与基线同步有内置工具（一手观察） | ✅ |
| 27 | 不写用户的 checkout：不 stash、不 reset、不提交别人的未提交工作（H24） | HFlow 规则 12 | worktree 隔离，加上系统提示里“破坏性操作先确认” | ✅ |
| 28 | 按显式路径暂存，不用 `git add -A`（W28、T5） | 脚本三次把自己的临时文件扫进了提交 | Claude Code 提交时一般逐文件加，但不强制；可用 PreToolUse hook 拦 | 📝 或 ⚙️ |
| 29 | 不 push；默认不 commit（W29） | — | 系统提示：只在用户要求时 commit/push | ✅；可加 ask 规则 |
| 30 | 清理只做可证明安全的；不用 `git worktree repair`（T30、H25、H28） | `repair` 会重写你其他 worktree 的 `.git` 文件；残留 9 个分支和 worktree | 桌面版归档会话即删 worktree；“PR 合并/关闭后自动归档” | ✅（知识留档） |
| 31 | 只合入已验收的提交，合入后在最终树上回归（T17、H27） | — | PR + CI + Auto-fix + Auto-merge（仅 squash，需仓库开启） | ✅ |

### F. 安全与护栏

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 32 | 只读要靠工具层保证，不能只靠 prompt（W23） | 没有沙箱的 DSH 审查者跑了 `install.ps1`，删掉你 `~/.claude` 下 127 个文件，并留下含凭据的备份；同型事故两次 | Plan 模式在工具层只读；子代理可设 `tools`/`disallowedTools`；`permissions.deny`；PreToolUse hook | ⚙️ |
| 33 | 秘密不进对话和日志（H21） | HFlow 审计发现 agent 子进程继承全部环境 | **原生 Windows 不沙箱**（官方文档原话）；你现在的 allow 列表放行了 `Bash(env)` 和 `Bash(printenv:*)`，会把密钥打进对话记录 | ⚙️ 建议从 allow 移除 |
| 34 | `Bash(find:*)` 同时放行了 `-delete` 和 `-exec`（W 调研发现） | — | 你的 allow 列表 | ⚙️ 建议收窄 |
| 35 | 不可信数据加围栏（H23） | — | harness 把工具结果当数据处理，有注入防护 | ✅ |
| 36 | 覆盖本机配置的部署脚本：只增不删、逐文件备份、默认 dry-run、只在临时 HOME 测（W24、W25） | 同 #32 的事故 | — | 📝 知识（只在写这类脚本时有用） |

### G. 恢复与运行

| # | 主题（来源） | 价值依据 | Claude Code 桌面版 | 处理 |
|---|---|---|---|---|
| 37 | 结果未知就停，恢复不等于重试；先查实际状态（H16、H17、T13、T19） | acpx 的 requestId 不是幂等键 | 会话持久化；交互使用时由你判断 | 📝 一行（机制 ❌） |
| 38 | 账本、收据、审批摘要、状态机、退出码体系（H/T 多项） | 一笔真实费用都没录入；大部分时间花在“状态机拒绝后人工处置” | 有会话转录、`/usage`、OpenTelemetry；没有任务级台账 | ❌ |
| 39 | “下一步做什么”的提示和收件箱（H38） | — | 侧边栏会话状态、需要输入时通知、PR 状态条 | ✅ |
| 40 | 定时或无人值守运行 | — | 桌面版本地定时任务（应用要开着）、云 Routines、`/loop` | ✅ |

### H. 工具踩坑知识（Claude Code 不会提供，建议留一个知识文件）

- **Windows / PowerShell**（W31、H29–H33、T26）：
  - PS 5.1 用 `-File` 运行时按 ANSI 读脚本，要兼容就保持纯 ASCII；
  - 脚本不写 `[CmdletBinding()]`，未知参数会被静默吞掉（`-DryRun` 因此被忽略，真的部署了）；
  - 删除要用 `-LiteralPath`；`Remove-Item -Recurse` 会连白名单子树一起删；
  - Python 的 `Popen.wait()` 在 Windows 上会吞掉 Ctrl+C；
  - NUL 设备的 `isatty()` 返回 True；
  - `os.walk` 会进入 junction，互指的 junction 会让遍历死循环；
  - 分支名大小写不敏感，换个大小写的分支名也能被移动；
  - `.cmd` 要经 `cmd.exe /d /c` 调用；
  - Defender 曾把一个 PowerShell 测试判成黑客工具；
  - `git worktree remove` 可能先注销、再删目录失败。
- **Claude Code**（W32）：
  - `%LOCALAPPDATA%\Temp\claude\` 会被整树清扫，要保留的东西别放 scratchpad；
  - 命令的 description 只写“何时触发”；
  - 自定义 `/plan` 会和内置命令冲突；
  - 本次又见：harness 会在后台输出末尾追加 `[exited with code N]`，按“最后一行”匹配的等待循环永远等不到。
- **Codex CLI**（W30、T25）：
  - `codex exec` 必须加 `</dev/null`，曾空等 24 分钟；
  - `--ignore-user-config` 会连带丢掉 `windows.sandbox="elevated"`；
  - read-only 沙箱仍然放行明文 HTTP 出网。

  只在继续用 Codex 时有用。
- **DSH / acpx / ACP**（T23、T24、W33、H35、H36）：
  - 只在回到 DSH 时有用，详细记录已在 HFlow 的 `docs/adr/0001-transport.md`，不必另存。

## 二、落地包

2026-10-18 已应用第 1、2 项（三份全局约定和 `~/.claude/settings.json`，原文件留有 `*.bak-20261018`
备份）；第 6 项即本次归档。第 3、4 项仍是可选项。

### 1. 全局 CLAUDE.md 增补（约 15 行）

写在 `~/.claude/CLAUDE.md`。按你的约定，`~/.codex/AGENTS.md` 和 `~/.dsh/AGENTS.md` 要同步一样的内容。

```markdown
## 工作纪律（来自旧 workflow / HFlow 的真实事故）

- 默认轻流程：读相关代码 → 最小且足够的改动 → 只跑能证明这次改动的测试 → 交出 diff、
  真实命令和退出码、残余风险，并明说哪些没跑。重流程只在我明确要求时用。
- 涉及 auth/权限/密钥、资金计费、数据库迁移或不可逆数据、部署/CI 核心、公共 API 契约时，
  改动前先停下确认；"直接做"不算对这些风险的确认。
- 系统级操作（提权/UAC、注册表、系统代理/证书、杀进程或 VPN、重装运行时）先要我明确同意；
  一种方法失败两次就停下给选项，不升级手段。
- 同一处修复连续失败两次就停，报告试了什么、原始报错、根因查到哪一步。
- "X 会拒绝/防住 Y"类结论要做负向对照：去掉 X 后样本确实通过；守护测试先证明会变红。
- 测试结果只对跑测时的代码有效；之后改了代码就重跑，不复用旧结论。
- 审查最多 3 轮；某轮问题全由上一轮修复引起就停下交给我。blocking 必须写出具体后果。
- diff 里对规则、测试、`.claude/` 配置、hooks、`.mcp.json` 的改动，审查时单独指出。
- 暂存用显式路径，不用 `git add -A`。单次交付的生产代码 diff 超过约 4000 行就拆分。
- "暂时/先这样/回头再说"的妥协要当场修或当面说明。
- 新增流程、规则或检查之前，先说清它防住了哪个真实失败；说不清就不加。
- 并行子代理按批分组，一般不超过 10 个；撞到用量上限先缩小规模。
```

### 2. `~/.claude/settings.json` 调整（桌面版会话同样生效）

- 从 `permissions.allow` 移除 `Bash(env)`、`Bash(printenv:*)`（防止密钥进入对话记录）。之后用到时会提示你确认。
- `Bash(find:*)` 改窄，或移除，因为它放行了 `-delete` 和 `-exec`。
- 新增 `permissions.ask`：`Bash(git push:*)`、`PowerShell(git push *)`。push 前一定先问你。
- 可选的 `permissions.deny`：读取 `.env` / 凭据文件。如果有合理的读取需求，就不加。

### 3. 可选：按项目启用“测试不过不收工”的 Stop hook

- 只在测试快、而且你希望强制门禁的项目里，写进该项目的 `.claude/settings.json`，不放全局。
- 注意要防止循环：hook 输入里有 `stop_hook_active` 字段，可以据此只拦一次。
- 这是第 9 条“教训落成 hook”的最小实践。

### 4. 可选：知识文件

- 把第 H 节的 Windows / PowerShell / Claude Code 踩坑写成 `~/.claude/rules/windows-gotchas.md`。
- 用 `paths:` 限定在 `*.ps1`、`*.py` 等文件才加载，不占每次会话的上下文。

### 5. 不建议做

- 不自建花费上限：桌面版没有，就看 `/usage` 和套餐用量。
- 不默认装 Codex 审查插件：它重新打开“Codex 额度被审查吃光”的口子，`/code-review` 已经够用。
- 不再写任何编排器、账本或状态机。

### 6. 归档

- 两个仓库各推一次、打 `final` 标签，README 顶部注明“已归档，经验见本文件”。
- 本文件就是两个项目的最终产出。

## 三、仍需实测的桌面版行为

官方文档没写清，用到时实测：
- `/rewind`（检查点回退）在桌面版的入口；
- WSL 会话里的沙箱；
- `/remote-control` 的入口（本会话有远程控制设置，应可用）；
- Stop hook 在 worktree 会话中，是从主检出读取 hooks。

## 附：主要来源

- 旧 workflow：
  - 规则与命令：`claude/CLAUDE.md`、`claude/workflow/AGENTS.md`、`claude/commands/*.md`；
  - 事故记录：`docs/ai/archive/*`、`docs/ai/HANDOFF.md`、`docs/ai/DSH-LANDING-NOTES.md`；
  - Team 模式：`team/policies/*`、`team/spike/*`、`team/runtime/*`、`docs/ai/TEAM_RUNTIME_CLOSEOUT_20260918.md`；
  - 提交：`5b73751`、`9c37efe`、`77244d1`、`dd3a2b1`、`4ea9e16`、`b1ded59`、`4e27425`、`df9c6ce`。
- HFlow：
  - `AGENTS.md`、`docs/architecture.md`、`docs/adr/0001-transport.md`、`docs/m2-live-acceptance-result.md`、`docs/history/Harness_Agnostic_Workflow_Greenfield_Plan_2026-09-19.md`（§1.2 成功定义）；
  - 提交：`2ea0020`、`63834d7`。
- Claude Code：
  - 总览与 worktree：code.claude.com/docs/en/desktop、/worktrees；
  - 权限与沙箱：/permission-modes、/permissions、/sandboxing；
  - hooks：/hooks、/hooks-guide；
  - 子代理与 Workflows：/sub-agents、/workflows；
  - 审查与记忆：/code-review-local、/ultrareview、/memory；
  - 成本：/costs、/cli-reference；
  - 定时任务：/desktop-scheduled-tasks、/routines；
  - `/goal`：/goal；
  - Codex 插件：github.com/openai/codex-plugin-cc。
