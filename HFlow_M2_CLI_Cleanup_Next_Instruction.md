# HFlow：接通 M2 CLI，提供保留成果的工作区清理

**起点：** 实施者报告 `d40e604`，上一轮唯一新增提交。已报告真实 acpx 零模型检查两项通过、Store 线程测试 8 项、M2 离线测试 5 项、总回归 88 passed / 1 skipped。

**证据边界：** 本指令作者没有读取该本地提交，也没有复跑这些项目测试。下文是实施边界与验收要求，不是源码审计结论。

**本轮结束条件：** 不再需要直接调用控制器的测试代码，就能从真实 CLI 完成一个 Fake M2 本地候选，并安全预览、按明确选择移除其工作区；候选和验收证据仍可恢复。

**新增 live 额度为 0。** 不读取真实凭据、不提交真实 DSH 任务、不调用 Codex/真实 Reviewer/Planner/Subagent。真实强停认证继续 not_tested，无人值守继续 disabled；旧账本与原 INCONCLUSIVE 试验不改写。

## 1. 范围：接线，不新建第二套工作流

复用现有 TaskSpec、CLI、controller、gitworkspace、Store、verify、report 与 Driver。只补 CLI 到已存在的 `workspace.mode=worktree` 路径，以及一个按 run 定位的安全清理入口。

不重开传输选型，不扩展 Driver、Job Object、线程模型或计费平台；不新增分布式锁服务、垃圾回收服务、分支管理平台、第二套配置 schema。通过的客户端启动与线程证据继续有效，只有对应代码/绑定确实改变才运行受影响的检查。

不修改旧仓库、全局 Git/DSH/acpx 配置，不 reset/stash/amend 现有历史，不自动 push/merge/release。清理测试只操作本轮测试创建的一次性 worktree，不顺手清理用户真实历史 run。

## 2. 一次核对 Git 路径解析，不能把误述固化成实现

实施回报称“`line[3:]` 少一位”。对未经修改的 porcelain v1 普通记录，这个描述不成立：格式是两个状态字符、一个空格、路径。[S1]

```python
record = b" M src/example.py"
assert record[3:] == b"src/example.py"
```

如果先去掉开头空白，状态字段就已经损坏，之后按原偏移切片自然会错。**不要据此直接把所有记录改为 `[2:]`，也不要认定当前源码一定仍有 bug。** 只读取现有生产解析函数与原失败样例：修复若已经正确，就保留实现并澄清说明。

优先固定为 `git status --porcelain=v1 -z --untracked-files=all`，使用未 trim 的原始记录。`-z` 适合机器解析，避免普通文本形式对特殊路径的引号/转义；但 rename/copy 会包含两个 NUL 分隔路径，不能当成两个独立普通记录。[S1]

复用现有路径解析测试，确保未暂存修改、已暂存修改、新文件、删除、重命名及中文/空格路径不会丢首字符。rename 的新旧路径都参加范围判定；不能把越界的旧路径漏掉。未支持的冲突/子模块等状态明确拒绝，不猜路径，不开发万能 Git 解析框架。

## 3. CLI 调用现有 M2 路径

以下是建议接口形态，不是声称当前版本已经提供这些参数。优先沿用仓内现有命令与参数名，最终 README、help 和测试必须一致，不保留两套同义接口。

```text
hflow run --task <TaskSpec.json> --driver fake
hflow status <run-id>
hflow report <run-id> --json
hflow clean <run-id>              # 默认只预览
hflow clean <run-id> --apply      # 明确请求移除这个 run 的工作区
```

### 3.1 配置和路由

`workspace.mode=worktree` 继续来自规范 TaskSpec。CLI 负责解析、确定性校验和调用现有控制器，不自行复制预算、接受判定、冻结或审查流程。

若已有 CLI 参数覆盖规范配置，覆盖必须在准入前形成唯一 effective TaskSpec，并纳入已有身份/幂等逻辑；不能落库后再隐式修改 cwd 或工作区模式。本轮不为此新增一套覆盖机制。相对路径只解析一次，记录源仓库、实际 base commit 和实际 run 工作区的关联。

`--driver fake` 明确表示测试执行，不能伪装成真实 DSH 交付；没有 DSH/acpx 安装也应能运行 Fake 示例。`--driver acpx-dsh` 接入同一个中立控制器路径，但没有相应 live 授权时，在读取凭据、启动 Harness、创建写入工作区或消耗新模型预留前返回明确阻塞；不要静默降级成 Fake。已批准的受控手动试验与无人值守权限不能混为一谈。

本轮不新增绕过授权的 `--force`、`--allow-unattended` 或万能确认参数。对真实 Driver 的未授权路径，用哨兵/替身证明没有真实启动即可。

### 3.2 工作区和候选

新 worktree 尽可能直接创建在最终的 run 路径，显式使用已有固定基线/分支策略，不依赖目录名推断。已有 rename + repair 修复若正确，不为风格重写；如确需移动，Git 已有 `worktree move`，外部移动后 `worktree repair` 的用途也有官方说明。[S2]

所有写入与正式验证都在对应 run 工作区，不回落到源仓库。继续保持：写入者退出后冻结候选；检查输入发生变化则拒绝复用证据；候选失败时保留现场，不自动修复或重派；原 TaskSpec 要求审查时不能为演示而关闭。

源仓库的用户 HEAD、index、工作文件、stash 和用户分支不被覆盖。linked worktree 本来会更新共享 Git 对象和管理元数据，因此不要宣称“原仓库 .git 完全零写入”。新增的 HFlow 专用候选引用属于受管元数据，不是用户分支。[S2]

### 3.3 可读结果

复用现有 report/receipt 字段，明确区分：

- 测试 Driver 与真实 Harness；本轮真实模型调用为 0。
- base commit、candidate commit、candidate tree 和范围内容指纹。
- 历史验收状态与当前工作区资源状态。
- 验证证据位置与候选恢复所依赖的 Git 引用。

`status/report` 不启动模型、不重新执行测试、不自动重建工作区。相同提交返回历史 run；即使该 run 工作区以后被显式清理，也不能为了返回历史结果重新派发任务。

## 4. clean 是移除工作区，不是删除交付成果

### 4.1 默认预览，按 run 精确定位

无 `--apply` 只输出计划。可支持显式 `--dry-run` 作为同义预览，但 `--dry-run` 与 `--apply` 同用要拒绝。`git worktree remove` 本身不提供这里的应用级预览，计划由 HFlow 读取现有事实生成，不要拼一个不存在的 `remove --dry-run`。[S2]

预览显示：run、规范化工作区路径、所属 Git common directory、当前 HEAD、候选引用、是否存在写入者、tracked/untracked/ignored 风险、计划保留的候选/receipt/日志、允许或拒绝的理由。预览不变更 run、不创建候选引用、不修 Git 管理文件、不删除文件。只读 Git 状态查询可用 `--no-optional-locks` 避免可选 index 刷新；复用现有只读 Store 路径，不因查询初始化数据库。[S1]

删除目标只能由已有 run 记录解析，不接受任意 `--path`、目录通配符、`--all` 或“按年龄扫目录”。不新建全机扫描器。

### 4.2 只有归属、停止和保存条件都明确才可 apply

复用现有路径与进程证据检查，至少满足：

1. 目标是这个 run 创建且仍归属同一仓库的 linked worktree，不是源仓库、其他 run、`.git`、任意用户目录或重定向后的路径。核对实际 worktree 注册与 Git common directory，不只比较字符串前缀。
2. 无活动 Worker/Reviewer/Verifier、无待执行写入操作；受管执行已确认结束。`BLOCKED`/`CANCELLED` 名称本身不是“已停止”的证明，unknown 或 still_running 不能删除。
3. HEAD 与记录的已冻结候选一致，且没有需要保留但尚未冻结的变更。dirty、HEAD 漂移、归属不明、Git lock、子模块或不支持的路径情况先拒绝，不自动修复/解锁/强制删除。
4. 交付候选有持续有效的 Git 引用，receipt 和必要证据位于待删 worktree 之外。

第一版允许范围内干净、已保存的失败候选在显式 `--apply` 后释放工作区；不自动删除失败现场。不支持“丢弃脏内容”，不新增 `--discard-changes`。未冻结改动的失败工作区就保留并解释原因。

### 4.3 SHA 是标识，不是成果保留策略

如果候选只由 detached worktree 的 HEAD 指向，把 SHA 写进 SQLite/Markdown 并不建立 Git 可达性。Git 的回收保护依据包括 refs、reflog 等；普通报告文本中的 SHA 不是永久保留引用。[S3]

优先复用项目已存在、不会随 worktree 一起删除的专用分支/引用。没有时，为当前候选建立一个最小的 HFlow 专用引用，例如：

```text
refs/hflow/candidates/<validated-run-id>/<validated-attempt-id>
```

名字来自控制器校验后的标识，不来自模型任意文本。通过 `git update-ref` 的 create/旧值校验语义创建；不存在才创建，已存在且指向预期对象则复用，冲突则拒绝，不能覆盖用户引用。[S4]

可以在冻结候选时建立，或在 `clean --apply` 删除前建立并验证；无论采用哪一种，dry-run 都不得创建引用。引用不是验收通过标记，失败候选也可被保留。

`clean` 只释放工作目录与该 worktree 的注册；不删除候选引用、候选提交、业务历史、验收凭据或日志。不要新开发自动 ref GC、打包导出系统或归档服务。不执行 `git gc/prune` 来证明安全。

### 4.4 ignored 不等于可丢弃

Git 普通 status 不展示 ignored 文件；清理前必须显式检查。[S1] `.env`、本地数据、用户笔记都可能被忽略，不能只检查“tracked 干净”。也不能把 `git clean -fdx` 当作收尾：`-x` 会纳入忽略的文件。[S5]

第一版保守策略：任何不在已有、明确可丢弃产物策略中的 untracked/ignored 文件都拒绝删除。没有产物策略时，全部拒绝即可。若已有对本 run 的验证缓存清理政策，可在预览中明确列出会随 worktree 删除的产物；不要仅因目录名叫 `__pycache__` 或 `.pytest_cache` 就默许其中任意内容可丢弃。

无须为此开发智能分类器。成功清理示例可以预先使用不在 worktree 留缓存的固定验证命令；不得为使清理通过而临时弱化验收或删除未知文件。拒绝清理带未知文件的工作区是正确结果。

### 4.5 执行、竞态与中断

实际移除调用 `git worktree remove <verified-path>`，不用 `--force`，不兜底 `rmtree`、`git clean` 或整仓 `worktree prune`。[S2]

apply 前重新检查现场；之前的预览不是永远有效的许可。通过现有 Store 的短事务/CAS 取得这个 run 的资源操作权并记录清理意图，让其他 HFlow 操作不能在删除期间重新使用它。不要持数据库锁跨越 Git 进程等待，不另建锁服务，也不把 Git 的 `worktree lock` 错当应用互斥锁。

Git 文件操作与 SQLite 更新不是一个原子事务。操作后分别确认路径与对应注册项的结果，再记录工作区资源状态；业务 ACCEPTED/FAILED 和原 receipt 不改写。

如果移除失败或只完成一部分，保留具体错误和现场事实，不声称已清理、不强行修复。若应用在文件移除后、状态更新前中断，后续只对账这个 run：结合已有清理意图、候选引用、路径与注册项确认是否已移除；不重派、不扫描/删除其他目录。

重复清理一个已确认移除的工作区应返回幂等结果。路径无故消失且没有相应清理证据时，报告 missing/unknown，不伪造“已清理成功”。对于不能确认的资源状态，保守阻塞。

本轮只承诺可信本地使用下对受管操作的保护，不声称能抵御同用户权限的恶意外部进程并发改文件。

## 5. 验收通过真实 CLI，不只直接调用 controller

复用现有 M2 样例和临时 Git fixture。从项目真实可执行入口（已有 `hflow` 或 `python -m ...`）发起测试。不要为测试单独写一个走得通的旁路 CLI。

| 测试组 | 必要证据 |
|---|---|
| 成功 M2 | CLI Fake 跑出小变更、真实候选 commit/tree 和验证凭据；原 base 的必要检查失败；candidate 通过；源用户 HEAD/index/工作文件/stash 不被覆盖 |
| 失败与幂等 | 错误候选不接受且保留；相同 TaskSpec 不多派发、不多建 worktree；清理后重复查询/提交不重建历史工作区 |
| 路由与授权 | Fake 不依赖 DSH；未授权 acpx-dsh 在真实启动和读取凭据前拒绝，不暗中降级；status/report/clean 无模型 |
| Git 解析 | 原始 porcelain 状态、rename 双路径、删除及中文/空格路径正确；覆盖已有失败样例，不只测固定字符串 |
| 预览和正常清理 | 默认 clean 没有应用/Git 内容变更；明确 apply 只移除目标临时 worktree；候选专用引用仍可解析、提交内容可读、receipt 仍可报告 |
| 危险清理拒绝 | 活动/未知执行、dirty、HEAD 漂移、陌生 untracked/ignored（至少一个 `.env` 样例）、错误归属/路径、Git 拒绝都不强删 |
| 重复和中断 | 重复 apply 不误删别的目录；预览后发生变化则拒绝；Git 失败/结果落库中断后保守对账，不把资源清理改成业务状态变化 |

已经覆盖的行为复用测试和证据，不为每一行建立一套框架；用 Event/Barrier 或受控故障注入，不做无限压力测试。收口时运行一次必要离线回归与一个 CLI 示例。输入未变不重复完整回归，也不为消除 junction skip 改系统权限。

允许正常清理测试移除其自行创建的临时工作区；本文件不授权删除用户既有真实成果、其他历史 run 或原始日志。

## 6. 交付与后续

修改范围以 CLI、workspace 清理的最小共用实现、必要 Store/报告字段接线和端到端测试为主。只向已有 README/operations 增加可运行命令和清理保留规则，不复制总方案到 AGENTS、不新增长研究报告。

建议提交：

```text
feat(cli): expose worktree runs and guarded cleanup
```

本轮回报只需：实际 SHA、可复制的 CLI 示例、成功与失败候选结果、清理预览/拒绝/应用/恢复引用证据、必要回归结果、新增 live 0。测试总数不是目标。

完成后称为“CLI 可操作的 M2 离线交付闭环”，不能宣称已完成真实 DSH 业务交付或无人值守生产认证。下一项真实工作使用现成流程与明确新增授权，不再重开一轮传输探索。本轮不运行真实强停补测，也不隐含批准 M2 业务 live。

若 clean 的安全前置不满足，清楚拒绝并保留现场；不要为追求“所有工作区都能删除”扩张成危险清理平台。CLI run 的既有可用成果应独立保留。

## 核对来源

这些来源用于界定 Git 的实际语义，不代表本机代码已经满足要求。

- [S1] Git status：porcelain v1/v2、NUL 分隔、重命名、ignored 与 optional locks：https://git-scm.com/docs/git-status
- [S2] Git worktree：add/move/repair/remove、管理元数据与 worktree refs：https://git-scm.com/docs/git-worktree
- [S3] Git gc：对象可达性与 refs/reflog 的保留作用：https://git-scm.com/docs/git-gc
- [S4] Git update-ref：create、旧值检查与事务：https://git-scm.com/docs/git-update-ref
- [S5] Git clean：`-x` 包括忽略文件：https://git-scm.com/docs/git-clean
- 既有《HFlow_Post_Launch_Failure_Next_Instruction.md》第 5 节：M2 离线交付边界；第 6 节：真实试验需另行明确授权。

**本轮成功标准：通过真实 CLI 跑出并查看一个离线候选，能安全释放其工作目录而不丢掉成果；危险或不确定情况明确拒绝，且不再为底层验证消耗模型。**
