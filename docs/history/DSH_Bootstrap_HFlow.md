# 给 DSH 的首次开发指令：HFlow 绿地重写

本文件可直接作为新仓库中首次开发的任务说明。完整设计见同目录的 `Harness_Agnostic_Workflow_Greenfield_Plan_2026-09-19.md`。**本次只完成离线最小纵切面，不一次性实现完整路线图。**

## 目标与背景

我要从零创建 Harness-agnostic agentic development workflow，暂名 HFlow。旧项目 `Dean20030514/dual-agent-workflow` 维护成本过高，这次不修、不重构、不迁移旧运行器。

新系统由小型确定性控制器负责准入、预算、证据与交付；原生 Harness 负责推理和工具。DSH 是首个真实执行环境，但角色不能绑定某个模型、厂商或 Harness。当前不调用 Codex，也不额外启动真实付费 Agent 进行开发验证。

## 首先阅读

阅读完整方案第 1、4、5、7、18、19 节。其他章节只在当前实现确实涉及其语义时查阅，不把整篇方案复制进 AGENTS.md 或每个 prompt。

关注已经调查过的一手来源：

- DSH 原生 ACP：`deepseek-ai/deepseek-harness` 的 `packages/acp/acp/README.md`。
- DSH 原生 headless：同仓库的 `packages/bundle/headless/README.md`。
- acpx：`openclaw/acpx` 的 `docs/custom-agents.md`、`docs/CLI.md`。

快照 URL 和 SHA 在完整方案第 24 节。不要假定当前本机版本与文档快照一致。

## 第一项工作：先检查，不修改本机全局环境

确认当前目录是新项目目录；如果在旧仓库里，不修改旧仓库，而是报告应使用的新工作目录。

检查本机已有 Python、Git、DSH，以及 acpx 是否存在。只运行必要的版本/帮助/本地配置检查；不展示秘密，不读取或复制整个 home，不自动安装/升级插件，不修改用户登录态和全局 Harness 配置。

写一条短 ADR：优先验证 acpx → 官方 DSH ACP；若必要契约不满足，首版改选官方 headless，不能同时开发两套生产 Driver。**本轮不执行 live 模型试验，尚未测试的能力标为 unknown，不虚构兼容性结论。**

## 本轮实现范围

用 Python 完成下面一个离线闭环：

```text
读取 TaskSpec
→ 确定性校验
→ SQLite 创建/复用运行
→ 事务性预算预留
→ Fake Driver 执行
→ 模拟验收证据
→ 控制器写出 ResultReceipt
→ status/report 读取结果
```

先使用标准库实现系统调用和 SQLite；数据模型按完整方案选择 Pydantic，依赖需明确锁定。只创建当前闭环真正需要的文件，不创建所有未来模块的空壳。

数据契约由一个地方定义，禁止手写三份不同格式 schema。ResultReceipt 由控制器生成，Fake Agent 不能直接把任务状态写成 ACCEPTED。

首版 `status`、`report` 不调用任何模型。运行数据与测试临时目录不要提交到版本库。

## 必须先有的自动测试

测试正常完成、相同提交不重复派发、预算不足时不启动 Driver、进程成功但验收失败不得接受、迟到结果不能覆盖新 attempt、结果未知时不得自动重跑，以及状态与预算同事务保存。

所有测试使用 fake/mock。真实 Harness 接入、真实工作区写入、强沙箱、跨进程取消和真实计费观测暂不宣称完成。若本轮未能实现某项测试，明确指出失败和剩余代码，不修改验收标准来制造通过。

## 开发纪律

先查现有库和项目内实现，避免重造协议、数据库、通用调度框架。找到现成能力后，记录一句使用理由即可，不额外生成长研究报告。

不要安装团队插件、开发 Web UI、向量记忆、插件市场、分布式队列、通用 DAG DSL 或全局部署器。不要额外创建 Reviewer/Planner/Subagent 团队来编写这套单 Worker 基础设施。

工作流/环境错误只做一次有明确假设的定位；若继续处理会偏离本轮最小范围，保留结果并报告阻塞，不递归修整个开发环境。

不要为了结果 JSON 格式问题重做已经完成的编码。不要重跑完全相同且输入未变化的检查来凑材料。

## 本轮交付

交付实际可执行代码和测试，而不只是又一份计划。结束时给出一段简短总结：本轮实现、测试命令与真实结果、尚未覆盖的能力、下一项最小任务。

不要声称已完成 Harness-agnostic 认证、已验证 DSH/acpx 互操作、已实现强只读沙箱或已证明节省 Token。当前交付仅是可验证的离线控制器纵切面。
