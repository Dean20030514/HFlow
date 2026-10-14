# HFlow：GitHub 调研与完整实施方案

**调研日期：2026-09-21**  
**HFlow 源码基线：`9483a84b03332fd4009697ee6d9f3147bcebd878`**  
**定位：保留现有 HFlow，DSH-first，将其收敛为可控、可验证、低维护成本的 Harness-agnostic 开发控制器。**

> 本方案不是让你再重写 HFlow，也不是修复或重构 `dual-agent-workflow`。目标是停止“为了工作流而不断开发工作流”，优先让现有 HFlow 交付真实项目中的小任务。团队与子代理是后续按收益启用的能力，不是第一版必须全开的功能。

## 阅读与证据说明

本文区分三种内容：**已核实的源码/仓库记录**、**基于这些事实的判断**、**拟实施设计**。新字段、命令、阈值与任务分期若标为“拟议”，不代表当前仓库已支持。

本次先完成 GitHub 候选搜索与两个用户仓库核查，再制定方案；检查了 HFlow 核心契约、准入和验证代码、M2 验收与离线恢复记录；外部项目以维护者自己的仓库和文档为依据。不是逐行覆盖全部仓库的安全审计。容器内尝试克隆时遇到 GitHub DNS 解析失败，因此**未运行 pytest、未复测 Windows/DSH、未调用真实模型、未修改或推送任何仓库**。文中的 `197 passed, 1 skipped`、强停通过等是仓库记录，不是本次独立实测。

`[Hxx]` 是 HFlow/旧仓库证据，`[Rxx]` 是外部调研来源；完整来源地址见文末。外部 `main/master` 阅读结果仅说明读取时的上游能力，**不等于你本机已安装版本拥有相同能力**。

---

## 一、结论与决策

### 1.1 推荐路线

采用：

```text
HFlow 的项目契约、预算、状态、候选与证据账本
                       ↓
              小而确定性的控制器
                       ↓
       可替换 Driver / 本机 Binding / 能力记录
                       ↓
        已选定的 acpx → 官方 DSH ACP
                       ↓
              DSH 原生推理和工具循环
```

核心决策如下。

| 决策 | 执行含义 |
|---|---|
| 保留当前 HFlow | 复用已有契约、SQLite、状态机、Driver、候选冻结、审查解析、清理与离线恢复，不再次从零搭骨架 |
| 旧仓库退出本轮实施范围 | 只作历史设计与经验来源；不迁移全局安装器、不复制完整 Team 运行器、不继续维修旧仓库 |
| DSH-first，而非 DSH-only | 当前唯一真实执行绑定可为 DSH；核心契约不能依赖某个厂商的角色名、模型名或全局目录 |
| acpx 继续作传输部件 | 不重做它已经解决的 ACP 客户端功能；也不把其新出现的 flow 引擎和 HFlow 同时变成工作流事实源 |
| v0.1 只承诺受监督的单任务闭环 | 一名实现者、一次固定程序验证、一个独立 reviewer、一个本地候选；默认不自动修复、不并行、不启用子代理、不发布 |
| 新增能力须先证明收益 | 以实际交付、总调用负担、维护负担为准，不能以 Agent 数量、协议页数或测试数量代替生产力 |

### 1.2 “Harness-agnostic”应当表示什么

它表示**控制器与特定 Harness 的接口、会话、模型选择和配置方式解耦**，而不是“所有 Harness 能力完全相同”或“任意模型都能塞进任意原生 Harness”。

建议坚持：模型只能通过其绑定的、真实可用的原生 Harness 路径使用；HFlow 不模拟厂商内部工具循环。不同 Driver 可以报告不同能力，不支持的能力明确拒绝，不伪装成已执行。

一种合理的当前状态是：

```text
业务协议与控制器：通用
真实生产绑定：只有 DSH，且仍是受监督使用
其他 Harness：未来需求驱动接入
跨 Harness 一致性：先通过两种离线测试 Driver 验证控制面
```

只有一种真实 Harness 时，可称“接口与控制面按 Harness-agnostic 设计”；不能据此宣称“多个 Harness 已通过真实一致性验证”。

### 1.3 不承诺不存在的成本保证

HFlow 能控制的是它自身可观察、可阻断的派发次数、并发、截止时间和本地进程生命周期。一次 Harness invocation 可以包含多个模型请求、内部重试和工具回合。**两个 invocation 不等于两个模型请求，更不等于固定比例的订阅周额度。**

建设 HFlow 时，你与 DSH 的开发会话也会消耗资源。后文“新增 live 调用为 0”仅指该任务不额外通过 HFlow 启动真实 worker，不表示 DSH 编写这段代码免费。

---

## 二、HFlow 当前基线：保留什么，不能夸大什么

### 2.1 已存在的成果

依据 README、代码和 M2 记录，当前已有以下值得保留的实现。[H01–H09]

| 能力 | 当前证据与边界 | 本方案处理 |
|---|---|---|
| 单一契约源 | `contracts.py` 定义 Pydantic 模型并生成 JSON Schema | 保留，不维护第二套手写 Schema |
| 确定性准入与记账 | `admission.py`、SQLite 事务与派发预留 | 保留，并补准入缺口 |
| 独立实现与审查 | 两个 invocation，独立会话/进程及分开的额度记录 | 保留，不以实现者自评替代审查 |
| 真实 Driver | `drivers/acpx_dsh.py`，使用已选择的 acpx/DSH 链路 | 保留，先整理绑定与离线验证 |
| Git 候选交付 | worktree、固定 base、冻结 candidate、固定检查、local receipt、受保护清理 | 保留，不重复实现 |
| reviewer 输出解析 | 只从该 reviewer 的最终有效消息解析 canonical `ReviewOutput` | 保留，不调用模型修 JSON |
| 离线恢复 | 已有 recorded review replay 与 guarded finalization | 保留来源、原始失败与后来决策，不改写历史 |
| 查询无模型 | `doctor/status/report`，`resume` 只协调已发生的工作 | 保留，不让查询触发 Agent |
| 自动化测试 | README 记录 `197 passed, 1 skipped` | 视为仓库报告，实施时在目标环境复核 |

### 2.2 M2 应怎样准确描述

当前最准确的描述是：

> **M2 候选已通过基于既有证据的离线恢复完成本地交付；不是一次未中断、未补线的真实端到端成功运行。**

需要同时保留四组身份。[H03]

| 身份 | 值 |
|---|---|
| 本次调查的 HFlow tip | `9483a84b03332fd4009697ee6d9f3147bcebd878` |
| 离线最终处理 build | `hflow/0.0.1+f1c68c2237f56e81558e75d364c1ade7de26dfac` |
| 原始执行 | `R-gkb3ld97x8`，原始 runner `3dbfeae`，`BLOCKED / review_rejected` |
| 交付候选 | `499ece7043fe3267b4ff89f9a5b5bc1d70c42481`，后来的决策为 `ACCEPTED / LOCAL_CANDIDATE` |

原来的 reviewer 实际返回了 accepted，但 Driver 当时丢弃了 structured verdict。修复与离线重放让有效结果进入了 canonical 接受路径；后来 receipt 保留原始失败的 provenance。恢复过程没有购买新模型执行，`AUTH-m2-live-2` 仍为 `used 2/2`。

**不应为了做出一张“全绿截图”重新购买这个已完成候选。** 下一次干净闭环应顺便在一个新、确实有价值、另行授权的业务任务上建立证据。

### 2.3 强停与安全不能混为一谈

M2 A 记录了真实 forced stop 的 PASS：DSH 启动了 helper，OS 证明进程归属于该 Job，强停后相关边界为空。证据只覆盖该机器、版本及绑定，而且测试使用的权限组合有明确限制。[H02]

这**不是**：

- 协作式 ACP cancel 的验证；所选 one-shot exec 路径仍不支持该语义。
- 强只读沙箱、任意工具进程隔离或所有平台都能安全终止的证明。
- 远端模型请求终止或远端计费停止的证明。

证据未变且绑定未变时，沿用既有 A 证据；启动/权限/进程边界相关组件变化时才做影响分析，并在确有必要且另行授权后复测。

### 2.4 当前先关闭的具体缺口

以下是基于已读内容发现的具体问题，不代表穷尽式漏洞清单。

#### A. 当前状态与历史文档混写

README 仍将 `forced_local_stop_live` 写为未测试；验收记录已写为该绑定 passed。`docs/architecture.md` 虽标明 M1，但正文仍包含“只有 fake”“无 production Driver”等旧描述，同时又混入后来 reviewer 解析的实现说明。Operations 也保留 M1 时代的限制。[H01/H02/H04/H09]

**处理：**保留历史记录，但标上 tested build、阶段和 superseded 指向；一个当前入口汇总能力与证据。不要删除历史，更不要让阅读者把不同时间的“今天”当成同一状态。

#### B. Reuse-first 已有契约，但准入不足

`ReuseDecision` 已存在，无需重新发明。`validate_task_spec()` 当前仅在 `choice == "reuse"` 且 `fit_test_status == "pending"` 时阻止派发；该函数没有同样拒绝“仍选择复用但适配测试失败”的组合，也没有对 `adapt` 做相同处理；既有决策主要检查 reference 非空，尚未证明其内容、有效期和适配证据。[H05/H06]

**处理：**先用表驱动离线测试补上组合规则，再把决策引用与测试证据绑定。不要再加一句“记得搜索 GitHub”了事。

#### C. 通用契约还有厂商专用字段

`MachineProfile`、`AgentBinding` 和 `CapabilityReport` 的结构已存在；README 仍说明机器绑定未正式落地。`ProfileLimits` 内还有 `codex_agent_turns` 字段。[H01/H07]

**处理：**在真正接通 profile loader 时，将厂商特例迁到本机 `binding_limits` 或 allow/deny policy；不因这一字段进行全仓重构。`implementer/reviewer/planner` 可以保留为执行阶段类型，与“前端/后端/测试”等组织角色区分。

#### D. 普通程序验证也需要资源管理

`CommandCheckRunner` 使用普通 `subprocess.run`，合并环境变量、向临时文件输出、结束后完整读取 stdout/stderr。所读实现没有为此路径建立与 Harness 一样的 owned-process 边界，也没有对输出文件设置字节上限。[H08]

**处理：**为固定检查复用已有进程边界、显式环境变量与输出上限，避免 worker 结束后测试子进程仍存活、日志耗尽资源，或不必要地继承凭据。先写本地 stub 测试，不做真实模型验证。

#### E. 不支持的交付层级目前只是 warning

准入函数对请求 integrated/published 的任务发出警告，并说明仅交付 LOCAL_CANDIDATE。[H06]

**处理：**改为默认提前拒绝；只有用户明确允许降级时才交付低一级结果，receipt 同时保存 requested 与 achieved。禁止“成功退出”掩盖未完成的交付目标。

#### F. 授权记录不是防伪安全边界

当前文档诚实承认 trusted-local 模式：`provided_by=user` 不能证明文本确由人类签发，同权限 executor 可能修改文件/数据库；换一个 authorization ID 也会得到新的每-ID额度。[H02]

**处理：**当前继续受监督，不把普通 JSON 称为防伪许可。先补跨 run/authorization 的项目预算上限以防意外耗费；只有真正需要无人值守时，才引入 executor 无法改写的授权主体与隔离边界。

---

## 三、GitHub 搜索与对标结果

### 3.1 搜索方法

本次实际进行的 GitHub repository 搜索包括：

```text
"harness" "orchestration"
acpx in:name
agent-orchestrator in:name
dsh org:deepseek-ai
"agent" "worktree" "orchestrator"
python-sdk org:agentclientprotocol
```

随后沿维护者链接读取 README、协议说明和 HFlow 中直接相关的代码。广搜出现的项目并不自动进入建议；例如 `native-cli-ai` 经读取后发现它本身就是另一套编码 Harness，而不是应塞进 HFlow 的轻量调度库。

筛选依据：原生 Harness 保留程度、机器可读接口、生命周期和故障语义、Windows/本机适配负担、是否增加第二套状态/调度/记账、许可证可核实程度，以及当前维护状态。**不以 stars 数量代替适配证据，也不把 README 宣称当成实测。**

### 3.2 九个重点项目

| 项目 | 实际看到的能力/边界 | HFlow 决策 |
|---|---|---|
| `deepseek-ai/deepseek-harness` | 官方 DSH；ACP 自动化端口；原生工具和子代理；README 明确 developer preview、存在破坏性变更 | **直接复用执行引擎**，仅采用已锁定且验证的组成；不 fork Harness [R01–R03] |
| `openclaw/acpx` | 无界面 ACP 客户端、one-shot/persistent sessions、NDJSON、权限、取消/会话控制；当前还提供 flows/runtime；pre-1.0 | **保留已选传输部件**；不同时运行第二套 flow 状态机 [R04] |
| `agentclientprotocol/python-sdk` | 生成的 Pydantic schema、asyncio、stdio JSON-RPC、生命周期/权限/累积器辅助 | **候补传输实现**；只有 acpx 有具体不可解决阻碍时再评估切换，不立即双轨 [R05] |
| `Untrivial-ai/agent-orchestrator` | 当前是项目级协调、worker、独立 branch/worktree、PR/CI/review、看板与本地 daemon 的完整工作台 | **借鉴任务/工作区/反馈建模**；不把桌面产品整体搬进 Python CLI [R06] |
| `awslabs/cli-agent-orchestrator` | 保留原生 CLI 进程与认证，以本地 server、隔离终端和 supervisor 协作；要求 tmux 等组件 | **借鉴原生 CLI 协作边界**；不作为 Windows DSH-first 内核直接依赖 [R07] |
| `BloopAI/vibe-kanban` | workspace、agent 切换、diff review、PR 等交互；当前 README 标注 sunsetting | **仅参考交互和历史实现**；不引入新的核心运行依赖 [R08/R09] |
| `gastownhall/beads` | 依赖图、ready/claim、持久化任务；当前采用 Dolt，init/setup 有额外集成行为 | **借鉴依赖/认领语义**；暂不新增第二套数据库与任务事实源 [R10] |
| `madebyaris/native-cli-ai` | Rust 编码 Harness，内置 providers/tools、会话、子代理、worktree、自动研究 | **排除作为 HFlow 内核**；这是更换/添加 Harness，不是复用一个调度部件 [R11] |
| `obra/superpowers` | 组合式软件开发 skills、规划、TDD、subagent-driven-development；安装方式依 Harness 而异 | **选择性借鉴流程内容**；不整包迁移全局 hooks、安装器或固定子代理循环 [R12] |

### 3.3 两项不能沿用旧印象的信息

**Vibe Kanban：**官方公告发表于 **2026-04-10**，明确是背后的 bloop 公司关闭，项目继续开源、由社区维护。不能把“公司关闭”说成“代码完全不能用了”，也不能继续把它视为原公司持续提供服务支持的方案。[R09]

**Beads：**旧地址 `steveyegge/beads` 本次访问跳转至 `gastownhall/beads`。当前文档将 Dolt 作为事实存储，JSONL 是导出/互通格式，不是 source of truth。采用它意味着实际引入另一套状态体系，而不是“加一个轻量 JSONL 文件”。[R10]

### 3.4 为什么不直接换用一整套现成 orchestrator

这是项目适配判断，而不是宣称外部产品较差。

完整工作台适合需要其 UI、daemon、PR 生命周期和既有代理生态的人；但你已经有了能记录预算、冻结候选、独立审核及恢复结果的控制器。现在整体切换，会重新支付配置、适配、故障和数据迁移成本。

**最有价值的复用单位应该是边界清楚的部件与实践，而不是另一整套产品。** 对 HFlow 而言，执行层交给 DSH，标准传输交给 acpx，候选交给 Git，事务账本交给 SQLite，Schema 交给现有 Pydantic。自行维护的只剩业务验收、预算与证据之间的关联规则。

这也不是永远禁止换方案。若后续同类真实任务证明现成产品满足同样的安全/预算要求且维护负担明显更小，可以迁移；但需要迁移收益证据，不能因为界面漂亮或上游新增一个功能就再次重写。

### 3.5 上游复用登记与兼容性验证

每个真正引入的组件应记录：`repo/package`、精确版本或 commit、读取日期、许可证位置、采用的 API、适配测试、已知限制、升级触发条件。已读 README 能确认 DSH/acpx 的 MIT 与 AO/CAO 的 Apache-2.0 声明；其他候选在实际引入前仍需读取 LICENSE 与相关第三方声明，本文不将未读取部分视为已完成许可核验。

DSH 与 acpx 的“最新能力”不自动回填到你当前绑定；本方案继续以 HFlow 已记录的 **acpx 0.17.1** 链路为起点。精确 DSH/package/profile/plugin 版本组合应从本机记录生成，不能凭上游 README 推断本机具备某个字段或 profile。

---

## 四、目标架构：薄控制器与三类数据

### 4.1 职责划分

```text
人类 / 外部需求入口
       │
       ├─明确的小任务─────────────┐
       └─模糊需求→可选规划 invocation│  规划也计入额度
                                  ▼
                         TaskSpec / WorkPackage
                                  │
                     准入 + 复用决策 + 能力匹配
                                  │
                     SQLite 预算预留与执行账本
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
      Git 工作区/候选管理                       Harness Driver
              │                                       │
              │                             acpx → 官方 DSH ACP
              │                                       │
              └─────────────候选产物───────────────────┘
                                  │
                   固定程序检查 → 独立 reviewer
                                  │
                        Controller-owned Receipt
                                  │
           本地候选 / 显式集成 / 显式发布（分期开通）
```

| 层 | 负责 | 不负责 |
|---|---|---|
| 契约层 | 目标、范围、验收、风险、额度、交付要求 | 模型 API、DSH YAML、Shell 拼接 |
| Controller/Store | 准入、状态、预算、所有权、取消优先级、接受与恢复 | 思考下一行代码、运行模型推理循环 |
| Workspace/Verify | 固定 base、候选、检查、输入指纹、清理 | 模型声称测试通过就通过 |
| Driver | 启动/观察/收集/终止/协调、协议归一化、能力报告 | 自行加预算、接受业务任务、决定再次调用 |
| Harness | 原生模型会话、工具、推理、受允许的子代理 | 签发最终 receipt、改写授权与事实账本 |
| Report | 将现有事实确定性渲染成终端/JSON/Markdown | 为生成报告再次调用 LLM |

### 4.2 三类数据严格分离

**项目契约：**放在目标仓库 `.hflow/`，版本化，包含 check allowlist、项目限制、scope deny、复用决策与稳定的项目上下文。

**本机绑定：**放在用户自己的 HFlow config 路径，不进目标仓库。保存可执行文件、安装版本、profile/插件组成、凭据引用名与能力证据。Windows 可使用 `%LOCALAPPDATA%\HFlow\config\`；具体路径为拟议，必须与现有运行时目录选择兼容。

**运行时数据：**继续使用已有平台数据目录，保存 SQLite、invocation 原始事件、有上限的日志、候选/证据引用。默认不复制到 GitHub，不把 API keys、完整 HOME、私有会话或系统环境转存进文档。

### 4.3 不新增第二套事实源

现有 Store 继续是 run/attempt/receipt 的唯一机器可写事实源。Markdown 是输入规范或生成视图，不是另一个需要模型同步的数据库。acpx 自己保存会话状态不等于它拥有 HFlow 业务任务状态；Git 对象拥有代码身份，不拥有业务验收状态。

不引入另一个任务数据库、分布式消息总线、向量记忆、Web 控制面或第二套持久化 workflow 引擎来完成当前单机串行需求。

### 4.4 目录演进，不进行目录大搬家

保留当前 `contracts.py`、`admission.py`、`authorization.py`、`controller.py`、`store.py`、`workspace.py`、`verify.py`、`review.py`、`report.py`、`cleanup.py`、`drivers/*` 和现有测试。

只在有明确调用者时增加小模块，例如拟议的 `bindings.py`（接通现有 MachineProfile）、`reuse.py`（决策引用校验）、`metrics.py`（确定性汇总）。不要先建立包含几十个空插件的框架。

文档以 README 当前入口、architecture、operations、ADR 为主；阶段结果按 tested build 存放。旧根目录 instruction 文件先保留并标历史状态，确认引用后再整理，不做未经核实的批量删除。

---

## 五、目标工作流：先让一个有价值的任务稳定完成

### 5.1 第一版的运行合同

下面是**建议配置目标**，不是声称现有 CLI 已支持全部配置项：

```text
execution_mode                 supervised / trusted_local
real_harness_binding           certified DSH binding
concurrent_tasks               1
implementer_invocations        at most 1
reviewer_invocations           at most 1
planner_invocations            0 by default
native_subagents               disabled
automatic_repair               disabled
automatic_provider_fallback    disabled
delivery                       LOCAL_CANDIDATE
new_live_authorization         explicitly granted for this new task
```

标准任务正常路径至多两个 HFlow 顶层 invocation。程序检查不占模型 invocation，但占 CPU、时间、磁盘，仍应有预算。实现失败时不为“完成流程”启动 reviewer；reviewer 的未用名额也不能自动挪作第二次实现。

独立审查不是首先要删掉的成本。应先删的是重复背景分析、重复全量检查、LLM 格式修复、对同一失败的盲目派发，以及为了补报告再次调用模型。

### 5.2 从需求到交付的正常路径

| 步骤 | 执行者 | 产物与门槛 | 模型调用 |
|---|---|---|---|
| 1. 明确任务 | 用户；必要时另行授权的 planner | 可检查的目标、范围、验收条件、固定 base | 明确任务可为 0；planner 必须另计 |
| 2. 查项目上下文 | 确定性工具，必要时研究 invocation | 项目契约、已有实现、复用决策引用 | 默认不新增研究 Agent |
| 3. 准入 | Controller | 验收/check ID、scope、复用、能力、绑定、预算与交付要求有效 | 0 |
| 4. 本地预检 | Driver + 既有 mock/配置工具 | 当前绑定可启动；配置合法；需要的能力有证据 | 不发送真实模型 prompt |
| 5. 领取预算 | Store | 事务性预留、记录 invocation/attempt 身份 | 0 |
| 6. 创建工作区并实现 | Workspace + 独立 DSH invocation | 固定 base 的一次性 worktree，范围内变更 | 至多 1 个实现 invocation |
| 7. 冻结与固定验证 | Controller + CheckRunner | 不可变候选引用、范围检查、正式验证证据 | 0 |
| 8. 独立审查 | 新进程/新会话 reviewer | 接收候选、diff、AC 和正式验证证据；返回合法 verdict | 至多 1 个审查 invocation |
| 9. 接受 | Controller/Store | 重新检查身份、取消意图、候选/证据绑定，事务性 receipt | 0 |
| 10. 展示与保留 | Report/Cleanup | 简短结论、候选位置、限制、成本观测；显式清理 | 0 |

**顺序细节：**沿用现有安全准入顺序，不为了表格机械重排代码。关键不变量是：未经授权不能读取 live 凭据或创建真实执行资源；预算必须在真实启动前完成事务性预留；任何启动前的本地预检失败不能假称已发送模型请求。

### 5.3 任务粒度

第一批任务选择“一个可描述的行为改变、一小组相关文件、一个明确验收入口”。例如修复一个参数边界、补一个已有接口的响应校验、修复某个业务状态转换；不要把“做好整个小程序后端”作为单次任务。

复杂需求才进入规划路径。planner 输出简短的任务依赖、范围、AC 和复用决定，不输出庞大的角色组织说明。Planner 的建议由准入规则决定能否执行，不能自己修改上限或授予新能力。

建议规划失败最多允许一次受预算覆盖的修正；在第一版未实现这一行为时，直接返回 validation errors，由当前操作者修改，不启动第二个模型来修 JSON。

### 5.4 继续使用现有状态机

不新建另一套“智能状态机”。沿用当前词汇：[H05/H07]

```text
Task:     DRAFT → READY → RUNNING → CHECKING → ACCEPTED
                            └──────────────→ BLOCKED / CANCELLED

Attempt:  CREATED / ACTIVE / SUCCEEDED / FAILED / CANCELLED
          OUTCOME_UNKNOWN / SUPERSEDED

Delivery: NONE / LOCAL_CANDIDATE / INTEGRATED / PUBLISHED
```

状态不够细时优先增加明确的 `phase`、typed reason 或 evidence，而不是为每次故障增加一个顶层状态。执行状态与交付层级分开：退出的进程不是 accepted，accepted 的本地候选也不是 deployed。

### 5.5 历史成功与当前成功分开

相同 TaskSpec 再提交，应继续返回原来的执行，不自动购买第二次工作。输出清楚区分：

```text
historical decision: ACCEPTED / LOCAL_CANDIDATE
original candidate:  <candidate identity>
current checkout:    matches / changed / unknown
new dispatches:      0
```

工作区已变化，不得把历史 accepted 说成当前 checkout 通过验收。需要对新基线工作时，显式创建新 revision，并继续受同一 work package 的总预算约束。

---

## 六、预算、幂等和失败恢复：真正减少浪费的控制面

### 6.1 至少区分四个计量层次

| 层次 | 可用含义 | 不得混淆 |
|---|---|---|
| 提交/准入请求 | attempted、admitted、blocked_before_dispatch 等控制器决策 | 不是 provider 请求次数；状态查询不是新尝试 |
| HFlow invocation | 实现、审查、规划、研究各自的启动/预留 | 一个 invocation 可以触发多个模型请求 |
| Harness 内部活动 | 只有当前接口确实提供时才记录步骤、工具调用、内部子代理或重试 | 没有暴露不能记 0；不能用文本自报还额度 |
| Provider/订阅 | 经可验证来源取得的 billed tokens、费用、剩余额度 | 不能从工具次数、墙钟时间或 HFlow invocation 推算精确周额度 |

尽量复用现有计数字段，先增加描述与汇总，不为更漂亮的仪表盘重命名整个数据库。`agent_turns` 这类历史名称应在文档中说明其实际粒度。

“失败不返还额度”指已消费的执行授权/预留策略，不表示已证明 provider 对所有失败都收费。启动配置拒绝、没有 ACP prompt 的情况应明确记录事实；已经按旧策略消费的名额不能悄悄改写为未消费。[H02]

### 6.2 建设成本与业务运行成本分账

建议只加两个简单的工作分类，先不建设复杂财务系统：

```text
work_class = workflow_maintenance | business_delivery
```

与 DSH 对话开发 HFlow、修改提示词、重新整理材料，都属于 maintenance；HFlow 对真实项目生成并交付候选属于 business_delivery。

如果没有 provider 真实账单，至少记录每项工作的受控 invocation、可取得的会话用量、人工干预次数和实际投入时间。不能把不可观测的维护用量排除后宣布“节省 90%”。

### 6.3 跨 revision 的根预算

**拟议：**引入轻量 `work_package_id` 与根预算关联，保留现有 run/attempt 机制。一个用户需求的初次实现、改计划、修复、审查和可控子任务共享上限；不能通过改 task ID、revision 或 authorization ID 获得隐式新预算。

最小实现可以是现有 SQLite 中一张根预算记录和显式关联，不需要独立的预算服务。授予新的 authorization 还必须落在已授权的根上限内；真正提高根上限只能由操作者明确批准。

在目前一次性、受监督、无自动重试的模式下，现有绑定严格的 per-task 授权可以暂时保留。**根预算是开放自动修复、自动规划、批量或团队调度之前的门槛，不应为了实施它无限推迟一次已明确授权的小任务。**

同权限用户可修改数据库这一限制仍存在；上述设计防意外重复消耗，不是假装实现了防恶意执行者的安全边界。

### 6.4 事务与不确定窗口

保留“先预留、后启动”的事务边界，另外验证以下崩溃窗口：

| 崩溃位置 | 恢复规则 |
|---|---|
| 预留前 | 没有获得执行资格，不得启动 |
| 已预留、尚未确认启动 | 先核查进程与记录；不能仅因缺少结果就重发 |
| 已发送 prompt、未获得最终响应 | 结果未知，保留预留；禁止自动重试 |
| 已有原始结果、尚未解析 | 离线解析/重放同一证据，不购买新结果 |
| 已验证候选、receipt 未写完 | 通过既有受保护事务协调；验明候选与证据，幂等记录 |
| 已有 receipt | 原样返回，只有新的合法决策才能追加新 provenance |

不能声称跨本地进程、网络和远端模型服务“严格 exactly-once”。HFlow 能做到的是：**不在未知结果下擅自再次派发，并让可能已发生的消费可追踪。**

预算预留的回收如果未来要支持，只能对有明确“未产生相应执行”的证据、经过明确策略批准的情况处理，并新增审计记录。第一版不必开发退款逻辑。

### 6.5 幂等键与运行绑定各司其职

业务身份和执行身份分开：

```text
logical task identity: project_id + task_id + revision + canonical spec digest
execution binding:    base commit + project contract digest + runtime/binding identity
```

绑定变化意味着既有能力证据可能失效，**不意味着允许对旧任务自动多执行一次**。重复请求先返回已存在的历史执行；真要对新绑定再做任务，必须显式形成新执行请求，并受根预算/新授权限制。

不要在幂等键中偷偷加入当前时间、随机 seed、每次启动生成的 build suffix，导致每次都被视为新任务。

### 6.6 失败分类和唯一默认动作

| 情况 | 默认动作 | 是否新增模型调用 |
|---|---|---|
| 可执行文件、配置字段、版本不兼容 | 停止；通过真实客户端 + mock 离线定位 | 否 |
| reviewer 输出格式/归属不合法 | `review_protocol_error`；保留原始输出；若是解析器缺陷则离线回放 | 否 |
| reviewer 合法返回 changes_requested | 保留业务意见；进入 blocked | 第一版否；后续按已批准的修复预算 |
| 正式测试真实失败 | 保留失败证据；定位业务缺陷还是环境故障 | 第一版否 |
| outcome_unknown | `resume/reconcile` 查询事实，无法确定则继续 blocked | 否 |
| scope_violation / candidate drift | 停止验收，保留工作区和证据 | 否 |
| 预算耗尽 | 返回限制与剩余状态，不自建新 authorization | 否 |
| 本地停止未确认 | 禁止清理相关工作区，保留进程身份 | 否 |
| provider 暂不可用 | 停止，解释当前绑定失败 | 不自动切 Codex/其他 provider |

**恢复与重试是两件事。**恢复处理已发生的工作；重试购买一次新执行。`resume` 这个名称不得被改造成隐式重试按钮。

### 6.7 防止“工作流继续吃掉工作流预算”

建议采用以下运营限制，数值是起始策略而不是已实现特性：

- 同一阻塞默认只做一个范围明确的修复任务，不升级成大规模框架重构。
- 连续两个真实业务任务因 HFlow 基础设施而非业务代码失败，暂停新增 live 派发，优先本地复现；不要启动第三次来“确认”。
- 已交付候选不为统计和展示重做；新闭环证据放在下一个有价值任务中取得。
- 维护工作持续超过可观测总投入的约 20% 时，冻结可选特性；这个阈值是建议，不是当前实测。
- 需要继续推进业务时，由人明确选择直接 DSH 的受监督模式；这不是 Controller 的隐藏 provider fallback。

---

## 七、验证、独立审查与上下文：省掉重复，不省掉证据

### 7.1 接受只看绑定的证据

建议保持这一判据：

```text
candidate scope is allowed
AND required program checks passed on that exact candidate/input set
AND independent review produced a valid accepted verdict when required
AND evidence belongs to the current task revision and invocation
AND there is no winning cancellation intent or superseding attempt
AND actual delivery meets the approved requirement
```

worker 输出“完成了”、进程 exit 0、最后一句包含 accepted，都不足以成立。

当前 reviewer 解析器已经解决了最终消息归属、JSON 严格解析、终止响应和原始证据回放等问题。应延续这条路径，不增加“第二个模型纠正 reviewer 输出”的 fallback。[H03/H07]

### 7.2 对 reviewer 的最小输入包

```text
1. 任务目标、验收条件与允许的范围
2. 固定 base 和 candidate commit/tree 身份
3. 已产生的 diff 与必要文件引用
4. 正式验证的 check IDs、命令摘要、结果与 evidence refs
5. 尚未证明的限制
6. canonical ReviewOutput 的最小输出示例
```

不给 reviewer 实现者完整聊天历史，不用实现者的“我觉得没问题”当验收证据，也不让其继承实现者的写权限。

组织角色可以同为 DSH，但审查必须是新的独立 invocation/session。它能降低同会话自我辩护和上下文污染，**不能保证两个相同模型没有共同盲点**。

### 7.3 只复用确实相同的检查输入

当前验证逻辑已按 candidate fingerprint/check digest 等条件复用同一 run 的通过证据，不应再做另一套重复缓存。[H08]

增强时保守使用：

```text
verification key = hash(
    complete candidate Git tree,
    approved check definition and argv,
    relevant lockfile / toolchain identity,
    approved environment profile,
    explicit external fixture/input identity
)
```

第一版优先整棵候选树，避免引入复杂“AI 猜测受影响文件”的缓存失效机制。非 Git 或无法枚举检查输入时，宁愿禁用缓存，也不借 scoped fingerprint 宣称覆盖了所有依赖。

检查定义、依赖锁、运行时或关键环境改变时，旧证据不能作为新环境的通过证明。网络、时间、随机性、外部数据库状态等无法固定的检查应 `cacheable=false`；不能仅因上次通过就跳过。

如果将来扩展到跨 run 缓存，必须额外审查检查可信度和输入完备性；**本方案不把当前的同-run 缓存说成已有全项目跨-run 缓存。**

### 7.4 记录复用，不伪造新验证

证据至少保留原检查时间、来源、输入指纹。复用时增加引用或 reuse 时间，不覆盖原 `verified_at`。

示例展示：

```text
unit: PASS — reused from E-123
original verification: <timestamp>
input identity: unchanged
new execution of check: no
```

如同已完成的 M2 离线恢复，应说“关联了既有验证证据”，不能说“又跑了一遍验证”。[H03]

### 7.5 把开发测试与正式验收分开

实现者可以运行小范围测试协助开发；这是其工作的一部分。冻结候选后由固定 CheckRunner 执行正式检查，形成可复用证据。

reviewer 默认消费这份正式证据，而不是再跑整套测试。只有检查缺失、输入不符、证据过期或验收存在未覆盖风险时才提出额外验证。新增验证命令必须经过项目契约允许，不能从任意模型输出直接执行。

对复杂项目，可用“快速必选检查 + 风险触发的补充检查”，但触发条件必须显式定义；不用“每个角色一律重新全量测”代替设计。

### 7.6 CheckRunner 的最小加固

对现有 `verify.py` 做小范围增强，不造通用容器平台：

1. 为检查及其后代提供受控生命周期，优先复用已有进程边界相关实现；Windows 与其他平台分别报告支持程度。
2. 显式传入必要环境变量。默认不要继承模型 API keys；确需业务测试凭据时使用项目批准的引用，不记录明文值。
3. 设置输出文件字节限制，并流式计算摘要/保留有限片段，避免先读完整巨量输出再截断。
4. timeout、取消、输出超限均形成明确 evidence error，停止本次检查及其受管后代；无法确认停止时不声称清理成功。
5. 对检查前后候选身份变化做现有一致性检查，不能让测试脚本顺便改代码后仍接受旧指纹。

“完整 Git tree 相同”仍不是强防篡改证明；同权限环境下其他进程可能改写文件。第一版明确 trusted-local，需要对抗恶意进程时再引入真实隔离。

### 7.7 文档和上下文限额

建议每次任务只带：稳定项目摘要、当前 TaskSpec、少量必要文件/符号和证据引用。全局历史 instruction、旧失败日志、完整 GitHub 研究报告都不应默认注入每个 worker。

任务结束只生成一份短 receipt 视图：结果、候选、验证、审查、成本观测、限制、下一动作。详细日志保留在本地引用，不让 LLM 每轮再写一篇长报告。

初始可将稳定上下文限制在数千字级、故障片段限制在几 KB；这些仅是可配置起点，不能以截断关键验收信息换取漂亮的上下文数字。

---

## 八、Binding、能力与安全：通用接口不等于同等保证

### 8.1 复用现有协议，不新建平行接口

当前 `HarnessDriver` 和扩展 `LifecycleDriver` 已覆盖所需接口。[H07]

```text
probe(binding)
start_handle(request)
observe(handle)
collect(handle)
cancel_handle(handle)
reconcile_handle(handle)
```

通用 Controller 只消费这些结果，不解析 DSH 私有文件，也不写 DSH 的具体配置键。DSH/acpx 配置生成和私有事件映射留在 Driver；未认识的字段拒绝或显式报告未知，不猜测。

正式接通 MachineProfile 时，首先做现有 schema 的加载、解析、引用和有效权限测试。不要又设计 `AgentConfigV2` 并保留两份同义结构。

### 8.2 角色、用途、绑定、模型分开

建议形成如下关系，示意不是可直接执行的当前 JSON：

```text
Task purpose:  frontend component / backend fix / test repair / research
Execution role: implementer / reviewer / optional planner
Role binding:  approved local binding ID
Binding:       native harness + driver + executable/profile + version
Model choice:  harness-advertised/configured option
```

组织角色由任务决定，不需要固定四名长期驻留 worker。`implementer/reviewer/planner` 可作为稳定的运行阶段类型保留；它们不等于把“前端一定用某模型”写死。

`codex_agent_turns` 这类厂商字段可逐步迁到通用 `binding_limits`。暂时不用 Codex，意味着未授权该绑定，而不是在控制器里四处写 `if harness == 'codex'`。

### 8.3 能力记录必须绑定实际版本

能力状态可继续使用 `documented / probed / enforced / unsupported / unknown`，并增加或关联这些身份：[H07]

```text
OS and architecture
controller/driver build
actual client executable and version
actual harness version
profile/plugin composition digest
permission/security mode
evidence refs and tested_at
```

源码上游声称支持 session cancel，不代表本机 acpx one-shot 路径已支持。当前上游 DSH 的 ACP 文档与本机已验证能力必须分别记录。[R02/H02]

对没有要求的能力无需做付费认证；开启某能力前才检查其证据。这避免“为了通用性”把每个平台、每种模型、每项协议全部测试一遍。

### 8.4 三件不能写错的安全事实

**Worktree 是变更隔离，不是权限隔离。**同一用户运行的进程仍可能访问其他目录、Git common directory 或凭据。

**进程边界是生命周期控制，不是文件系统沙箱。**证明 helper 被强停不等于证明它此前没越界。

**acpx 的 approve-reads/approve-all 是权限请求处理策略，不是任意 Shell 命令的安全证明。**尤其 approve-all 不能解释成“只批准文件写入”。当前 reviewer 的真实隔离级别应沿用 `prompt_only`/audit-only 等已证明等级，不根据自报提升。[H02/H03]

### 8.5 当前安全模式与未来门槛

| 模式 | 可承诺的边界 | 允许的运行方式 |
|---|---|---|
| 当前 supervised/trusted_local | 固定任务、受控 worktree、显式额度、范围审计、已验证的本地停止路径；不抗同权限恶意执行者 | 人在场，小任务，失败即停 |
| 未来 restricted | OS/容器/独立用户真正约束可写路径、凭据和外部访问，独立验证效果 | 仅在该具体绑定通过验证后声明 |
| 未来 unattended | 在 restricted 之外，还需要可靠恢复、受保护授权、无人看守的停止与通知边界 | 不能仅靠去掉确认框实现 |

不要把第二行和第三行当成本次 v0.1 的全部必做项；也不能在尚未完成时暗示已经具备。

### 8.6 外部内容和敏感数据

GitHub README、issue、第三方代码、工具输出都可能包含对 Agent 的指令。它们作为研究数据，不得提升自己的执行权限、引导读取 API keys 或绕过 HFlow 授权。

评测日志公开前做显式脱敏；原始 transcript 默认本地保存，不承诺自动脱敏绝对可靠。HFlow 配置只存凭据引用，不存值；子进程不自动继承整个 HOME 和所有环境。

对 HFlow 自身的代码修改，使用固定的已安装/已提交 runner 运行，不让正在执行的 worker 热修改当前 Controller 再给自己签验收。可信本地模式下这仍是操作纪律，不是抗攻击防护。

---

## 九、把“先复用轮子”变成准入协议

### 9.1 不把 GitHub 搜索变成每次任务的固定税

需求修复、局部逻辑变更且已有清晰实现时，可以合法 `not_required`。需要新组件、新依赖、协议适配或基础设施时，才要求查已有本地实现与外部候选。

查询顺序：当前仓库 → 已认可的复用决定 → 精确 GitHub 搜索 → 必要时维护者文档。既有决定有效时直接引用，不重新买一轮研究。

### 9.2 最小复用记录

沿用已有 `ReuseDecision`，内容控制在足够决策的范围：

| 字段/证据 | 要回答的问题 |
|---|---|
| need | 真正缺的能力是什么？ |
| local_search | 仓库哪里已有类似功能，为什么不能直接调用？ |
| external_candidates | 候选的仓库、版本/commit、相关接口、许可证位置和关键限制 |
| choice | reuse / adapt / build / defer |
| reason | 为什么该选择总维护成本更低？ |
| required_fit_test | 唯一或少量关键适配问题是什么？ |
| fit evidence | 实际执行结果、输入/版本、证据引用，而非一句“应该可用” |
| revisit_on | 何种版本、许可证或需求变化才重新研究？ |

通常精读一到三个相关候选就够；搜索结果很多不意味着需要都写成长报告。如果没有找到可用候选，应诚实记录搜索范围，不能把“没有搜到”说成“GitHub 不存在”。

### 9.3 准入组合规则

| 选择/状态 | 准入条件 |
|---|---|
| not_required | 任务确实不新增需研究的组件；记录简短理由或项目规则依据 |
| exempt | 由允许的项目策略/操作者批准，不能由 worker 随意豁免 |
| existing_decision | 引用可读取、与当前需求和版本匹配；需要的证据仍有效 |
| reuse/adapt，要求 fit test | 必须有通过证据；pending 或 failed 均不得进入实现 |
| reuse/adapt，无需 fit test | 明确证明属于已有适配范围，并给出 not_required 理由 |
| build | 已对本地/外部候选作取舍，说明最小自研边界；不能只写“为了灵活性” |
| defer | 不进入该组件的实现；允许返回已记录的延期决定 |

第一步只补 pending/failed/adapt 等明确漏洞。决策引用的完整证据管理随后按真实需求增加，不为了字段齐全创建大型研究数据库。

失败的 fit test 可以支持改选其他组件或 build，但不能同时保持 `choice=reuse` 并假装测试通过。

### 9.4 防止研究与开发脱节

新增依赖/组件的 TaskSpec 必须引用已选决定；候选产物若与决定不一致，作为范围/计划变更处理。审查只检查与本任务相关的决定，不再重复整份调研。

每个决定包含一个可验证结果，例如“实际 acpx 配置能解析”“真实客户端能与 mock 完成一轮”“该 SDK 能传回本契约所需事件”，而不只是“项目 stars 多、架构不错”。

### 9.5 本轮实际复用清单

```text
直接继续使用：HFlow 现有 Store / contracts / review / workspace / cleanup
直接继续使用：已验证 acpx → DSH ACP
直接继续使用：已有真实客户端 mock 与录制事件回放
选择性借鉴：AO/CAO 的工作区、生命周期、任务状态展示方式
选择性借鉴：Superpowers 的小任务/TDD/YAGNI，而非整套强制仪式
备用方案：官方 ACP Python SDK，仅在现有传输出现明确障碍时评估
本轮不引入：Beads/Dolt、完整 AO/CAO 平台、另一个 native coding Harness
本轮不新增依赖：Vibe Kanban；保留其工作区/审查交互的参考价值
```

这份清单就是“先复用”的结果：**保留已经适合的轮子，比找到更多新轮子更重要。**

---

## 十、团队与原生子代理：保留目标，但按收益分期开通

### 10.1 两级自治仍可实现，不必现在全开

推荐目标拓扑：

```text
User / optional planner-lead
            │ proposes tasks and dependencies
            ▼
HFlow deterministic admission + budget + scheduler
            │
    ┌───────┴────────┐
    ▼                ▼
Worker A          Worker B          ← 独立任务 / 独立 worktree
    │                │
    └── optional native subagents   ← Harness 内部，能力通过才启用
            │
     frozen candidates
            ▼
independent review + serialized integration
```

自治表示：在已批准的范围、额度、能力和依赖内选择执行方式；不表示可以自行扩大权限、自行建新预算或改写验收条件。

Lead 可以是人，也可以是绑定到任意合适原生 Harness 的 planner，不要求 Codex。它按事件或明确需求被唤起，不作为昂贵的轮询器持续询问其他 Agent“做好了吗”。

### 10.2 顶层 worker 的动态分配

第一版一个 worker 即可。进入团队阶段后，初始只允许两个有清晰边界的并行任务，必要时再扩。

角色由任务标签/技能配置决定，例如前端、后端、测试、文档、调研。不要预先启动一个产品 Agent、前端 Agent、后端 Agent、测试 Agent，无论项目是否需要都让它们来回讨论。

调度依据应是显式条件：

```text
dependencies satisfied
AND approved binding supports required capabilities
AND root/project/binding budget remains
AND concurrency allowance remains
AND write scopes / exclusive resources do not conflict
```

这部分用规则和数据库完成，无需 LLM 路由每个状态变化。

### 10.3 DAG 的最小实现

当前 TaskSpec 已有 dependencies 字段；存在字段不代表当前完整 DAG 调度已实现。[H07]

未来需要时，先实现：依赖 ID 校验、无环检查、ready task 选择、任务 revision 绑定、scope/resource 冲突检测。单机 SQLite 足够作为起点，不先部署分布式队列。

任务依赖必须说明基于什么输入，而不能只说“另一个任务 accepted”。若 B 依赖 A 的代码：应让 B 基于包含 A 候选的明确集成 commit 开始，或者明确定义候选组合方式；A、B 同从旧 base 启动不等于 B 已消费 A 的产物。

### 10.4 Integration 是明确的交付阶段

只有 `LOCAL_CANDIDATE` 的阶段，HFlow 不自动 merge/push/deploy。未来开通 integration 后，建议使用串行的专用 integration workspace：

1. 冻结目标分支当前 tip，并记录每个输入 candidate commit。
2. 逐个应用可接受候选；冲突先停止，不能未经预算启动“冲突修复 Agent”。
3. 在组合后的新 tree 上执行集成检查。单个候选通过，不自动证明组合后也通过。
4. 输出 `INTEGRATED` 的具体 commit 和来源清单；原本的 local receipts 保持历史身份。
5. 发布/推送必须有独立授权和明确目标，不从 accepted 自动推导出 publish 权限。

目标分支移动、输入候选变化或冲突导致代码修改，均会生成新的集成候选；不能把旧检查重新贴在新代码上。第一版无需支持复杂自动冲突求解。

### 10.5 Replan 只影响相关子图

如果 A 失败，先阻塞依赖 A 的任务，不重跑已完成且不依赖 A 的任务。若 A 的交付接口/候选发生变化，确定依赖其输出的节点集合，重新验证其中尚未成立的输入前提。

Replan 可以由 planner 提出，Controller 检查根预算、任务 revision 和依赖变更。已完成任务的证据留存，不通过删除重建掩盖返工成本。

### 10.6 原生子代理必须同时满足三类条件

| 条件 | 必须回答的问题 |
|---|---|
| 控制 | 能否限制子代理总数、同时活跃数、递归深度和截止时间？ |
| 观测 | 能否知道哪一个父 invocation 产生了哪些子代理；内部成本缺失时是否如实标 unknown？ |
| 生命周期 | 父任务取消/超时后，能否停止其拥有的子任务与进程，并有证据？ |

重要的当前上游限制：官方 `dsh-subagent-acp` 文档明确说明，该后端不提供可选的 start-time depth caps、tool filters、structured output 等能力，相关请求会被拒绝。[R03]

因此，不能在方案里写一个 `max_depth=1` 就宣称对这个后端完成强制控制。可以选择其他经过验证的原生后端；也可以保持禁用。**能力未知时默认禁用，不能为了完成架构图自己再造一整套模型工具循环。**

父 Agent 只得到孩子的最终回答，不意味着孩子执行免费；它仍有独立上下文、模型步骤和进程启动成本。[R03]

### 10.7 并发总量必须可解释

未来的并发限制应同时覆盖顶层和原生子任务：

```text
active top-level invocations <= approved top-level limit
active native children      <= approved native limit
combined resource use       <= machine/project approved bound
```

同时需要“总共允许启动多少个孩子”，仅限制同时活跃数量不能阻止串行创建上百个子代理。不能采用“4 个 workers，每个各自默认最多 4 个 children”，然后只按 4 统计总负担。

早期只开一层并行：顶层任务并行时，native children 保持 0；试验原生子代理时，顶层并行保持 1。确有收益后才组合。

### 10.8 第二种 Harness 的接入验收

通用性不靠配置字段里出现第二个名字来证明。

先用两个行为不同的离线 Driver 跑相同控制契约，证明 Controller 不依赖 DSH 特定事件和权限文案。真实第二 Harness 在用户确实要使用时再接入：固定版本、无模型启动/配置测试、契约 fixtures、最后一个另行授权的有用任务。

同一 TaskSpec 若请求第二 Harness 不具备的能力，应提前拒绝并说明缺口，不能降低审查或安全标准后仍称等价执行。不要为了展示“支持多 Harness”专门烧掉 Codex 周额度。

---

## 十一、分阶段实施路线与停止条件

### 11.1 不重做 M0/M1/M2

当前历史编号保持原含义；下面使用 S0–S5 标记本方案增量阶段，避免再次把已完成工作命名为“待从零完成的 M0”。

| 阶段 | 目标 | 必须交付 | 本阶段额外 HFlow live 上限 | 不做什么 |
|---|---|---|---|---|
| S0 基线收敛 | 统一当前状态，固定已知实现与证据 | 当前能力表、历史记录标记、离线测试实际结果、明确阻塞项 | 0 | 不重跑已完成 M2、不重做传输选型 |
| S1 单任务准入/运行加固 | 消除已知的可离线暴露问题 | reuse/delivery 准入修复、必要的检查资源控制、真实客户端 mock 与结果契约通过 | 0 | 不加队列、团队、Web UI、第二 Harness |
| S2 新业务任务闭环 | 在实际目标项目交付一个新候选 | 一次受监督的新任务，正式检查、独立审查、Controller receipt | 至多 2 个顶层 invocation，另行授权 | 不购买旧候选、不自动补跑、不把失败额度返还 |
| S3 有界修复与简单接入 | 只有实际被业务修复需求推动时实施 | 根预算、至多一次修复、精确失败路由、binding loader 最小实用接入 | 每任务另批；标准一修复闭环最多 4 个顶层 invocation | 不为可读性进行全仓重构 |
| S4 组合交付/两任务并行 | 解决真实项目存在的依赖或并行需求 | 串行集成、输入绑定、最小 DAG、初始并行至多 2 | 新的工作包授权 | 不默认四工种常驻，不自动解决所有冲突 |
| S5 原生子代理/第二 Harness | 需求驱动验证真正的通用性 | 可执行的能力约束、 lineage 与终止证据、通用契约测试 | 按试验目标另批 | 不为了展示而全平台全模型认证 |

这里的 live 上限只约束由 HFlow 启动的真实执行。开发阶段 DSH 本身的会话费用另计；上限不是精确 provider 请求/计费上限。

### 11.2 近期任务清单：一个任务一个可见结果

| ID | 范围 | 主要现有文件/资产 | 完成条件 |
|---|---|---|---|
| T01 | 当前基线与文档事实统一 | README、docs/architecture.md、docs/operations.md、M2 结果文档 | 当前入口不再与已记录事实冲突；历史失败/恢复、隔离限制保留 |
| T02 | 准入最小补洞 | admission.py、contracts.py、现有 admission/contract 测试 | reuse/adapt 的待测/失败组合拒绝；不支持的交付默认拒绝；表驱动回归通过 |
| T03 | 正式检查的受管生命周期 | verify.py、drivers/winjob.py 及相关已有进程边界 | 检查超时后受管后代停止；日志有上限；凭据继承最小化；候选漂移不接受 |
| T04 | 接通现有机器绑定契约 | MachineProfile/AgentBinding、CLI/selected Driver 入口 | 本机实际绑定明确；未知配置拒绝；不写全局 Harness 配置；泛化 vendor 特例不破坏历史数据 |
| T05 | 把已发生故障变成离线回归 | real_client_checks、review_wire、real_client_review、saved_review_replay 等 | 当前配置通过真实客户端；正常/异常 verdict 通过真实结果收集路径；缺组件明确 skip/block |
| T06 | 零模型输出与成本汇总 | report.py、已有 receipt/Store | 输出 requested/achieved、历史/current、原始/恢复、已知/未知用量；无需模型写报告 |
| T07 | 一个新的实际业务任务 | 目标项目 TaskSpec/ProjectConfig；现有 Driver | 另行授权不超 2 invocation，交付候选或准确 blocked；不擅自重试 |
| T08 | 可选的一次有界修复 | 根预算关联、Controller、失败路由与测试 | 只有正确类型的可修复业务失败能触发；最多一次；跨 revision 总限额成立 |

**实施依赖不是“八项全做完才许使用”。**

T01、T02、与实际执行路径相关的 T05 应先完成。T03 中与所选检查命令有关的生命周期/输出风险需要可控；当前小任务如使用已知短小无后代检查，可将通用化剩余工作记录为限制，而不是无期限阻塞。

T04 可先保留已存在且证据绑定清楚的 DSH 启动方式，直到需要多项目复用时再接入完整 loader。T06 先做短文本/JSON，不必做仪表盘。T08 明确不属于第一轮 live 的前置要求。

### 11.3 每项变更的完成定义

一次改动需交付：具体目标、范围内 diff、最小回归测试、真实运行结果、已知限制和下一动作。不要要求每个小补丁都生成新总设计、完整研究报告、长篇复盘和部署手册。

同一行为的故障能用单元/集成 mock 重现时，不申请真实模型测试。新的 Driver 参数必须从当前已安装工具的帮助/配置校验或固定版本源码取得，不能从记忆发明。

### 11.4 第一条新业务 live 任务的放行门槛

```text
[ ] 与已有候选不同，确有业务价值
[ ] 范围小，AC 可自动检查，base commit 明确
[ ] 当前 DSH/acpx 绑定与必要证据相符
[ ] 真实客户端 + mock 验证覆盖将运行的权限和 reviewer 路径
[ ] 所选正式检查已在本地验证可控
[ ] 未启用 planner/原生子代理/修复/并行/发布
[ ] 新授权明确覆盖至多 1 implementer + 1 reviewer
[ ] 原有 AUTH-m2-live-2 与历史 run 不修改
[ ] 失败保存产物、结果与原因；无自动新增授权或重试
```

新任务成功时，才新增“该绑定在该新任务上未中断完成闭环”的证据；不是给原来的 3dbfeae 运行盖上新的成功章。

### 11.5 阶段升级的退出标准

S2 成功后，先使用而非立刻扩展架构。只有实际任务暴露“无法自动完成一次合理修复”“存在清晰并行机会”“需要另一 Harness”时，才进入对应后续阶段。

失败原因如果是模型对业务需求理解不足，优先缩小任务或改善 AC/上下文，不修改进程框架；如果是 Driver 配置/解析问题，优先离线测试，不切换成更贵模型。

---

## 十二、验收测试矩阵与成本基线

### 12.1 必要测试不是越多越好，而是覆盖真实边界

| 测试组 | 关键案例 | 预期 |
|---|---|---|
| 准入 | reuse/adapt + required fit pending/failed；失效决策引用；unsupported delivery | 在创建真实执行资源之前拒绝，原因明确 |
| 配置 | 真实客户端拒绝虚构字段/枚举；已批准权限组合合法 | 本地失败，不发送真实 prompt |
| 正常传输 | mock 经真实客户端与生产 collect 路径返回 reviewer verdict | 真正抵达接受谓词，不预填 review 绕过 Driver |
| 输出异常 | 缺失/歧义/重复 JSON key/错 session/错 prompt/非最终消息 | 不能形成 accepted；保持协议错误和业务拒绝的区别 |
| 取消与竞态 | cancel 先于最终结果；旧 attempt 迟到；重复 collect | 取消/新 revision 优先，旧结果不能覆盖 |
| 崩溃恢复 | reserve 后崩溃；prompt 后断开；结果已录制未处理 | 不自动派发；可重放则离线恢复，不确定则 blocked |
| 验证缓存 | 树、检查、依赖、关键环境改变；非确定检查 | 失效重验或禁止缓存，不能继承无效 pass |
| 验证资源 | 多输出、超时、持有 helper 的子进程 | 输出有界；停止身份明确；不残留受管工作 |
| 范围/工作区 | 越界、链接/路径逃逸、候选漂移、dirty 用户 checkout | 不接受、不破坏用户工作区，不靠 prompt 自证安全 |
| 清理 | 主工作区、未停止进程、未冻结改动、未知 ignored 文件 | 拒绝危险清理；成功清理仍保留 candidate refs 与证据 |
| 计数 | 重复任务、历史 receipt、离线 replay/report | 不新增模型派发；历史消费不返还、不混成当前验证 |
| 通用契约 | 第二个离线 Driver 的不同能力和错误形式 | Controller 行为一致，缺少能力提前拒绝 |

使用已有 fixtures 和真实客户端 mock 为主，不重复造另一套 Fake 世界。一个只验证 FakeDriver 自己返回 accepted 的测试，不足以验证生产 Driver 已把 reviewer 的消息接到了 Controller。

### 12.2 CI 的合理层级

拟议 CI 以无模型为默认：schema/静态检查、单元测试、mock 生命周期、录制事件回放。Windows 相关进程边界需 Windows runner；其他平台不要把 skip 当 PASS。

真实客户端 mock 需要固定版本客户端存在。缺少它时允许清楚地 skip 或 block 相应验证，不能下载 latest 之后声称重现了用户本机绑定。CI 不需要 DeepSeek/Codex 凭据，也不应自动进入 live 阶段。

录制 fixture 进入公开仓库前必须完成脱敏与来源说明。无法安全公开的记录保持本地；CI 使用可公开的等价构造案例，不能把私有日志默认为可上传材料。

### 12.3 应记录的最小指标

| 指标 | 定义/意义 | 限制 |
|---|---|---|
| 可用交付数 | 达到要求交付层级、证据有效的业务任务数 | 本地候选与发布分开，不能混算 |
| 无额外修复交付率 | 第一次批准执行就完成要求的任务占比 | 标明样本数；离线恢复另列 |
| 顶层调用负担 | 每个可用交付所消费的 implementer/reviewer/planner 等 invocation | 不是实际 provider 请求数 |
| 工作流阻塞率 | 因配置/传输/控制器/证据接线而 blocked 的业务任务占比 | 与业务代码缺陷分开 |
| 重复派发 | 对相同逻辑任务非批准的新派发数 | 目标 0 |
| 人工介入 | 每个任务必须人工恢复/重配/搬运材料的次数 | 初期人工输入 TaskSpec 不应偷偷排除 |
| 验证复用 | 有效缓存命中数与避免的检查时间 | 不把漏测算节省 |
| 维护占比 | 同一观测口径下 maintenance / 全部投入 | 成本不可观测时用时间等代理指标并注明 |
| 用量可见性 | 多少执行可得到真实 billed usage | 不能用 null 作为 0 降低总额 |

关键比较是“单位可用交付的总负担”，不是工作流在空跑状态下用了多少 token。

### 12.4 与直接 DSH 的比较方法

先整理可用历史记录，不为评测重复购买已经交付的任务。随后在同类型、相近规模、相同验收强度的自然业务任务中记录直接 DSH 与 HFlow 的表现。样本较少时只作探索性比较，不宣称因果证明。

建议先积累约 5–10 个可比任务，记录任务难度、改动范围、验证成本、调用负担和人工介入，展示原始样本和中位数。如此小的样本不应过度解读 p95 或精确百分比。

可以把“非业务维护负担低于约 20%”“重复派发为 0”“不用额外模型写报告”作为起始目标。任何“节省 30%/50%/90%”必须来自实测口径，而不是把总调用数除以原来的 Codex 周额度。

如果 HFlow 相比直接 DSH 没有带来可重复的证据、恢复或人工负担收益，先减少可选步骤，不继续堆 Agent。

---

## 十三、日常操作、升级与旧仓库退出

### 13.1 当前已存在的操作入口

以下命令取自仓库当前文档，使用时仍需以目标环境的 `--help` 和当前源码为准。[H01/H09]

```sh
# 无模型查询与契约
hflow doctor --json
hflow schema
hflow status <run-id>
hflow report <run-id> --json

# 已发生工作的信息协调，不是重试
hflow resume <run-id>
hflow cancel <run-id>

# 先预览，确认后再释放已受管工作区
hflow clean <run-id>
hflow clean <run-id> --apply

# 既有离线示范与测试入口
python examples/m2_cli_demo.py
python -m pytest -q
python tools/m0_probe/real_client_checks.py all
```

真实运行需要当前 build 对应的授权文件和 Driver 参数。本方案不发明一条可以绕过这些要求的“万能 live 启动命令”。新 live task 包应从现有 CLI/授权契约生成，并经当前版本 help/schema/mock 核对。

`hflow bind`、`hflow metrics`、自动 DAG/repair 等命令在本方案中都不是现成能力。未来可先以现有命令参数或小工具实现，不需要为了界面整齐扩充 CLI。

当前 README 记载的退出码为 `0=accepted`、`2=admission refused`、`3=blocked after dispatch`、`4=usage error`。[H01] 建议保留这些既有类别，并在 JSON 中提供具体 reason、requested/achieved delivery、是否派发与恢复来源。自动化调用者不能只判断进程是否退出，更不能把单个 Harness 的 exit code 当成 HFlow 的最终业务验收。

### 13.2 阻塞处理手册

```text
看 status/report 中的事实和失败类型
        ↓
原始 invocation / candidate / checks 是否齐全？
        ↓
能否使用已有离线 replay 或 mock 重现？
        ├─ 是：修本地接线/契约，重放已有证据，记录后来的决定
        └─ 否：保留 unknown/blocked，报告缺失事实
        ↓
确需新的业务执行？由操作者另行授权，在原工作包总预算内进行
```

不做：删除 run 目录让计数归零、手改 SQLite 为 accepted、让实现者补写 reviewer verdict、重新跑所有已通过检查来制造新时间戳、把未完成结果包装为成功。

### 13.3 升级策略

acpx 和 DSH 均为快速演进项目，默认不在每次任务启动时执行 latest 升级。[R01/R04]

记录已验证的依赖组合。升级应是独立的小任务：明确为什么升级 → 固定目标版本 → 查破坏性变更与相关代码 → 本地配置/协议 mock/已知失败回归 → 比较能力记录 → 必要且批准后用新业务任务验证。

只修改纯文档不会自动推翻进程强停证据；启动器、客户端、Harness、权限配置或进程管理发生变化则需检查影响。证据复用应依据影响分析和精确绑定，不是一概重测，也不是永远不测。

失败时回到已知绑定，但不删除失败记录、不回退任务消费。禁止同时进行“修业务 bug + 升级 DSH + 换 transport + 装一堆插件”。

### 13.4 旧仓库怎样退出

`dual-agent-workflow` 本轮只读。保留历史和必要的迁移说明，不要求现在 archive、delete、卸载或清空任何全局目录。

如果将来确需保留旧内容，只允许按价值单独挑选：一个已验证的检查命令、一条安全规则、一个短角色说明、一个研究决定。每个迁移有明确用途和测试，不整包搬运 `install.ps1`、全局配置树与旧 Team 协议。[H10]

HFlow 不依赖旧仓库存在才能启动。新项目接入只需要明确的项目契约与本机绑定，不需要先修改所有 Codex/Claude/DSH 的全局 AGENTS 文件。

### 13.5 数据保留与清理

继续使用已有 guarded clean：先 preview，再明确 apply，重新核实身份和当前状态；不使用 `--force` 扫掉异常工作区。[H09]

候选 commit 必须有稳定 ref 或其他保留机制，不把“worktree 目录还在”作为唯一备份。receipt、证据和 provenance 与工作区释放分开。

日志设上限和保留期限，但不自动删除尚未解决的 unknown/blocked 关键证据。旧记录归档可以是确定性的维护操作，不需要模型总结每条日志。

---

## 十四、给 DSH 的下一步任务指令

不要把本报告整份交给 DSH 并要求“一次全部实现”。先做下面这一个**小范围离线任务**。它是建议的任务描述，不是已授予新模型 worker 调用的授权。

```text
你现在只在 Dean20030514/HFlow 工作，不修复、不重构、不迁移 dual-agent-workflow。

调查基线是 9483a84b03332fd4009697ee6d9f3147bcebd878。
先读取当前 HEAD、工作树状态、AGENTS.md，以及 admission.py、contracts.py 和相关测试。
若 HEAD 有变化，说明相关文件差异；不要 checkout/reset/clean，不覆盖用户未提交修改。

本次唯一目标：补齐复用适配结果与不支持交付模式的准入边界。

先以现有契约为准写表驱动回归，覆盖：
1. reuse/adapt 且要求 fit test 时，pending 或 failed 都不能派发；
2. not_required、existing_decision 等已有合法用法不被无关破坏；
3. 当前未实现 integrated/published 时，在真正执行前明确拒绝，
   不仅 warning 后把 LOCAL_CANDIDATE 作为原要求已完成；
4. 拒绝路径不创建真实 invocation、不消耗 live 授权。

再做最小实现，复用现有 ValidationReport、RefusalCode 和 schema。
本任务不增加自动降级功能、不建设复用数据库、不实现 integration。
如果新的准入规则需要变更历史测试，保留原行为的解释，不能删除测试让结果好看。
如果 fixture/schema 的真实含义与任务描述冲突，先明确冲突和最小兼容方案，不发明字段。

限制：
- 不升级/切换 acpx、DSH 或 Python 依赖；
- 不修改任何全局 HOME/profile/插件配置；
- 不启动真实 HFlow worker、planner、reviewer、子代理或 Codex；
- 不修改旧 authorization，不重跑旧 M2，不重新执行旧候选的 finalize；
- 不实现 repair/team/DAG/新 Harness，不调整当前运行的 Controller 为自己授权。
- 不 commit/push/deploy，除非用户另行明确授权。

本次“无新增 live worker”不代表你当前 DSH 编码会话没有成本。

交付：
- 变更文件与最小 diff 摘要；
- 实际执行过的测试命令、退出码、passed/failed/skipped；
- 尚未执行/不能执行的验证及原因；
- 一段不超过约 15 行的结果说明。
不要生成新总方案，不重复整份 GitHub 调研，不请求真实模型替代离线验证。
```

该任务完成后再处理 T01 中的当前状态文档收敛或 T03/T05 的实际阻塞；不因读到后续阶段而自行扩展本任务。

---

## 十五、来源与可复查范围

### 15.1 读取范围说明

HFlow 主要依据固定 tip 下的相关契约、准入、验证实现和验收材料。`controller.py`、`store.py`、整个 Driver 等没有在本次被逐行完整复核；关于它们的完整性质，本文依据已读接口和仓库自己的结果报告，并明确提出后续测试，不将其视为本次安全认证。

部分文件分段读取：`contracts.py` 覆盖约第 1–620 行，`verify.py` 约第 1–220 行，operations 约前 200 行；README/验收说明读取相关正文。外部项目主要读取 README 和直接相关协议文档，不对它们的全库质量、许可证适配或本机可用性作未执行的担保。

下列地址为来源索引，不是安装脚本。外部 main/master 地址可能变动；真正实施依赖时必须固定具体版本/commit，并重新核实对应 LICENSE。

### 15.2 用户仓库来源

```text
[H01] HFlow README — 当前入口、能力边界、测试/命令与明确未实现内容
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/README.md

[H02] M2 controlled live acceptance result — 强停、启动配置故障、原始 B 结果与 trusted-local 边界
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/docs/m2-live-acceptance-result.md

[H03] Reviewer wire repair / offline replay / local finalization — 后来的接受决策与原始失败并存
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/docs/m2-review-wire-repair.md

[H04] Architecture — M1 历史结构、状态、已声明的事务与恢复性质；存在时态混写
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/docs/architecture.md

[H05] Contracts — schema、Task/Attempt/Delivery、CheckDef、ReuseDecision
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/src/hflow/contracts.py

[H06] Admission — 复用条件、预算/风险、unsupported delivery warning
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/src/hflow/admission.py

[H07] Contracts — TaskSpec/Workspace/MachineProfile/Driver/Invocation/Candidate 等读取段
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/src/hflow/contracts.py

[H08] Verify — CommandCheckRunner 与同-run evidence 复用入口
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/src/hflow/verify.py

[H09] Operations — 授权、现有 CLI、worktree 清理与真实客户端 mock
https://github.com/Dean20030514/HFlow/blob/9483a84b03332fd4009697ee6d9f3147bcebd878/docs/operations.md

[H10] dual-agent-workflow README — 旧系统全局部署面、两套落地与 Team 入口
https://github.com/Dean20030514/dual-agent-workflow/blob/main/README.md

[H11] HFlow drivers 目录 — 已有 acpx_dsh、winjob、fake 等资产
https://github.com/Dean20030514/HFlow/tree/9483a84b03332fd4009697ee6d9f3147bcebd878/src/hflow/drivers
```

### 15.3 外部 GitHub 与维护者来源

```text
[R01] deepseek-ai/deepseek-harness — 开发预览、原生 Harness、MIT 声明
https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md
读取的 blob SHA: 36adfe913902e75ce0673adc2cb5cb692151a291

[R02] 官方 DSH ACP package — 程序化接口、实际能力与未暴露的信息
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md
读取的 blob SHA: a30396733026108fdeaff67713fbe7aefb8c5101

[R03] 官方 DSH ACP subagent package — 独立进程、返回结果、start-time 能力限制
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/subagent/subagent-acp/README.md
读取的 blob SHA: 87fb7f3101d2d329300213bed2acb4a479c83b72

[R04] openclaw/acpx — 跨 ACP Agent 客户端、one-shot/session、flows、pre-1.0 提示
https://github.com/openclaw/acpx/blob/main/README.md
读取的 blob SHA: 3905e26c3a3a5f4d7db1c70d64bccf7c8077cc55

[R05] agentclientprotocol/python-sdk — 官方 Python ACP 协议与客户端部件
https://github.com/agentclientprotocol/python-sdk/blob/main/README.md
读取的 blob SHA: b8aaac8a486ac50191fcfbc38ebae2e8b42cab95

[R06] Untrivial-ai/agent-orchestrator — 原生 worker、worktree、项目协调与反馈视图
https://github.com/Untrivial-ai/agent-orchestrator/blob/main/README.md

[R07] awslabs/cli-agent-orchestrator — 保留原生 CLI 的调度；tmux/本地服务依赖
https://github.com/awslabs/cli-agent-orchestrator/blob/main/README.md
读取的 blob SHA: 0be66245f2496266c1a14cb716b9aba11f43642c

[R08] BloopAI/vibe-kanban — 工作区与 diff 审查；README 的 sunsetting 提示
https://github.com/BloopAI/vibe-kanban/blob/main/README.md
读取的 blob SHA: 32c6ed4ccd4649a9c78058a694dbbc1297c421a8

[R09] Vibe Kanban 官方公告 Goodbye bloop — 2026-04-10；公司关闭，项目转社区维护
https://www.vibekanban.com/blog/shutdown

[R10] gastownhall/beads — 当前图任务追踪/Dolt 存储与集成行为
https://github.com/gastownhall/beads/blob/main/README.md
读取的 blob SHA: 025cdc3e478bb2408dbe76d5b26a0c3112dfb33d

[R11] madebyaris/native-cli-ai — 自身是一套原生 coding Harness，而非薄控制器部件
https://github.com/madebyaris/native-cli-ai/blob/main/README.md
读取的 blob SHA: 0ff4e76e67efe2e43109119210c70e79d8603e0a

[R12] obra/superpowers — 可组合开发方法、TDD/YAGNI/小任务与逐 Harness 安装
https://github.com/obra/superpowers/blob/main/README.md
读取的 blob SHA: cf80400690849b37861f39d396d231ea89ac693b
```

### 15.4 最终决策摘要

**现在做：**保留 HFlow；修小范围离线缺口；明确当前状态；沿用已有传输和恢复；对一个新业务任务做受监督的两-invocation 闭环。

**实际需要时再做：**根预算与一次修复；机器绑定接入；串行集成；两任务并行；可控原生子代理；第二 Harness。

**现在明确不做：**旧 workflow 修复/重构、第三次从零搭框架、全局插件装修、整套重型编排平台、为了全绿重复购买已交付任务、把未知用量记为零、用模型反复整理工作流材料。

HFlow 的价值应是让同一份有效工作不再被重复购买，并让失败可定位、可保留、可恢复；不是让日常开发必须先维护一个越来越复杂的 Agent 平台。
