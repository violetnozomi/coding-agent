# NZ-Coder 终端产品真实测试问题清单

本文档只记录真实终端交互中复现的问题，不以单元测试通过代替产品验收。后续每轮 Linux、Windows 或远程终端实测均追加到这里；问题修复后保留原记录，并补充修复版本、验证证据和状态。

## 状态与优先级

- 状态：`open`、`fixing`、`verify`、`closed`、`wontfix`
- P0：核心任务无法完成、数据或权限边界错误、崩溃
- P1：显著影响日常使用、效率或结果可信度
- P2：可用但体验明显不成熟
- P3：轻微显示或便利性问题

## 2026-08-14 Linux PTY 实测

### 测试环境

- 系统：Linux，真实 PTY，80 列终端
- 安装入口：`/home/pyh/.local/bin/nz-coder`，版本 `0.1.0`
- 模型：`openai-compatible/deepseek-v4-flash`
- 主测试目录：`/home/pyh/test_nzcoder`
- 隔离编辑副本：`/home/pyh/test_nzcoder/.product-test-cwbEAh`
- 主目录只读请求 Session：`session-20260814_144346-d34519a4`
- 交互与取消测试 Session：`session-20260814_144826-c96015eb`
- 编辑测试 Session：`session-20260814_144950-ad401b3f`
- 编辑测试 Run：`20260814_144950_39e0223c`

### 已确认可用

- 全屏 TUI、输入 composer、状态栏和等待动画可以正常工作。
- 输入 `/` 会显示 slash completion。
- `Ctrl+K` 命令面板支持搜索并执行命令。
- `/model`、`/mode`、`/session` picker 可以打开、筛选、取消和选择。
- `/permission`、`/status`、`/memory`、`/diff` 可以输出结果。
- 权限弹窗支持单次允许、持续允许、拒绝。
- 只读 Agent 请求完成后能恢复 `IDLE`，trace 正常落盘。
- `apply_patch` 能修改多个文件；`/undo` 能同时撤销文件和相关会话消息；`/redo` 能恢复。
- 快速连按两次 `Ctrl+C` 可以退出并显示 `Goodbye!`，没有 Python traceback。

## Open issues

### TP-001：通用 glob 会扫描产品私有状态目录

- 优先级：P1
- 状态：closed
- 复现：在 `/home/pyh/test_nzcoder` 请求“只读检查当前目录，告诉我有哪些子项目”。
- 证据：`glob_search({"pattern": "*", "path": "."})` 返回 `.nz-coder/sessions`、索引数据库、memory、worktree、trace 和 `.ruff_cache` 等内容，单次输出约 6.5 KB。
- 影响：污染代码检索结果、浪费上下文、可能把会话私有数据提供给模型。
- 预期：repo intelligence 和通用搜索默认忽略 `.nz-coder/`、`.git/`、缓存、构建产物；只有用户显式请求时才读取产品私有目录。
- 验收：相同请求只返回三个业务子项目，trace 中不存在对 `.nz-coder` 内容的模型可见搜索结果。

### TP-002：简单目录理解任务工具调用和 token 消耗过高

- 优先级：P1
- 状态：verify
- 复现：同 TP-001。
- 证据：4 个 turn、7 次工具调用、8.3 秒；除目录读取外，还调用 project profile、glob，并并行读取三个 README。trace 的累计 `total_tokens` 指标达到约 16.3k。
- 影响：简单问题响应慢、API 成本高，复杂任务更容易提前耗尽上下文。
- 预期：先用一次受过滤的目录读取建立候选项目；仅在需要描述项目内容时读取少量 manifest/README，并限制总工具输出。
- 验收：同类任务不超过 2 个 turn、4 次工具调用，且不扫描私有状态目录。

### TP-003：运行中 Ctrl+C 取消不够及时且缺少明确结果反馈

- 优先级：P1
- 状态：closed（A397 真实 80×24 PTY + trace 终态复测）
- 复现：发送“深入只读审查 cron_engine/parser.py 和相关测试”，进入 `RUNNING` 后按一次 `Ctrl+C`。
- 证据：按键后 Agent 仍继续完成 glob、目录读取和多次 read_file，约 4 秒后回到 `IDLE`；界面没有稳定显示“取消已请求”“已取消”或“自然完成”。
- 影响：用户无法判断取消是否生效；长命令、网络请求或错误循环可能继续消耗时间和费用。
- 预期：首次 Ctrl+C 立即设置 cancellation token、停止调度新工具，并在当前不可中断边界结束后显示明确的 cancelled 结果。
- 验收：取消后 trace 为 `cancelled`，不再启动新的工具调用，TUI 显示取消状态和耗时。
- A397 验证：真实 DeepSeek TUI 在首个Provider请求中按Ctrl+C，0.204秒回到IDLE；界面先显示
  `CANCELLING · waiting for safe boundary`，终态notice在80×24 idle屏保留，PTY resize完整重绘后仍可见
  `Run cancelled`。Session `session-20260827_120940-4707f0df`仅有1次303.405ms的cancelled Provider调用，
  0 tools、0 edits、唯一`run_end(status=cancelled)`，终态后0事件；transcript assistant为
  `finish=cancelled`且step-finish reason为`cancelled`。进程正常exit 0，无traceback。

### TP-004：Agent 会为无关目的主动访问 workspace 外目录

- 优先级：P0
- 状态：closed
- 复现：在隔离副本中请求实现 `@daily` cron 别名并补测试。
- 证据：Agent 主动请求执行 `ls -la /home/pyh/test_nzcoder/cron_engine && diff -rq /home/pyh/test_nzcoder/cron_engine /home/pyh/test_nzcoder/.product-test-cwbEAh`。任务没有要求比较副本，也不需要访问 workspace 外路径。
- 影响：违反最小权限和 workspace 隔离预期；在真实项目中可能读取不相关或敏感数据。
- 预期：除非用户显式给出外部路径，Agent 的探索、验证和 Bash workdir 均限制在 workspace 内；外部路径即使可读也不应由模型自行扩展范围。
- 验收：相同任务不产生 workspace 外路径；trace admission/permission 层能拒绝并给 Agent 可恢复的诊断。

### TP-005：权限拒绝后整轮任务直接结束，不会降级继续

- 优先级：P1
- 状态：closed
- 复现：在 TP-004 权限弹窗选择拒绝。
- 证据：整轮约 30.5 秒后以 `Denied by user` 结束，Run 状态为 `blocked`；没有回退到 `read_file`、`apply_patch` 或 workspace 内验证路径。
- 影响：一次无关或过宽命令被拒绝就丢失整轮工作，用户必须重新提示。
- 预期：权限拒绝作为一个可恢复的 tool result 注入；Agent 应缩小范围或选择等价安全工具，除非被拒操作是完成任务的必要条件。
- 验收：拒绝非必要命令后，Agent 无需用户追加指令即可完成原任务，最终说明被拒操作未执行。

### TP-006：acceptEdits 模式下正常验证命令仍频繁打断

- 优先级：P1
- 状态：closed
- 复现：切换到 `acceptEdits`，要求修改代码并运行指定 pytest。
- 证据：文件编辑无需确认，但指定的 `python -m pytest tests/test_parser.py ...`、后续只读诊断 Bash 都分别触发权限弹窗。
- 影响：完整 coding loop 需要用户长时间盯守；“允许编辑”模式无法顺畅完成编辑—验证闭环。
- 预期：明确区分安全、workspace 内、无网络的测试/检查命令与高风险 Bash；或者在模式说明中清晰表达并提供可持久化的精确规则。
- 验收：用户允许一次 pytest 前缀后，同一会话内等价的限定测试不重复询问；危险参数仍需确认。

### TP-007：验证失败后的诊断策略过重、收口过慢

- 优先级：P1
- 状态：verify
- 复现：隔离副本因目录布局导致 `ModuleNotFoundError: cron_engine`。
- 证据：首次 pytest 失败后，Agent 运行组合命令读取 `.pytest_cache`、执行 import、`pip show`，随后又请求读取 profile、README、Git 和 pytest 版本；整轮超过 112 秒仍未形成清晰结论，最终因拒绝后 blocked。
- 影响：环境类错误会触发长时间试探和多次权限弹窗，掩盖真正的代码修改结果。
- 预期：先分类为环境/包布局错误，执行一个最小诊断；如果验证环境与项目结构不匹配，应快速报告 blocker，同时保留修改与未验证状态。
- 验收：相同 import blocker 在一次诊断后 30 秒内结束，最终结果明确区分“代码修改完成”和“环境验证失败”。

### TP-008：80 列终端下 picker 和表格信息截断严重

- 优先级：P2
- 状态：closed
- 复现：80 列终端打开 Ctrl+K、`/session`、`/sessions`。
- 证据：命令描述被截断；Session picker 的操作说明硬换行；模型名只显示 `deepseek-v4-fla...`；`/sessions` 多列压缩为大量 `…`，难以区分会话。
- 影响：默认小窗口下核心导航信息不可读，接近“功能存在但不好用”。
- 预期：窄屏使用单列或两行布局，优先保留名称、相对时间、消息数和标题；次要字段按需详情查看。
- 验收：80 列下命令和会话可唯一识别，帮助文字不被边框切断。

### TP-009：长命令和工具状态在窄屏下换行混乱

- 优先级：P2
- 状态：closed
- 复现：运行包含 pytest 或组合 shell 的 Bash 工具。
- 证据：状态行跨多行显示，`bash` 被拆成 `ba`/`sh`，时间和命令互相挤压；权限弹窗内长命令也被硬截断，用户难以审核完整内容。
- 影响：降低可观测性，尤其影响权限决策可信度。
- 预期：状态栏只显示短摘要，完整命令在可滚动详情中展示；权限弹窗提供安全换行和横向不可见内容提示。
- 验收：80 列下命令摘要、风险原因和允许范围均可完整理解。

### TP-010：`/help all`、`/sessions` 等大块输出会淹没后续结果

- 优先级：P2
- 状态：closed
- 复现：依次执行 `/help all`、`/sessions`、`/memory` 或 Agent 请求。
- 证据：后续页面仍残留先前 help/session 表格；运行中 tool cards 与旧内容混杂，最终答案容易离开可见区域。
- 影响：长会话中用户难以定位当前任务、最终回答和最新错误。
- 预期：全屏模式为当前 run 保留稳定区域，或提供清屏/折叠/跳到最新回答的明确快捷键。
- 验收：执行大输出命令后启动 Agent，80 列窗口仍能稳定看到当前请求、工具进度和最终回答。

### TP-011：顶层 `nz-coder --help` 信息过少

- 优先级：P3
- 状态：closed
- 复现：运行 `nz-coder --help`。
- 证据：只显示一行 Usage，缺少子命令说明、常用示例和配置入口；相比 `run --help`、`models --help` 信息明显不足。
- 影响：新用户无法从标准 CLI 帮助完成首次使用。
- 预期：列出稳定子命令、用途、快速开始和文档入口。
- 验收：用户只看 `--help` 即可知道如何交互启动、headless 运行、检查配置和选择模型。

### TP-012：默认退出手势的时间窗口不直观

- 优先级：P3
- 状态：closed
- 复现：空闲时先按一次 Ctrl+C，稍后再按一次；再快速连续按两次。
- 证据：分开发送的两次没有退出，也没有可见提示；快速连按两次才显示 `Goodbye!`。
- 影响：用户会误以为 Ctrl+C 失效。
- 预期：第一次空闲 Ctrl+C 显示“再次按 Ctrl+C 退出”及有效时间；或者支持 `/exit` 的 completion 提示更明显。
- 验收：首次按键后反馈始终可见，第二次在明确窗口内退出。

### TP-013：任务已完成且测试通过后仍长期停留在 RUNNING

- 优先级：P0
- 状态：closed
- 复现：在正确包布局的隔离 workspace 中，要求增加 `@hourly`、补最小测试并运行指定 pytest。
- 证据：Agent 正确修改 `cron_engine/parser.py` 和 `cron_engine/tests/test_parser.py`，工具结果已经明确为 `32 passed in 0.02s`；但随后继续进入 `diff_status`、`review_run_evidence` 等收口阶段，TUI 超过 100 秒仍显示 RUNNING，始终没有最终回答。外部重复执行同一测试为 `32 passed in 0.01s`。
- 影响：用户无法判断任务是否真的结束；一个十几秒可完成的小改动被产品状态机拖成数分钟，并额外消耗模型调用。
- 预期：测试成功且修改证据完整时，收口阶段应有严格的调用/时间预算；非必要 reviewer 或 diff 失败不能阻塞最终回答。
- 验收：同一用例在测试工具返回后 10 秒内输出最终总结并回到 IDLE，最终总结包含修改文件和 `32 passed`。

### TP-014：非 Git workspace 的 `diff_status` 失败会污染正常流程

- 优先级：P1
- 状态：closed
- 复现：在不含 `.git` 的有效项目目录内完成修改或执行 plan 请求。
- 证据：`diff_status` 返回 `git diff failed (returncode=129): Not a git repository`，随后又注入 `tool-failure-diagnostic`；plan 模式下 Agent 还尝试 `git -C ... status --short && git ...`，被 shell policy 拒绝后直接显示原始 `Denied`。
- 影响：产品将 Git 当成隐式前提；普通目录虽然可以编辑和测试，却在状态检查/收口阶段失败，增加无意义推理和错误噪声。
- 预期：`diff_status` 在非 Git workspace 使用 ChangeTracker/事务快照生成差异，或返回结构化的 `not_applicable`，不得作为 tool failure 触发诊断循环。
- 验收：非 Git 目录中的编辑任务能够正常形成 diff 和最终总结，不出现 Git usage 文本或失败诊断。

### TP-015：Picker 关闭时存在输入按键穿透

- 优先级：P1
- 状态：closed
- 复现：打开 `/mode` picker，快速输入筛选词 `plan` 并回车选择。
- 证据：模式确实切换为 `plan`，但同一串 `plan` 又被提交为新的用户请求，Agent 随即启动一次无意义 run。
- 影响：快速键盘操作可能意外消耗 API、触发工具调用，甚至在其他 picker 场景执行非预期命令。
- 预期：modal 接管并消费用于筛选和确认的全部键盘事件；关闭 modal 后至少等到该输入事件完成再恢复 composer。
- 验收：快速输入 `plan<Enter>` 只改变模式，conversation 中不新增 user message，也不启动 Agent。

### TP-016：Plan 模式能阻止业务写入，但结束路径不够稳定

- 优先级：P1
- 状态：closed（A397 真实 Provider + 80×24 PTY 复测）
- 复现：plan 模式要求仅规划向 `cron_engine/README.md` 添加 marker。
- 证据：业务文件中 marker 始终不存在，说明写保护有效；内部成功写入 `.nz-coder/plans/<session>.md`。但该轮调用了 `diff_status`、`write_plan`、`plan_exit` 和 `question`，长时间没有最终文本；trace 中单个 `plan_exit` 显示约 25.4 秒，取消后 timeline 标记 `(no final text)`。
- 影响：安全边界正确，但用户难以拿到可执行方案或明确的“等待回答”界面，plan 模式看起来像卡死。
- 预期：`plan_exit`/question 必须在 TUI 中显示明确交互状态；若没有必须澄清的问题，应直接返回计划摘要并 IDLE。
- 验收：该用例不改业务文件，并在 15 秒内展示计划正文或清晰的问题弹窗。
- A369 修复：`plan_exit` 不再依赖模型临时组织一个含糊的 question。Runtime 直接生成完整计划摘要和
  `Approve Plan (Recommended)`、`Implement in This Session`、`Keep Planning` 三个稳定选择；批准、当前
  Session 实施和继续规划分别落到独立状态转换。计划正文由产品边界提供给 selector detail，避免模型遗漏
  审批语义。相关 plan-mode/loop 回归通过；本轮未发起真实 Provider 请求，因此仍保留 15 秒真实时延验收。
- A397 验证：Session `session-20260827_115550-74ac999b`从`run_start`到审批结算为10.97秒，
  `plan_exit`交互本身628.803ms，满足15秒门槛。默认批准后`run_end=completed`，4次Provider调用、0编辑；
  README SHA-256仍为`9950905ee3c0931d24150018d8cb6e4dc3cd61dd7782f7ba60adfe39830fd0e0`，仅生成
  产品内部plan文件。

### TP-017：Session resume 后历史内容出现结构化数据残片

- 优先级：P2
- 状态：closed
- 复现：强制结束一次没有 final text 的 run，重新启动 TUI，通过 `/session` 恢复 19-message Session。
- 证据：picker 能正确列出并恢复目标 Session，mode 也恢复为 `plan`；但恢复后的可见历史顶部出现独立的 `}`、`]` 等结构化数据尾部，随后才显示 `Resumed session ...`。
- 影响：历史恢复的首屏观感像 JSON 泄漏或渲染损坏，用户难以信任会话状态。
- 预期：恢复时只渲染用户可见 message parts；隐藏 metadata、tool schema、内部计划结构，并把视口定位到最后一条完整消息。
- 验收：恢复同一 Session 后首屏无孤立括号/内部 JSON，只显示正常消息卡片和恢复提示。

### TP-018：Python LSP 能启动但无法解析 workspace 内的跨文件定义

- 优先级：P1
- 状态：closed
- 复现：正确可导入且 pytest 全通过的 Git workspace 中，对 `cron_engine/tests/test_parser.py` 导入和调用位置的 `parse` 执行 LSP hover/goToDefinition。
- 证据：optional LSP pack 正常加载；导入位置第 5 行第 32 列的 `goToDefinition` 返回 `No results found`。调用位置第 15 行第 13 列的 hover 仅返回 `(import) parse: Unknown`，`goToDefinition` 仍返回 `No results found`。相同 workspace 的 pytest 为 `31 passed`。
- 影响：doctor 显示 Python LSP 已安装并不代表语义导航可用；Agent 会退化为文本搜索，面向真实包布局时无法可靠跳转定义、获取类型或引用。
- 预期：LSP 初始化应识别嵌套 `pyproject.toml`/包根，必要时为文件选择最近项目根或注入 execution environment；doctor 增加一次真实 workspace probe，而不只检查 executable。
- 验收：上述两个位置均返回 `cron_engine/parser.py` 中 `parse` 的准确范围，hover 显示函数签名而非 `Unknown`。

### TP-019：窄屏 `/processes` 列表截断了后续操作必需的 process_id

- 优先级：P1
- 状态：closed
- 复现：80 列 TUI 中启动 `proc_0ebfa2a812ca`，执行 `/processes`。
- 证据：表格显示成 `proc_0ebfa2a81…`，Status、Command、CWD、PTY 等列大多只剩 `…` 或空窄列；无法从列表复制完整 ID。只有回看 Agent 最终文本后，才能手工执行 `/processes logs proc_0ebfa2a812ca` 和 kill。
- 影响：进程管理功能存在，但用户仅凭管理页面无法操作进程，特别是多个相似 ID 时。
- 预期：窄屏采用逐项卡片或两行布局，完整保留 process_id、status 和 command；次要字段放入 inspect。
- 验收：80 列下用户只看 `/processes` 输出即可复制完整 ID，并完成 logs/kill。

### TP-020：Run 总结复用了上一轮耗时/变更信息，造成状态错觉

- 优先级：P2
- 状态：closed
- 复现：先完成一次 7-tool 编辑任务，再发起只启动 persistent process 的 1-tool 请求。
- 证据：第二轮 RUNNING 区域仍长期展示上一轮 `Run completed · 7 tool(s) · 16.4s` 和 `Δ 2 changed file(s)`；当前轮结束后才在下面追加 `Run completed · 1 tool(s) · 2.8s`。后续 LSP 请求期间也持续混入已经 killed 的 process 表格和旧 run summary。
- 影响：用户容易把上一轮修改/耗时误认为当前轮状态，当前请求的工具卡和最终结果不突出。
- 预期：开始新 run 时将旧 summary 折叠为历史块；固定状态区只显示当前 run 的计数、耗时和变更。
- 验收：连续执行编辑、process、LSP 三轮时，每轮 RUNNING 区只显示本轮指标，历史可滚动查看但不会占据当前状态区。

## 2026-08-14 三批修复记录

### 第一批：P0 与运行闭环

- TP-004：Bash admission 与工具执行层都增加 workspace 路径边界；模型自行构造的绝对路径、Windows 绝对路径和逃逸 workspace 的父级相对路径会被拒绝。
- TP-005：普通权限拒绝现在作为可恢复 tool result 返回给 Agent；只有已确认的连续相同调用 doom loop 才终止运行。
- TP-013/TP-014：非 Git workspace 的 `diff_status` 改用 ChangeTracker 差异，不再返回 Git usage 错误；TP-013 仍需用真实模型复测“测试通过到最终回答”的耗时。
- TP-003：运行中首次 Ctrl+C 立即显示 `Cancellation requested`，只触发一次取消；空闲首次 Ctrl+C 显示 1 秒退出窗口。取消后的模型/工具边界耗时仍需真实长任务复测。

### 第二批：检索、验证与 LSP

- TP-001：通用 glob/grep 默认忽略 `.nz-coder`、`.nz-coder-runs`、`.git`、缓存、依赖和构建目录；显式指定私有目录时仍可访问。
- TP-002：系统提示新增轻量 repo orientation 预算：先做一次过滤后的目录读取，同类简单任务最多 2 turn、4 tools；实际 token/调用数仍需模型复测。
- TP-006：会话级“始终允许”pytest 规则收敛为 `family:pytest`，覆盖 `pytest`、`py.test`、`python -m pytest`，不会顺带放行 `python -c`。
- TP-007：pytest collection 阶段的模块导入失败会先进入 `import_or_package_layout` 分流，只允许一次 workspace 内最小探针，禁止安装包、`pip show` 和重复 profile/version 试探；实际 30 秒收口目标仍需模型复测。
- TP-018：Python LSP 会把嵌套 package marker 根提升到可解析该 package 的父目录。真实 basedpyright probe 已返回 `cron_engine/parser.py` 中 `parse` 的定义与函数签名。

### 第三批：80 列 TUI 与会话可读性

- TP-008/TP-019：窄屏 `/sessions` 与 `/processes` 改为逐项卡片，完整保留 session/process ID 和关键操作信息；80 列 PTY 已验证。
- TP-009：运行状态行按终端宽度单行裁剪，权限卡中的完整 Bash 命令改为安全换行展示。
- TP-010/TP-020：新 run 开始时清理上一轮通知和运行展示，当前状态区不再混入旧 help、旧统计或旧变更摘要。
- TP-011/TP-012：顶层 `--help` 增加首次使用、主要命令、模型与配置入口；真实 PTY 已验证首次 Ctrl+C 提示和双击退出。
- TP-015：真实 80 列 PTY 中快速输入 `plan<Enter>` 只切换 mode，没有穿透成用户请求。
- TP-017：恢复历史时不再把非文本结构化 message content 序列化成可见 JSON 残片。
- TP-016：question/plan 生命周期已有持久 UI 状态与独立卡片，且 picker 未再穿透；仍保留真实模型 plan 收口时延复测。

### 自动化与直接运行证据

- `python -m compileall -q nz_coder`：通过。
- `git diff --check`：通过。
- 全量测试：`2115 passed, 21 skipped`；聚焦回归为 `221 passed`。
- 真实非 Git workspace：`diff_status` 返回 `workspace_mode: non_git` 和 ChangeTracker 差异，不再报错。
- 真实 80 列 PTY：`/sessions` 显示完整 ID；Ctrl+C 提示、双击退出和 `/mode` picker 输入隔离通过。
- 真实 Python LSP：definition 指向 `cron_engine/parser.py`，hover 返回 `parse(expression: str) -> CronExpression`。

### 尚需真实模型复验

- TP-002：简单目录理解是否稳定控制在 2 turn、4 tools 内。
- TP-003：长模型请求/长工具边界上的取消停止时间及最终 trace 状态。
- TP-007：真实 import/package-layout blocker 是否在一次探针、30 秒内收口。
- TP-013：非 Git 编辑任务在测试通过后 10 秒内最终回答并回到 IDLE。
- TP-016：Plan 模式在无额外澄清时 15 秒内显示计划审批卡或计划摘要。

## 2026-08-14 修复后真实回归（第二轮）

### 实测轮次

| 轮次 | 场景 | 结果 | 时间 | 关键证据 |
|---|---|---:|---:|---|
| 1 | 根目录轻量项目识别 | completed | 8.0s | 4 tools；无私有目录；新增输入约 874 tokens |
| 2 | 非 Git workspace 编辑 + pytest | completed | 16.4s | 6 tools；2 files；`34 passed`；测试后约 3.1s final |
| 3 | 80 列 Plan TUI + 审批 | completed | 47.5s | 8 tools；业务文件零修改；约 14s 为人工等待审批 |
| 4 | 跨 Python/C++ 三项目长只读审查 | completed | 157.8s | 41 tools；145KB tool output；11 model calls；最终报告有代码证据 |
| 5 | 真实 TUI 长任务取消 | UI cancelled | 约 6s | 立即显示 CANCELLING，1s 内 IDLE；trace 未闭合 |
| 6 | Git workspace 多文件长编辑 + 分层验证 | completed | 164.0s | 47 tools；38 model calls；5 files；外部复验 `75 passed` |
| 7 | 重复轻量项目识别 | completed | 7.8s | 5 tools；无私有目录；新增输入 1179 tokens |

### 本轮状态调整

- TP-013 关闭：非 Git 编辑任务中，pytest 返回 `34 passed` 后约 3.1 秒生成 final，总耗时 16.4 秒；`diff_status` 未产生 Git 错误。
- TP-002 保持 `verify`：两次轻量任务分别为 4 tools 和 5 tools，延迟均约 8 秒；隐私过滤稳定，但“不超过 4 tools”尚不稳定。
- TP-003 重新打开：UI 取消反馈和停止调度已改善，但 trace/session 证据没有正确结算，见 TP-023。
- TP-016 重新打开：Plan 安全边界正确，但排除 14 秒人工审批等待后仍约 33 秒，超过 15 秒验收目标；审批内容在 80 列下也不完整，见 TP-026。

### TP-021：`doctor` 与其他入口的 workspace 参数不一致

- 优先级：P3
- 状态：closed
- 复现：执行 `nz-coder doctor --workspace /home/pyh/test_nzcoder`。
- 证据：argparse 返回 `unrecognized arguments: --workspace ...`；`nz-coder run` 则使用 `--cwd`。
- 影响：自动化安装检查必须先切换进目标目录，无法使用与 headless run 一致的显式 workspace 选择方式。
- 预期：`doctor` 支持 `--cwd`（可保留 `--workspace` alias），帮助和错误信息保持一致。
- 验收：从任意目录执行 `nz-coder doctor --cwd <workspace>`，workspace、配置和 LSP 检查均针对给定目录。
- 修复与验证：`doctor` 现同时接受 `--cwd` 与兼容别名 `--workspace`，所有检查和 JSON 输出都使用解析后的目标目录；参数专项与全仓回归通过。

### TP-022：Repo Map 与通用搜索的默认忽略规则没有统一

- 优先级：P1
- 状态：withdrawn（2026-08-28 范围审计后已回退）
- 复现：根目录执行跨三个业务子项目的只读架构审查。
- 原证据：`grep_search`/`glob_search` 与 `repo_map` 对手工测试产生的 `.product-*` 副本处理不一致。
- 范围问题：`.product-*` 不是 NZ-Coder 管理目录或 InfCodeX 保留字。把测试夹具命名提升为产品规则会
  隐藏用户合法的 `.product-catalog`、`.ci-tools` 等源码，比原来的测试污染更严重。
- 当前合同：默认仅排除 `.nz-coder`、`.nz-coder-runs`、版本控制元数据、缓存、依赖和构建目录；其他
  未受管隐藏目录继续参与 search、Repo Map、ProjectProfile 与 workspace snapshot。
- 回退验证：`.product-catalog` 和 `.ci-tools` 的真实源码可被相应消费者读取；受管目录仍保持排除。
  Watcher 启动窗口丢事件的独立修复继续保留。

### TP-023：Ctrl+C 取消后 trace 与 Session 没有终态

- 优先级：P1
- 状态：closed
- 复现：真实 80 列 TUI 发起深入只读审查，在第三批 read tools 运行时按一次 Ctrl+C。
- 证据：界面立即显示 `CANCELLING · waiting for safe boundary`，约 1 秒回到 IDLE；但 JSONL 最后一项是新的 `llm_request`，没有 `run_end`、`cancelled` 或对应 response。Session 只保留 user message 和 reminder，没有可见 cancelled part。
- 影响：UI 与持久化状态不一致；resume、timeline、trace 分析和 daemon attach 无法判断该轮是取消、崩溃还是仍在运行。
- 预期：取消在安全边界后统一经过 lifecycle finalize，写入 `run_end(status=cancelled)`，终止未完成 question/tool part，并保存可见取消摘要。
- 验收：取消后 trace 最后一项为 cancelled run_end；Session resume 显示该轮已取消；没有孤立 llm_request 或 running part。

### TP-024：显式要求的完整测试目录被 Broad Test Gate 拒绝

- 优先级：P1
- 状态：closed
- 复现：用户明确要求修改后运行 `python -m pytest -q cron_engine/tests`。
- 证据：工具返回 `Error: Broad test runner blocked. A source diff already exists`；Agent 只能改为逐个列出三个测试文件，虽然最终等价覆盖并得到 `75 passed`。
- 影响：安全策略覆盖用户明确验证范围，增加额外模型轮次，并可能在文件很多时漏掉测试。
- 预期：用户请求中明确给出的 workspace 内测试命令/目录应进入 requested verification scope；仅拦截模型自行扩大到无界全仓测试的情况。
- 验收：相同明确命令可直接运行；模型无依据从单测扩展到昂贵全仓测试时仍被限制。

### TP-025：长任务的 Agent 循环与累计上下文成本过高

- 优先级：P1
- 状态：closed（真实 Provider 同题达到 15/25 门槛并通过独立验收）
- 复现一：跨三个项目做深入只读审查。
- 证据一：41 tools（31 次 read_file）、约 145KB 工具输出、11 次模型请求、累计约 312k tokens、157.8 秒；其中两次 provider 请求分别约 77 秒和 57 秒。
- 复现二：实现 aliases + CLI count validation + tests + README。
- 证据二：47 tools、38 次模型请求、累计约 1.095M tokens、164 秒；最高单次输入约 45k，并未超窗，成本主要来自错误恢复和重复携带上下文。
- 影响：中等规模任务的调用成本和等待时间不可预测；SWE-bench 会快速消耗预算，并更容易在后期因 provider 延迟失败。
- 预期：为每阶段设置预算；优先结构化 repo evidence；连续 edit/test recovery 合并；达到足够证据后停止探索；trace 给出 per-stage calls/tokens/latency。
- 验收：同一长编辑任务不超过 15 次 model calls、25 次 tools；外部测试仍全部通过，最终报告不丢证据。

## 2026-08-14 InfCodeX / infcode-dev 终态与预算融合复测

### 实现依据

- 取消参考 InfCodeX `tool-cancellation.ts`、`catch-terminals.ts` 的“先结算
  transcript，再保存，再发终态”顺序，并参考 infcode-dev `prompt.ts` 的
  `MessageAbortedError` 语义。NZ-Coder 保留 Python SessionRuntime/生命周期边界，
  没有引入对方的 TypeScript host。
- 验证范围改为 run-local ContextVar：只放行用户自然请求里明确声明的 pytest
  路径，目标为空、扩大到仓库根或切换到 sibling tests 仍被 Broad Test Gate 拒绝。
- 工作预算采用 InfCodeX managed budget 的分区思想；真实长任务复测后把首次验收窗口
  从 70% 提前到 60%，orange/red 仍为 85%/95%。普通终端
  默认硬上限由 50 调整为 20，显式 `--max-turns` 和评测专用 override 仍可覆盖。

### 真实产品证据

- Session `session-20260814_162135-4c0290e2`：真实 TUI 在第三次 Provider 请求
  期间 Ctrl+C，约 1 秒回到 IDLE。trace
  `.../session-20260814_162135-4c0290e2__20260814_162135_7903c24c.jsonl`
  最后一条为 `run_end(status=cancelled, turn_count=3)`；Session assistant 为
  `finish=cancelled`、`MessageAbortedError`，没有 running tool part。
- 取消期间已开始的并行 read tool 被结算为 error，没有在 cancelled `run_end`
  之后启动新工具。TP-023 因此关闭；TP-003 保留 `verify`，原因是 TUI 已有
  CANCELLING/IDLE 反馈，但仍需确认 cancelled 摘要在不同终端高度下持续可见。
- CLI 不再在 typed cancelled result 之后追加 `substantial task/save_memory`
  合成提醒，恢复历史的最后业务轮保持为 cancelled assistant。
- 隔离目录 `.product-budget-B8tnir` 的编辑任务在 source diff 后执行用户指定的
  `python -m pytest -q cron_engine/tests`，未被 Broad Test Gate 拒绝；最终一次为
  `76 passed in 0.58s`。一次访问父 workspace 的组合命令仍被
  `Blocked: path outside workspace` 拒绝。TP-024 关闭。
- 首次长任务复测暴露 `--max-turns 12` 只写入 RunRequest metadata、未绑定 Runtime
  override：trace 错误显示 `max_turns=50`，人工中止前已产生 30 次以上模型请求；
  该问题没有被掩盖，随后增加 Runner 合同并修复参数传播。
- 修复后的 Session `budget-real-fixed-20260814` 使用真实
  `nz-coder run --max-turns 4`：trace 严格为 4 次 `llm_request`，第 4 次前记录
  `work_budget_pressure(zone=yellow, completed_turns=3)`，随后自然完成且没有第 5
  次请求；`run_end` 的 runtime 为 `turn_count=4, work_budget_zone=yellow`。
- 这证明硬上限与收敛提示已进入真实产品链，但尚未以默认 20 轮重跑同一完整编辑
  任务，因此 TP-025 保留 `verify`，不把受控 4 轮审查冒充完整性能验收。
- 默认 20 轮真实长编辑 Session `default20-long-20260814` 已完成：168.39 秒、
  20 次 model calls、26 次 tools、约 617k tokens，修改 6 个文件。外部独立验收为
  `103 passed in 0.80s`，但 Agent 在预算内只完成了 CLI 定向回归，没有执行最终
  整目录回归，因此超过 `<=15 calls / <=25 tools` 门槛，TP-025 继续保留 `verify`。
- 本轮额外暴露 TP-024 的中文自然语言边界缺口：提示中的
  `pytest -q cron_engine/tests，并...` 把中文逗号后的说明吞进 scope，导致已关闭问题
  在中文提示下复发。scope parser 现同时以 `，`/`,` 截断，并新增回归测试；该修复
  省去本轮 1 次拒绝、1 次拆分失败和后续恢复。随后真实 Session
  `chinese-scope-real-20260814` 使用同样的中文连接句，在产生 README diff 后直接执行
  原始目录命令并得到 `103 passed in 0.79s`；全程 6 次 model calls、5 次 tools、
  0 次 tool failure，证明修复已经穿过真实 Runner/ContextVar/Bash gate 完整链路。

### 自动回归

- 聚焦 runtime/headless/取消/测试范围/预算回归：74 项与新增专项均通过。
- 最终生产改动后重新运行完整仓库：`2126 passed, 21 skipped`，
  7 条 warning 均为既有多线程进程中使用 `fork()` 的 DeprecationWarning。

### TP-026：80 列 Plan 审批卡无法完整审阅计划摘要

- 优先级：P2
- 状态：closed（A397 真实 80×24 PTY 键盘复测）
- 复现：80 列 TUI 中生成包含多条步骤的 Plan，进入 `Plan ready` modal。
- 证据：摘要行在卡片右边界被直接切断（如 `@YEA...` 内容不可见），选项 description 也被截断；卡片没有滚动、展开或打开计划文件提示。
- 影响：用户只能批准一个无法完整阅读的计划，削弱审批的可信度。
- 预期：长摘要可垂直滚动/分页，或 modal 只展示完整短摘要并提供“View full plan”入口；选项文字安全换行。
- 验收：80 列、24 行下可仅用键盘读完整摘要和两个选项，再批准或返回修改。
- A370 修复：带 `detail` 的 selector 使用独立虚拟化 Markdown 视口，不再把完整正文塞入单行 option；
  `PgUp/PgDn/Home/End` 控制详情滚动，选项与完整问题/计划正文分区显示并按 80 列安全换行。确定性测试
  覆盖正文不截断、详情滚动与 Plan 三项操作；真实终端尺寸和鼠标/键盘组合仍需下一轮 PTY 验收。
- A397 验证：真实80×24 modal显示产品生成的三项稳定选择；按`End`后可读到21行摘要末尾
  `PTY-END-MARKER-8F3C`，按`Home`返回并以默认项批准，随后显示`Run completed`。独立第二次modal还实际
  显示`Keep Planning`与底部第15–21行，证明详情视口和option区没有复用同一截断行。

### TP-027：显式验收依赖模型主动记得运行，短任务可无证据结束

- 优先级：P1
- 状态：closed
- 复现：要求 Agent 修复一个 1 行 bug，并明确写出
  `python -m pytest -q tests`，同时要求模型不要主动调用 Bash。
- 原始问题：Runtime 只把命令当提示词；模型如果提前总结、在长任务末尾耗尽轮次，
  就可能留下正确 patch 但没有最终验收证据。默认 20 轮长任务
  `default20-long-20260814` 正是这种情况。
- 修复：新增 run-local `VerificationContract`。它只接受安全、workspace-relative、
  有明确测试目标的 pytest 命令，在 yellow/orange/red 或自然完成边界通过正常工具
  pipeline 自动执行；同一 mutation generation 只尝试一次，后续编辑会重新启用。
- 首次真实复测：自动 Bash 得到 `3 passed`，但下一轮 DeepSeek V4 thinking 请求返回
  `reasoning_content must be passed back`。根因是 provider 投影只保留已有字段，没有像
  InfCodeX 那样为每个 assistant replay（包括合成 tool turn）补默认空字段。
- 最终证据：Session `verification-contract-state-fixed-20260814` 完成 1 行修复；模型工具
  只有 read/glob/edit，唯一 Bash 的 call id 为 `verification-contract-1-1`，事件 zone 为
  `completion`，输出 `3 passed in 0.00s`。随后模型读取证据并总结，`run_end=completed`、
  headless exit code 0，trace 无 API error。Runtime contract 同步结算旧 VerificationManager
  的 planner-only required stage，终态为 `verification_needed=false`、
  `verification_state=passed`、`next_required_stage=null`，不再出现 completed/verifying 矛盾。
- 验收：解析/状态/Runner/DeepSeek replay 聚焦回归 47 项通过，真实 headless 产品闭环
  通过。该修复减少“模型记得测试”的随机性，但没有把 TP-025 的长任务数值门槛冒充
  已达标，TP-025 仍保持 `verify`。最终完整仓库回归为
  `2141 passed, 21 skipped, 7 known fork warnings`。

## 2026-08-14 TP-025 长任务收敛复测与修复

### 五次同任务真实证据

- 所有运行都从原始 `cron_engine` 隔离副本开始，运行前基线均为 `59 passed`；任务、
  模型、默认 20 轮、auto 权限和最终独立验收保持一致。
- `convergence-baseline-20260814`：20 model calls / 32 tools，约 510k total tokens，
  `max_turns`；独立测试 91 passed，但 README 未更新。前 5 轮已读完实现和测试，随后
  6 轮用于 shell/import/PYTHONPATH 探测，首次源码修改迟至第 12 轮。
- `convergence-final-20260814`：实施阶段门把预编辑环境探测从 6 次压到 2 次；仍为
  20 / 29、约 649k tokens。独立测试 88 passed，但 scheduler 测试未落盘，模型总结
  却声称已经补充，暴露终态证据不诚实。
- `convergence-final2-20260814`：`apply_patch` 已兼容 provider 常见的顶层 `path` 加
  无 path hunks 写法，五个目标文件全部落盘；20 / 28、约 614k tokens。验收仍为
  88 passed / 13 failed，根因是 CLI subprocess helper 把 cwd 算成 package 目录而非
  package 父目录。旧恢复提示诱导了全局环境探测。
- `convergence-final3-20260814`：加入 subprocess package-root/workspace-boundary
  分类诊断，并修复最大步数摘要误报 completed；19 / 26、约 604k tokens，五个目标
  文件完整，独立验收 93 passed，真实 `run_end=completed`。
- `convergence-final4-20260814`：yellow/首次 Runtime acceptance 从 70% 提前到 60%；
  第 12 轮自动测试及时发现 scheduler 回归，第 13 轮完成修复。最终 18 / 28、约 768k
  tokens，六个真实修改文件，独立验收 105 passed，`run_end=completed`。Provider 延迟
  和推理 token 波动显著，不能用单次耗时代表本地工具性能。

### 本轮产品修复

- 明确验收契约 + 足够调查证据 + 尚无编辑时，阻止继续做 pre-edit shell 环境探测；
  structured reads、writes、project creation 和无契约任务不受影响。
- `apply_patch` 保留标准逐 hunk path，并新增单文件顶层 path fallback；所有 hunks 仍先
  验证再原子写入，路径继续经过 `_safe_path()`。
- `RecoveryState` 区分 subprocess package root 与 collection import failure；前者直接
  指向测试 helper 的 cwd/env，禁止 pip/global environment 探测。workspace 越界则明确
  要求移除 workdir、使用当前 workspace root。
- 最后一轮文本明确承认“已达到最大步数/maximum steps reached”时返回 `max_turns`，
  不再因为模型输出了总结就冒充 `completed`；恰好在最后一轮正常给出 `done/finished`
  的 1-turn child 仍保持 completed。
- 首次收敛/验收窗口改为 60%，orange/red 仍为 85%/95%，让失败验收有实际修复预算。

### 结论

- 正确性闭环已从“测试通过但缺文件/总结夸大”提升到连续两次五层需求完整、独立整目录
  测试全绿，且触顶终态已经诚实。
- 数值门仍未达到：最佳完整运行是 18 model calls / 28 tools，目标为 15/25。因此
  **TP-025 继续保持 `verify`，不能关闭**。剩余主因是首轮读取非关键入口文件，以及
  验收通过后的 `diff_status`/`verify_changed_files`/py_compile/重复 pytest；下一轮应做
  provider-neutral 的“显式验收已通过”状态消费与非 Git 验证收口，而不是继续降低硬上限。
- 最终完整仓库回归：`2151 passed, 21 skipped, 7 known fork warnings`；Ruff 与
  `git diff --check` 均通过。

## 2026-08-14 显式验收消费与三轮真实长任务复测

### 修复内容

- `VerificationContract` 新增 token 级精确命令匹配。模型主动执行的显式验收命令会按
  当前 mutation generation 结算成功/失败，并同步现有 VerificationManager；Runtime
  自己生成的 Bash 带内部 marker，仍由 Runner 单点结算，避免同一次命令双计数。
- 验收证据记录 `source` 与 `zone`。模型主动验收或自然完成边界的通过证据可以提示最终
  总结；yellow/orange/red 的预算区检查即使通过也只算中间证据，后续编辑会重新激活。
- RuntimeState 持久化 Todo 未完成项。预算区遇到 open Todo 时延后 synthetic acceptance，
  但模型自然结束时仍必须执行用户声明的验收，不能用未完成 Todo 绕过测试。
- subprocess package-root 诊断不再让模型猜 `parents[...]` 层数，而是给出当前 active
  workspace root、检测到的 package 名，以及“cwd 必须是包含 package 目录的根目录”。
- `apply_patch` provider schema 现在要求每个 change 明确携带 path；handler 继续兼容已有
  top-level single-file path。另新增事务化 `op=append`，用于无可靠 old_text 锚点的 EOF
  追加；该能力已有红绿单测，但本轮没有再消耗 API 做第四次长任务实测。

### 三轮同条件真实结果

| Session | model/tools | tool failures | 独立验收 | 终态 | 主要阻塞 |
|---|---:|---:|---|---|---|
| `convergence-contract-settlement-20260814` | 20 / 31 | 6 | 94 passed / 14 failed | `max_turns` | CLI helper 先指向旧 checkout，后又把 cwd 设成 package 目录 |
| `convergence-contract-settlement2-20260814` | 20 / 29 | 10 | 91 passed / 1 failed | `max_turns` | 6 次 apply_patch 缺 path；新 scheduler 边界测试发现真实快速路径缺陷 |
| `convergence-contract-settlement3-20260814` | 20 / 30 | 7 | 93 passed / 14 failed | `max_turns` | 缺 path 已降为 0；2 次 EOF 大段 old_text 锚点失败，CLI cwd 仍差一级 |

- 三轮都从原始 `59 passed` 隔离副本开始，使用同一中文任务、DeepSeek V4 Flash、auto
  权限与 20 轮上限。累计 token 分别约 574k、610k、699k；本地完整 pytest 均小于 0.5s，
  wall time 与 token 成本主要来自 Provider 和重复恢复轮次。
- 第三轮证明 schema 修复穿过真实 provider：第二轮 6 次 `requires path` 到第三轮为 0；
  但 20/30 仍超过 15/25 门槛，且补丁没有独立全绿，因此不能关闭 TP-025。
- 当前主要剩余差距已经收敛为：初始 8–12 个 read tools 仍偏多；复杂测试追加仍使用脆弱
  大段 replace；模型在 package parent 的简单一行修复上仍会 off-by-one；预算测试虽能
  提前暴露回归，但多阶段任务缺少 Todo 时无法确定所有需求是否已经落盘。

### 自动回归

- 聚焦 RuntimeState/Runner/Recovery/Loop/Tool 回归：最终相关批次 157 项通过。
- 完整仓库：`2173 passed, 21 skipped, 7 known fork warnings`，耗时 103.88 秒；Ruff 与
  `git diff --check` 通过。
- 结论：验收状态闭环和 provider tool schema 有真实改善，但长任务产品门仍未通过，
  **TP-025 保持 `verify`**。

## 本轮确认可用

- 正确 Python 包布局下，Agent 能完成一次真实小功能修改：增加 `@hourly`、补充测试，结果为 `32 passed`。
- `plan` 模式没有修改目标业务文件，说明核心只读写保护生效。
- 直接 shell `!pwd` 能即时执行，并返回准确 workspace 路径。
- `/session` 能列出当前与历史 Session，并恢复 19 条消息及原 `plan` 模式。
- `/timeline` 与 `/trace` 能提供 turn、工具和耗时证据；本轮据此定位到 `plan_exit` 约 25.4 秒及多轮无 final text。
- Git workspace 中完整编辑—测试—总结闭环正常：增加 `@monthly`、`31 passed`，7 个工具、16.4 秒后回到 IDLE；说明 TP-013 与非 Git 收口路径高度相关。
- `auto` 模式能够无确认完成 workspace 编辑和指定 pytest。
- persistent process 能在 1.3 秒左右启动并返回稳定 `proc_*` ID；`/processes logs` 读到 `READY`，`/processes kill` 成功终止，退出后未残留子进程。
- LSP optional pack 能按需加载，hover 请求可返回结构化范围；但跨文件解析能力未达到可用标准，见 TP-018。
- 空闲状态快速双击 Ctrl+C 本轮立即显示 `Goodbye!` 并以 exit code 0 退出。

## 后续真实测试队列

- 在 Git workspace 中重复完整编辑闭环，区分模型延迟、review 收口与非 Git diff 的各自耗时。
- 测试从 `plan` 切回 `acceptEdits` 后能否继续同一任务，以及 picker 按键穿透在 model/session 选择中是否同样存在。
- 测试 `/compact` 前后上下文保留、长工具输出裁剪和最终回答可见性。
- 测试 Session resume/fork/export、附件、timeline、message navigation。
- 测试直接 shell `!`、长进程 `/processes`、取消和日志 follow。
- 继续测试 persistent process 的 follow、自然退出、Session 恢复与 daemon attach 所有权。
- 测试 Python LSP 项目根修复前后的 definition/reference/diagnostics，并继续覆盖 Bash/C++ LSP。
- 测试 Git 仓库中的 diff、undo/redo 与用户已有修改共存。
- 测试 daemon/attach 断开重连与权限决策一致性。
- 在 Windows 安装包上重复同一套核心用例。

## 2026-08-14 长任务契约化收敛（静态/假 Provider 验证）

### 本轮修复

- 把现有 planner 的一次输出扩展为 `plan + TaskContract`，没有新增模型调用。
  `RequirementLedger` 按 mutation generation 记录预期产物、目标验证和精确验收证据；
  `CompletionGate` 在硬需求缺证据时阻止自然完成，Todo 不再是完成判据。
- 对多产物、中高复杂度任务生成有界 `ImplementationBundle`，首轮直接提供项目根、
  Python package/module cwd、测试入口、预期文件和高置信源码片段，减少重复摸索目录结构。
- 验证改为分层调度：yellow 只做静态检查，orange 做目标测试，red 只在需求账本清晰时
  允许精确验收，自然完成边界必须运行用户声明的 acceptance command。验收通过后直接
  使用已有最终回答，不额外消耗一次模型调用。
- 默认 20 次硬上限内划分 13 个 normal calls、2 个 closure calls 和 5 个 emergency calls。
  closure 阶段隐藏广域探索/子 Agent 工具，只保留已知路径读写、diff 和聚焦验证。
- Durable Session 保留完整工具证据；发给模型的 provider projection 会在后续写入后压缩
  旧文件读取，在同代验证通过后压缩旧失败输出，避免 stale evidence 继续占上下文或误导模型。
- 新增 provider schema adapter 和递归 linter。DeepSeek/OpenAI-compatible 的展示 schema
  可安全简化组合关键字和描述，但 canonical schema、嵌套 `required`、enum 与 handler
  契约保持不变。

### 验证与状态

- 本阶段聚焦回归：`218 passed`；其中 fake-provider 用例确认 planner 只调用一次并产生
  可持久化 TaskContract，完成边界执行精确验收后不产生第三次无意义模型调用。
- 完整仓库回归：`2212 passed, 21 skipped, 7 known fork warnings`，耗时 147.25 秒。
- 所有当前改动 Python 文件通过 Ruff；`git diff --check` 通过。
- TP-025 仍为 `verify`：本轮解决的是源码架构和确定性状态闭环，尚未重新消耗外部 API
  运行同任务 A/B，因此不能把调用数、工具数或 SWE-bench 分数写成已经提升。
- 后续真实测试应固定仓库快照、模型、prompt、验收命令和预算，比较旧/新 Runtime 的
  完成率、model calls、tools、tokens、wall time、重复读取及验证次数。

## 2026-08-14 Contract-Led Runtime 真实 DeepSeek A/B

两轮都从同一个原始 `59 passed` 的 `cron_engine` 副本开始，使用完全相同的中文任务、
DeepSeek V4 Flash、auto 权限和 20 execution-turn 上限；最终均由外部进程独立运行
`python -m pytest -q cron_engine/tests`，不采信 Agent 自报结果。

| Session | planning | execution calls / tools | tokens | 独立验收 | 终态 |
|---|---|---:|---:|---|---|
| `contract-led-real-20260814` | 默认关闭 | 20 / 32 | 493,135 | 94 passed / 6 failed | `max_turns` |
| `contract-led-plan-real-20260814` | 显式开启但 contract JSON 截断 | 20 / 28 | 417,010 | 80 passed / 5 failed | `max_turns` |

### Trace 结论

- 默认产品配置 `NZ_PLANNING_ENABLED=false`，所以第一轮没有 planner、TaskContract、
  RequirementLedger 或 ImplementationBundle；前 6 个 execution turn 仍逐项读取 12 个
  文件。此前 fake-provider 证明的是可调用路径，不是默认产品路径。
- 显式开启 planning 后，DeepSeek 的 planner 输出长 3672 字符，在 JSON 字符串中间被
  截断；trace 为 `planning_contract_invalid: Unterminated string`。兼容 fallback 将原始
  文本存为 plan，但把 contract 清空，因此第二轮仍没有 `implementation_bundle_ready`。
- 第二轮实际还有 1 次 planning 和 2 次 replanning Provider calls；它们没有计入
  `llm_request=20` 或 headless usage。以“20 model calls”描述成本会漏报控制面调用。
- 两轮都修改了 parser、三类测试和 README，但 CLI subprocess 从错误 package root
  导入旧实现；第一轮还遗漏 `SAT-SUN` 跨周范围语义。Runtime 在最后一次验收看到真实
  失败并诚实返回 `max_turns`，但没有足够 closure budget 修复。
- 第一轮曾生成 `cd /home/pyh/test_nzcoder ...`，被 workspace boundary 正确拒绝；说明
  安全边界有效，但结构化 package/module cwd 事实没有进入模型上下文。

### 新确认问题

- **TP-028（P0，open）**：Contract-led 能力在默认产品配置下不启用；即使手动启用，
  真实 Provider 的截断 JSON 会让 contract 和 bundle 静默退化为空。验收条件应包含：
  默认长任务必须产生有效 contract；JSON 截断可确定性恢复或 fail closed；trace 必须有
  `implementation_bundle_ready`，并且 RequirementLedger 实际观察写入和验证。
- **TP-029（P1，open）**：产品调用/usage 统计只覆盖 execution `llm_request`，不包含
  planning、replanning 和 sidecar Provider 调用。验收条件是 result/trace 同时报告按
  purpose 分类的全部 Provider calls/tokens/latency，并保留 execution-turn 单独指标。
- **TP-030（P1，open）**：`doctor --workspace PATH` 已不再接受，但真实测试文档仍使用该
  命令。当前 workaround 是在目标目录运行 `nz-coder doctor`；CLI 与文档需统一。

结论：本轮真实测试没有通过，TP-025 继续 `verify`。当前应先修 TP-028，使合同化主路径
在真实 Provider 下真正生效，再重跑同题；否则继续调整 verification/closure 只是在优化
旧执行路径。

## 2026-08-15 默认 Contract 主链修复与第三轮真实 DeepSeek 复测

### 已实现并通过本地门禁

- 默认路径不再依赖 `NZ_PLANNING_ENABLED`：只要用户给出可验证的精确 acceptance
  command，Runtime 会零 Provider 调用生成保守 `TaskContract` 和 `RequirementLedger`。
  可选 planner 成功时可以丰富该 contract；JSON 截断、旧格式或空 contract 时保留
  bootstrap contract 和确定性 fallback plan，不再静默清空主链。
- 多 requirement contract 即使文本启发式被判为 simple，也会生成首轮
  `ImplementationBundle`。单一嵌套 Python 项目会报告正确 project root、module cwd、
  test root 和 `python -m pytest -q <nested>/tests`。
- ModelGateway 的 buffered/streaming 终态统一发出按 purpose 分类的 calls、attempts、usage、
  duration；RuntimeState、run metadata 和 headless JSON 暴露相同聚合。coding usage 仍由
  Runner 计入 RunContext，planning/replanning/sidecar 只在 observer 中加入一次，避免双计。
- 新增组合回归固定“自然结束先执行精确验收、验收更新 ledger、CompletionGate 再放行”的
  顺序。聚焦回归 `215 passed`；完整仓库 `2224 passed, 21 skipped, 7 warnings`，耗时
  152.25 秒；全仓 Ruff 和 `git diff --check` 通过。

### 第三轮真实结果

隔离目录：`/home/pyh/test_nzcoder/.product-contract-led-fixed-FJHzrM`；Session：
`contract-led-fixed-real-20260815`。仍从原始 `59 passed` fixture 开始，使用相同任务、
DeepSeek V4 Flash、auto 权限和 20 execution-turn 上限。

| execution calls | tools | Provider total | 独立验收 | 终态 |
|---:|---:|---:|---|---|
| 20 | 35 | 699,514 tokens | 92 passed / 5 failed | `max_turns` |

- 真实 trace 首次证明默认产品路径已激活：`task_contract_bootstrapped=1`、
  `implementation_bundle_ready=1`；headless JSON 报告 `provider.calls=20`、
  `attempts=20`、`calls_by_purpose={"coding": 20}`。本轮没有 planning/replanning/sidecar
  调用，因此控制面分类仍只有 fake-provider 回归证据。
- 主功能、parser/scheduler 测试和 README 已修改；独立失败全部来自新增 CLI 测试 helper
  仍将 subprocess `cwd` 硬编码为 `/home/pyh/test_nzcoder`。子进程因此导入旧 fixture，
  不是隔离工作区的新 parser。Agent 在读到该 helper 后仍尝试越界 `cd` 和两次 editable
  pip install，没有完成应有的一行 workspace-relative 修复。
- bootstrap contract 没有 planner 提供的 expected artifacts，因此 bundle trace 为
  `artifact_count=0`、`candidate_count=0`。它提供了正确的嵌套 project/module/test facts，
  但没有减少首轮逐文件读取；20 轮共 35 个工具调用，效率比前两轮更差。
- 模型运行的 pytest 命令都追加了 pipe/tail/grep，严格 acceptance 匹配正确地没有把它们
  冒充用户精确契约。但模型始终以工具调用结束，Runner 在 hard-cap 分支直接结算；最终
  `verification_contract.attempts=0`、ledger 全 pending、final text 为空。Runtime 诚实返回
  `max_turns`，却没有在硬上限边界补跑精确 acceptance。
- shell 默认没有 `pipefail`，所以 `pytest ... | tail` 的 shell exit code 可能是 0。验证分析
  从输出识别到失败，但 Tool Recovery 没得到 command failure，因而没有注入 package-root
  诊断；随后发生了错误的环境安装恢复路径。

### 新增/收窄问题

- **TP-028（P0，open，已部分修复）**：默认 contract/bundle 激活已由真实 Provider 证明，
  但零调用 bootstrap 无 expected artifacts，导致候选文件与 mutation evidence 缺失，
  RequirementLedger 无法在 hard cap 前收口。下一验收必须同时看到非空候选工作集、ledger
  evidence 和独立测试全绿。
- **TP-029（P1，verify）**：真实 headless 已输出 coding purpose 聚合；planning、replanning、
  sidecar 的 exactly-once 统计已有本地回归，但尚无一轮真实多 purpose trace。
- **TP-031（P0，open）**：hard-cap 在最后一次模型响应仍有 tool calls 时绕过精确 acceptance
  contract；必须在终态前执行一次 due contract 并持久化结果，且不能把失败伪装成 completed。
- **TP-032（P1，open）**：Bash pipeline 可掩盖 pytest 非零状态，CLI subprocess 行为与父
  pytest 分叉时 Recovery 只识别 `No module named`，不能识别“导入了陈旧 workspace 包”。
  需要保留 pipeline 首个失败状态，并直接引导检查 helper 的 `cwd/env`，禁止用 pip install
  掩盖测试隔离错误。

结论：TP-028 的“默认主链未激活”根因已消除，但第三轮仍未通过产品门，TP-025 继续
`verify`。下一步不是再堆 prompt，而是按 TP-031 → TP-032 → bootstrap candidate/artifact
inference 的顺序修复确定性 Runtime 边界，然后再进行一次同题付费复测。

## 2026-08-15 第四轮前确定性 Runtime 收口（本地门禁阶段）

本轮先不调用真实 Provider，而是针对第三轮 trace 的五个确定性根因建立 G1-G7 门禁。
架构设计见 `docs/superpowers/specs/2026-08-15-long-horizon-deterministic-runtime-design.md`。

- **TP-031 已本地修复，待真实验证**：natural response、buffered tool batch、streamed tool
  batch 和 loop exhaustion 统一进入 Terminal Boundary settlement。最后一次工具编辑会先执行
  当前 mutation generation 的 exact acceptance，再更新 RequirementLedger，最后决定状态。
  G1/G2 同时覆盖 PASS/FAIL：PASS 仍要求所有硬 requirement 有证据；FAIL 永远不能
  `completed`。工具终态没有模型正文时，只根据 changed files 与 exact command 生成事实摘要。
- **预算语义已收紧**：产品预算明确为 1-13 normal、14 closure repair、15 closure finalize、
  16-20 bounded emergency。第 15 次后只有“已有 diff、存在失败证据、修复目标已知且无需广搜”
  才继续；否则按名义 SLA 结束。bounded emergency 的 schema 可见性和执行 guard 双重限制
  repo-wide 搜索、task、无关新文件与 package install，并记录独立计数。
- **TP-032 shell 部分已本地修复**：POSIX Bash 使用 `-o pipefail`；父/子 ToolExecutor 都以
  `ToolOutput.metadata.exit` 为命令成功真值，不再解析展示文本。仅有 `sh` 时验证 pipeline
  fail closed，提示直接运行命令并让 NZ-Coder 自己截断/落盘输出。
- **TP-032 subprocess 部分已本地修复**：Recovery 会从 pytest 失败 ID 找到 helper，以受限
  AST 解析 `subprocess.run/Popen/check_*`、`python -m package` 和静态 cwd 表达式。检测到旧
  fixture 后输出 helper 行号、旧 cwd、active workspace 与 package，只建议修改 test helper，
  不再建议安装包或改生产代码。
- **TP-028 artifact 部分已本地修复**：BootstrapArtifactResolver 不依赖 Provider、LSP、
  embedding 或 RI readiness。它能从 cron 请求解析 parser、三份独立测试与嵌套 README 为
  hard artifacts，并把 scheduler/CLI/`__main__` 作为 soft candidates。多个测试文件拆成多个
  requirement；pytest PASS 不能满足未修改的 docs/artifact/test requirement。
- Runtime/headless 摘要继续分别报告 Provider purpose calls/attempts/tokens/duration，并新增
  `verification_failures`、`package_install_attempts`、`emergency_broad_exploration` 与
  `work_phase`。这些统计没有新增任何模型调用。

当前状态仍是“本地门禁阶段”，不把上述修复宣称为真实长任务性能收益。只有 G1-G7、完整
pytest、Ruff 和差异检查全部通过，才允许第四次同题 DeepSeek 复测。

## 2026-08-15 第四轮真实 DeepSeek 反证与后续确定性修复

第四轮使用隔离目录
`/home/pyh/test_nzcoder/.product-deterministic-fourth-MkdSxe`、Session
`deterministic-fourth-real-20260815`、DeepSeek V4 Flash、auto 权限和相同 20-call
硬上限。原始 fixture 独立基线为 `59 passed`，没有用前轮产物续跑。

| coding calls | sidecar calls | tools（含 Runtime synthetic） | Provider tokens | 独立验收 | 终态 |
|---:|---:|---:|---:|---|---|
| 20 | 1 | 32 | 930,399 | 34 passed / 61 failed | `max_turns` |

- 确定性主链真实生效：trace 有 `task_contract_bootstrapped=1`、
  `implementation_bundle_ready=1`；Terminal Boundary 在 generation 5 和 6 各执行一次
  exact acceptance，`VerificationContract.attempts=2`，两次失败都被持久化，未误报
  `completed`。package install 为 0，广域命令被 emergency gate 拒绝。
- 任务本身失败。最终 `parser.py` 的解析循环出现两个连续的
  `expanded.append(vals)`，五字段结果整体错位；这是普通 Python 语法检查无法发现的语义
  回归。Agent 在第 15 个 coding call 才首次执行测试，修复预算已经不足。
- 调用分解：前 5 个 coding calls/12 个工具用于目录和逐文件读取，其中 `__init__.py`、
  `pyproject.toml`、`__main__.py` 与一次被拒绝的测试探测不是首次编辑所必需；随后 8 个
  coding calls 围绕同一 parser 分段 edit/replace/read，存在明显 provider micro-step；
  emergency 的 5 个 calls 中只有读取并修复 CLI helper 两次是局部修复，其余是诊断探测。
- Recovery 的优先级存在误导：全量失败同时覆盖 CLI、parser、scheduler，却先选择了
  `subprocess_workspace_drift`，导致 Agent 修了真实但次要的 CLI cwd，未先检查共同的 parser
  生产回归。失败后的 verification plan 又把大量 test node 全部变成 required commands，
  放大上下文。当前证据说明下一刀仍应是确定性恢复排序和聚焦验证，不足以证明需要 Code Mode。
- 第四轮还暴露两处可观测性/契约边界：文本中的 `0/7` 与 acceptance 目录
  `cron_engine/tests` 被 slash 正则误认成源码 artifact；模型从 synthetic history 复制
  `_nz_runtime_contract` 后可把任意 `python -c` 伪装成 emergency 验收。headless 顶层
  `total_tokens` 又只加 input/output，与 mutually-exclusive purpose buckets 的 930,399 不一致。

真实反证后已用本地 TDD 收口上述确定性问题：artifact resolver 只把现存文件或明确文件名
作为 artifact，并在真实 cron 文案下绑定 `cron_engine/parser.py`；runtime internal-looking
verification flag 必须与精确 contract 或静态分类一致；多测试文件失败优先分类为
`widespread_test_regression`，每个测试文件最多保留一个且总计最多三个 required target；
hard-cap 失败也生成非空事实摘要；RunResult total 改为互斥 token buckets 之和。第四轮原始
fixture 和 trace 保留为失败证据，没有重跑覆盖结果。TP-025 继续 `verify`，不能宣称终端长任务
已经稳定通过。

后续修复的聚焦回归为 `158 passed`；完整仓库为 `2247 passed, 21 skipped, 7 known fork
warnings`，耗时 114.88 秒。上述变更文件 Ruff 与 `git diff --check` 均通过。

## 2026-08-15 第五轮真实 DeepSeek 测试

第五轮使用新的隔离副本
`/home/pyh/test_nzcoder/.product-deterministic-fifth-sFz6mK`，运行前从工作区根执行原验收得到
`59 passed`，并确认 `cron_engine.__file__` 指向该副本。Session 为
`deterministic-fifth-real-20260815`，仍使用 DeepSeek V4 Flash、auto 权限、相同任务和
20-call emergency hard cap。

| coding calls | sidecar calls | tools | wall time | Provider tokens | 独立 suite | 终态 |
|---:|---:|---:|---:|---:|---|---|
| 19 | 1 | 31 | 207.10 s | 871,743 | 102 passed | `completed` |

正面证据：bootstrap contract 这次正确绑定 parser、三份 test 和 README；generation 4 的
exact acceptance 失败后进入 bounded emergency，generation 5 再次 exact 得到 `102 passed`；
contract attempts=2，RequirementLedger 七项全部 satisfied，package install=0。外部重新运行
同一 suite 也是 `102 passed`，CLI parse 和 month/day name 功能抽样通过。这证明 Terminal
Boundary、结构化 shell failure、ledger consumption 与 emergency 内的最终局部修复可以形成
真实闭环。

产品门仍然失败：19 coding calls 超过 15-call SLA，31 tools 超过 25；
`emergency_broad_exploration=1`，并有两次 emergency 探测被 gate 拒绝。前四 calls 读取了
11 个文件/目录证据；parser 实现后直到第 15 call 才首次运行 suite。两次 test append 因错误
old_text 失败，随后又运行 non-Git workspace 的 `git diff`；这些恢复往返是主要超标来源。

恢复排序仍需调整。`widespread_test_regression` 避免了第四轮只盯 CLI 的误导，但第五轮失败
同时包含两个彼此独立且均可确定性证明的问题：CLI helper 的 stale absolute cwd，以及
scheduler 当前小时未先检查 allowed hours。当前分类抑制了精确 workspace drift 事实，模型在
call 16 读到 helper 后又用 call 17/18 探测旧目录，直到 call 19 才同时修两处。下一版应生成
composite diagnostic，而不是在“widespread”与“workspace drift”之间二选一。

官方 102 项 suite 还不足以证明用户要求的兼容性。外部定向抽样发现：原版数字表达式
`0 0 * * 5-1` 会抛 `CronParseError`，第五轮补丁却返回 `[0,1,5,6]`，改变了现有数字 API；
`FRI-MON/2` 又因对环绕后的原始数值做 `(v-lo) % step`，得到错误的 `[0,1,5]`。补丁只测试了
非环绕 `MON-FRI/2`，没有覆盖 numeric descending preservation 或 wrap+step。因此第五轮不能
按“102 passed”关闭功能正确性。

另一个独立产品缺陷是 final summary：Terminal trace 的最终 decision 为 `completed`、reason
为 `exact_acceptance_and_ledger_satisfied`，settler 也生成确定性摘要，但 headless JSON 的
`text` 仍为空。根因是 `ProductionRunLifecycle` 只把 `content_text` 发给 callback，没有写入
`last_status["content"]`；typed result 随后只能从空 assistant tool-call messages 回溯。该问题
需要在 Lifecycle→RunResult envelope 修复并增加真实 headless 回归。

结论：第五轮比第四轮从 61 failures 提升到 suite 全绿并得到诚实 `completed`，但调用数、
工具数、final summary 和额外兼容性抽样均未达到门槛，TP-025 继续 `verify`。原始 Session、
trace 和隔离补丁全部保留，没有修改该 fixture 来美化结果。

## 2026-08-16 第六至八轮效率收口复测

三轮继续使用相同任务、DeepSeek V4 Flash、auto 权限、20-turn hard cap 和全新的
`59 passed` 隔离 fixture；没有续跑或修改前一轮产物。

| Session | coding / sidecar calls | tools | tokens | 独立 suite | 状态 |
|---|---:|---:|---:|---|---|
| `efficiency-sixth-real-20260816` | 19 / 4 | 37 | 628,234 | 97 passed | completed，但摘要是过渡文本 |
| `efficiency-seventh-real-20260816` | 16 / 0 | 26 | 529,028 | 96 passed | completed，事实摘要 |
| `efficiency-eighth-real-20260816` | 15 / 0 | 24 | 455,057 | 92 passed | completed，事实摘要 |

真实 trace 反向推动了以下修复：terminal tool batch 统一生成 deterministic factual summary；
失败写入不推进 mutation generation；static/targeted 只对新的 production mutation 重开；exact
失败后的 test-only repair 不再先跑较弱 targeted；contract-led pre-edit 调查达到 6 次后隐藏
继续广搜工具；non-Git closure 阻止 `git diff/status`；composite diagnostic 的 primary、supporting
和 repair target 随当前失败刷新。

第八轮达到 TP-025 的单次 15/25 数值门，且 `emergency_broad_exploration=0`。但状态仍保持
`verify`：第七轮以 16/26 小幅超门，尚未证明跨随机运行稳定；同时外部兼容探针虽确认数字
`5-1` 仍拒绝、`0/7/SUN` 等价，生成补丁仍拒绝 `FRI-MON/2`，说明新增 suite 对“名称范围”
存在语义盲区。效率门首次通过不等于功能正确性可只凭自生成测试关闭。

最终完整仓库回归为 `2267 passed, 21 skipped, 7 known fork warnings`（151.40 秒）；
compileall、Ruff 与 `git diff --check` 均通过。

## 2026-08-17 第九轮语义完成证据与 Provider 兼容修复

本轮在 `/home/pyh/test_nzcoder/.product-semantic-closure-WEsEJY` 运行。原 fixture 的 CLI
测试写死 `/home/pyh/test_nzcoder`，因此先只把复制品 helper 的 cwd 改为基于 `__file__` 的
自身根目录；功能代码保持原样，规范化后基线为 `59 passed`。

| Session | coding / sidecar calls | tools | tokens | 独立 suite | 终态 |
|---|---:|---:|---:|---|---|
| `semantic-closure-ninth-real-20260817` | 20 / 6 | 28 | 951,595 | 101 passed | `max_turns` |

功能结果比第六至八轮更完整：外部探针确认 `5-1` 抛 `CronParseError`，`0/7/SUN` 等价，
`FRI-MON/2` 为 `[0,5]`。但 Runtime 没有把 suite 全绿当作 compatibility 完成；R6 正确保留
`required_evidence=[semantic_review]` 并停在 candidate。因此这轮非零退出是可信终态，而不是
补丁失败或 false completed。

### TP-033：语义审查在 DeepSeek thinking 模式下无法形成证据

- 优先级：P0
- 状态：verify
- 真实证据：第 15–20 个 terminal boundary 均以 `semantic-contract` 强制启动 sidecar；六次
  Provider 请求均立即返回 400 `Thinking mode does not support this tool_choice`。旧 fail-open
  accept 没有被写成语义证据，这是正确安全行为，但主 Agent 无法修复 Provider 协议，最终多耗
  5 个 coding calls 后到达 hard cap。
- 源码差距：InfCodeX OpenAI Provider 会在上游拒绝 forced tool choice 时保留 tool schema、
  去掉强制字段并重试；NZ-Coder 原 Gateway 直接把 400 终结为 client error。infcode-dev 也将
  tool choice 保持在 Provider projection，而非当成所有兼容端点共有的能力。
- 修复：buffered/streaming Gateway 新增一次 forced-tool-choice capability fallback；无关 400
  仍 fail-fast。`deepseek-v4*` 的 bounded structured verifier 关闭 thinking，避免 1024 token
  全被 reasoning 消耗。使用第九轮完整 evidence packet 的真实后置调用在 2.63 秒内返回
  `verifier_ok/accept`，184 output tokens，且没有 fallback。
- 尚缺验收：原第九轮失败记录不覆盖；需要后续一个全新端到端 coding task 证明 ledger 写入
  `semantic_review_passed` 后 Terminal Boundary 直接形成 `completed`。因此保持 `verify`。

### TP-034：文档化的 `run --prompt` 参数实际不可用

- 优先级：P2
- 状态：closed
- 真实证据：按顶层 `nz-coder --help` 执行 `nz-coder run --prompt ...`，进程立即以 exit 2
  返回 `unrecognized arguments: --prompt`；改用位置参数后真实任务才能启动。
- 修复：headless parser 正式支持 `-p/--prompt TEXT`，并保留位置参数和 stdin；帮助文本与
  实际 parser 已统一，回归覆盖文档命令和 clean stdout。

TP-025 继续 `verify`。这轮 patch 质量和具体兼容探针通过，但 20/28 超过 15/25 产品门；其
中最后 5 个 coding calls 是 sidecar Provider 协议失败造成的确定性浪费，修复后的整轮下降
幅度必须由下一次真实端到端运行测量，不能用单独 verifier 调用代替。

修复后的本地门禁为 `154 passed` 聚焦回归和 `2282 passed, 21 skipped, 7 known fork
warnings` 全仓回归（119.45 秒）；Ruff、compileall 与 `git diff --check` 全绿。

## 2026-08-17 第十六至十九轮真实语义闭环反证

四轮继续使用全新的隔离 cron fixture、DeepSeek V4 Flash、相同任务、auto 权限和
20-turn hard cap。这里记录的是原始运行结果；后续源码修复没有覆盖或美化这些 Session。

| Session | coding / sidecar calls | Provider tokens | 独立验收 | 终态 |
|---|---:|---:|---|---|
| `semantic-closure-sixteenth-real-20260817` | 19 / 4 | 809,896 | 114 passed，但 `FRI-MON/2` 错为 `[0,1,5]` | `completed` |
| `semantic-closure-seventeenth-real-20260817` | 20 / 2 | 919,339 | 99 passed；名称环绕步长正确，但数字 `5-1` 被错误放宽 | `max_turns` |
| `semantic-closure-eighteenth-real-20260817` | 16 / 0 | 504,003 | 77 passed / 12 failed，CLI 子进程找不到 package | `max_turns` |
| `semantic-closure-nineteenth-real-20260817` | 15 / 0 | 595,726 | 99 passed；数字兼容正确，但名称环绕步长仍错 | `max_turns` |

### TP-035：语义 Sidecar 对环绕范围修复出现确定性盲区

- 优先级：P0
- 状态：verify
- 第十六轮的 suite 全绿与 Sidecar accept 都没有发现“先按原始 5,6,7,0,1 下标取步长，
  再把 7 归一为 0”导致重复别名参与步长的问题。第十七轮又证明反向风险：修正名称环绕时，
  把旧的所有 descending numeric rejection 移到 `allow_wrap` 分支，会静默放宽数字 API。
- 已修复：compatibility deterministic review 新增两类源码证据规则：别名去重必须发生在
  index-based step 之前；旧 descending guard 不能被字段级 positive wrap gate 吞掉。合法的
  ordered `dict.fromkeys` 归一不会误报。确定性风险与模型审查理由合并；存在确定性反例时
  本代直接 revise，不再浪费一次 LLM judge，下一代 clean diff 才启动语义模型审查。
- 仍保持 `verify`：规则已有单元测试和原 diff 回放，但没有用第五次付费重跑把 TP-025
  宣称为稳定通过。

### TP-036：Runtime 自己的验收命令污染 StallDetector

- 优先级：P1
- 状态：closed（源码与 trace 复核）
- 第十六轮的四次 stall sidecar 中有两次来自 Runtime 重复执行同一个 acceptance command，
  并非模型空转。第十七轮修复后该类误触发降为 0。
- Tool policy 现在只排除 call id 为 `verification-contract-*` / `verification-stage-*` 且 marker
  与 canonical contract/stage 同时匹配的 Runtime-owned call。模型仅复制
  `_nz_runtime_contract` 或 `_nz_runtime_verification_stage` 仍会进入 stall 检测和权限校验，
  不形成绕过。

### TP-037：package-root 诊断有分类但没有局部修复目标

- 优先级：P0
- 状态：fixed locally，待真实复测
- 第十八轮已识别 `subprocess_package_root`，但没有把失败 test helper 写入
  `recovery_repair_targets`；随后通用 `No module named` 又把 `needs_broad_exploration` 设为真，
  使 16-20 bounded emergency 不可用，Agent 在第 16 轮提前结束。
- Recovery 现在把已解析的 helper 设为 repair target；RuntimeState 对
  `subprocess_package_root` / `subprocess_workspace_drift` 的证据目标保持局部收敛，不再被
  通用 import 文本降级为广域探索。

### TP-038：未完成 Requirement 没有反馈给模型，MAX_TURNS 又透传假完成正文

- 优先级：P0
- 状态：fixed locally，待真实复测
- 第十九轮把 README 写到了 workspace 根，而契约要求 `cron_engine/README.md`。Runner 在
  turn 10、11、12 都正确判定 R5/R6 unresolved，却没有向下一次模型调用注入具体缺项；模型
  连续返回完成说明。turn 15 最终状态虽是 `max_turns`，headless text 仍保存了模型“全部完成，
  验收通过”的自报结论。
- `CompletionGate` 提示现包含 requirement id、description、status 和精确 expected artifacts；
  Natural Terminal Boundary 最多允许两次全局 synthetic repair reanimation，第三次仍无新证据
  就以 `completion_gate_reanimate_budget_exhausted` 事实终止，不再静默调用到第 15 轮。mixed
  deterministic/semantic ledger 不再因尚未达到 semantic-only 状态而绕过提示。
- 提示把主 Agent 可修复项与 Runtime-owned `semantic_review` 分区。第十九轮式 R5/R6 状态只让
  Agent 修 `cron_engine/README.md`；R6 明确等待独立 Sidecar，不再诱导模型为审查证据继续改
  parser。
- 所有 `max_turns` 正文改由 Runtime 生成，不再按 natural/tool boundary 分叉。摘要会检查
  当前代 `VerificationContract`：exact 已通过就如实写 passed，未通过才写 did not pass，
  并始终列出 unresolved requirement IDs。
- natural、streamed terminal 和 buffered terminal 都把配置的 turn cap 传给 Lifecycle；UI 在
  有事实正文时显示该正文，而不是覆盖成通用 `max_turns=N`。`max_turns` 的最后一条 durable
  Assistant message 同样被 Runtime 摘要替换，因此 resume 不会重新展示模型的假完成文本。

本轮聚焦回归在写入文档前为 `130 passed`，覆盖 Native Runner、CompletionGate、Sidecar、
Focused Tool Policy、Recovery 与 RuntimeState；Ruff、compileall 和 `git diff --check` 通过。
完整仓库随后为 `2304 passed, 21 skipped, 7 known fork warnings`（117.18 秒）。TP-025 与
TP-035 继续 `verify`，当前不能宣称长任务终端产品已经稳定。

### 2026-08-17 TP-038 bounded reanimation 补充验收

- 参照 InfCodeX `Runner.stopHookReanimateBudget=2`，CompletionGate 的两次 correction 预算由
  Runtime 全局持有，而不是依赖模型是否产生 mutation。第十九轮的“连续返回完成说明”模式在
  fake-provider Runner 中固定为 4 个 coding calls：首次工具回合、两次 correction、第三次
  事实终止；不会再消耗到名义 15-call SLA。
- 新增 mixed ledger 集成链：R5 缺文档时注入精确路径；修正后重新通过 exact acceptance；
  R6 转为 semantic-only；独立 verifier 写入当前代 evidence；同一个 Runner 最终以
  `semantic_review_and_ledger_satisfied` 完成。
- 聚焦回归 `221 passed`；完整仓库 `2310 passed, 21 skipped, 7 known fork warnings`
  （112.57 秒）。TP-038 保持 `fixed locally，待真实复测`，因为本轮仍未消费外部 Provider。

## 2026-08-18 第十九轮同 Session 续跑

本轮没有重跑原任务，而是在
`/home/pyh/test_nzcoder/.product-semantic-closure-nineteenth-WRtSUt` 继续 Session
`semantic-closure-nineteenth-real-20260817`，显式限制为 8 turns。真实运行 116.18 秒、8 次
coding calls、12 个 tool events、536,527 tokens，终态 `max_turns`。

产品正文的假完成问题已获得真实正证据：headless `text` 是 Runtime-owned
`Stopped at the work limit without claiming completion...`，没有保留模型旧的“全部完成”。Agent
实际生成的补丁也通过独立 `109 passed`，并通过 numeric descending、0/7/SUN、
`FRI-MON/2`、`FRI-MON/3` 和 month step 外部探针。但 Runtime 没有允许模型执行用户尾部声明的
精确 suite，因而不能把本轮记作 completed。

### TP-039：长 continuation 截断了验收命令，已关闭的 Broad Test 问题复发

- 优先级：P0
- 状态：fixed locally，待真实复测
- 真实证据：Lifecycle 保存的 `initial_task_text` 只有 300 字，结束于 `prese`；尾部测试要求和
  `python -m pytest -q cron_engine/tests` 消失。RuntimeState 因此得到
  `task_mode=unknown`、`wants_tests=false`、空 VerificationContract/TaskContract/Ledger；同一精确
  命令在第 6、7 calls 连续返回 `Broad test runner blocked`。
- 第二根因：即使不截断，旧英文 command regex 也会把后续句子
  `Do not claim completion...` 当成 pytest 位置参数，因包含非 test path 而丢弃整个 contract。
- 修复：Lifecycle contract input 保留完整真实 User turn；memory/retrieval 仍保留独立有界 query。
  VerificationContract 从候选中选择最长安全 pytest 前缀，所有目标都必须是 workspace-relative
  test scope，shell composition 和越界路径仍拒绝。
- 本地证据：相同 606 字 continuation 可恢复 `cron_engine/tests` scope、exact contract、test mode
  和带 compatibility semantic review 的 RequirementLedger；相关红绿链 79 项通过。

### TP-040：Tool-batch 终态正文覆写 tool-call owner，durable transcript 顺序不合法

- 优先级：P0
- 状态：fixed locally，待真实复测
- 真实证据：续跑 Session 的最后 Assistant 同时包含 Runtime 的 `max_turns` 摘要和
  `verify_changed_files` tool call；其后仍有该 call 的 Tool result 与 synthetic failure
  diagnostic。虽然用户看到的正文真实，但 resume/provider history 不应把终态正文附着到尚有
  后续结果的 tool owner。
- 修复：只有无 `tool_calls` 的 settled Assistant 能承载终态正文。tool-batch 边界会在所有 Tool
  results 之后追加独立 Runtime Assistant，并补齐 message identity、parent、timing、end state
  与 text part；原 tool-call Assistant 保持协议原貌。
- 本地证据：回归直接构造 `User -> Assistant(tool_calls) -> Tool -> synthetic diagnostic`，确认
  `max_turns` 后最后一条是无 tool calls 的事实 Assistant，原 owner content 仍为空。

TP-025 继续 `verify`：本轮每次 input 从约 40K 增至 64K，8 calls 已累计 536,527 tokens。
源码核对表明最近两个真实 User turns 的工具证据保护与 infcode-dev 相同，不能为了单次数字好看
直接裁掉第二近 turn；下一步需要专门设计并验证 terminal/max-turn continuation summary boundary。

### TP-041：未完成 Session 续跑会向 Provider 重放全部历史，token 成本失控

- 优先级：P0
- 状态：closed（大 Session 离线量化 + 真实 Provider 续跑）
- 真实证据：第十九轮同 Session 的 8-call continuation 累计 536,527 tokens，单轮 input 约从
  40K 增至 64K。817 KB durable Session 有 71 条消息；旧 provider projection 没有识别前一个
  `max_turns` 是显式续跑边界，因此每次都再次发送完整旧工具轨迹。
- 根因：普通 model-pressure compaction 与 unfinished-run resume 是两个问题。前者只有接近窗口时
  才应裁剪，后者在新的 User 接管未完成 Session 时就应使用一个有界、可审计的 handoff；继续改
  recent-two-turn 保护会破坏正常多轮证据，而不能正确表达 run boundary。
- 修复：`max_turns` / `interrupted` 的 terminal Assistant 持久化不超过 6,000 字的确定性
  `_nz_continuation`；下一条真实 User 触发 Provider-only prefix projection。summary 保留最新完整
  User instruction、contract/ledger、changed files、verification 和 next step，且不调用模型。完整
  transcript 继续留在 Session；正常完成、没有后续 User、空 User 均不启用。
- 安全与协议：旧 summary 的 `&<>` 全部转义，不能闭合 continuation 标签伪造当前指令；当前真实
  User 单独包在 authoritative block。projection 不生成孤儿 Tool message，也不修改 durable
  messages；ContextManager 不再 micro-compact 已隐藏 prefix。
- 离线验收：对同一真实 Session 投影后，Provider view 从 68 条消息/约 60,535 tokens 降为
  1 条/约 573 tokens，减少 99.05%；1,823 字 boundary 仍保留用户验收命令尾部。
- 真实复测：小型 Session 续跑 trace 明确记录 `dropped_messages=3`、summary 717 字和 2 条 API
  messages（system + bounded User）；Provider 一次成功并执行 `read_file`。这证明真实产品链消费了
  boundary，而不只是 helper 离线返回正确结果。TP-025 的跨任务稳定性结论仍不变。

### TP-042：Hard-cap Assistant 在 DeepSeek thinking wire 上缺少 `reasoning_content`

- 优先级：P0
- 状态：closed（真实 Provider 复测）
- 真实证据：continuation smoke 首次请求在 357 ms 返回 DeepSeek 400，usage 全 0。trace 顺序为
  `llm_request -> max_steps_prompt_injected -> model_call client_error`；Session 正确保留 API error，
  没有把 0 token/`max_turns` 误报为成功。
- 根因：model-aware Session projection 先运行，Runner 后追加 role=Assistant 的
  `_MAX_STEPS_PROMPT`，所以最终 wire 存在一条没有 `reasoning_content` 的 late message。此前只测
  durable history 和 synthetic tool Assistant，没有覆盖 projection 之后的 Runtime 注入。
- 修复：OpenAI-compatible 最终 request normalization 对
  `preserve_reasoning_content=True` 的每条 Assistant 复制并补齐该字段，空 reasoning 使用 `""`；
  不修改调用方 messages，也不影响未 opt-in 模型。这与 InfCodeX 最终 wire serializer 的职责一致。
- 真实验收：修复后同 Session 续跑一次 Provider completed，无 400/retry，input 12,559、output 45、
  reasoning 29；`read_file README.md` 成功读出精确 marker。显式 1-turn 上限后的 `max_turns` 是测试
  设计结果，不是 Provider 或 continuation 失败。

### TP-043：低压力会话暴露全部 63 个工具，trace 又漏算 schema 成本

- 优先级：P1
- 状态：closed（真实 Provider A/B）
- 真实证据：A269 的单文件读取只有 2 条 model messages，trace 估算 3,112 tokens，Provider 却报告
  12,559 input。对实际 registry 做离线分解得到 63 tools、40,240 schema chars、约 10,063 tokens；
  message + schema 与 Provider 数值一致，排除 continuation 历史仍被重发的假设。
- 根因：现有 progressive exposure 只在 context window 接近压力时启用；1M window 让成本很高的
  schema 被视为“无压力”。此外真实 deferred candidate 为 20，小 MCP 保护移回一个后只剩 19，
  又低于默认 20 的最小阈值，最终回退为全量。streaming 路径还直接读取 host specs，绕过 exposure。
- 修复：schema 超过 6K 本身即可触发延迟暴露，默认最小规模降为 8；只延迟已有审计集合，核心编码
  工具和 `tool_search` 常驻，Session 解锁语义不变。streaming/non-streaming 统一消费 run-scoped
  exposure。`llm_request` 新增 message/tool/total 三类 token 估算与可见工具数量。
- 真实 A/B：相同 Session 和同类 read-only prompt 下，input 12,559 → 10,190，下降 2,369
  （18.9%）；Provider 均一次 completed、无 API error、同样执行 `read_file` 并读到 marker。新 trace
  估算 10,765，对实际 10,190 的误差约 5.6%。当前 10K 是完整 coding system + 46 个核心/直接能力
  的基线，不再属于历史重复；是否继续做 task-specific 工具裁剪应由多任务召回率 A/B 决定，不能
  只凭这一个简单任务继续缩工具。

### TP-044：完全隐藏 deferred schema 会让真实模型绕过专用工具并耗尽回合

- 优先级：P0
- 状态：closed（多类型确定性矩阵 + 真实 Provider 前后对照）
- 触发背景：TP-043 的 18.9% 成本下降是在单文件 `read_file` 任务上取得；该工具属于 resident
  surface，无法证明 repo/workflow/verification 等 deferred 能力仍可达。8 类本地检索矩阵虽为
  8/8 命中、最差 rank 2，但这只验证搜索算法，不验证模型会先调用 discovery bridge。
- 真实失败：修复前 workflow history 任务没有选择 `tool_search` 或 `workflow_runs`，而是连续使用
  `list_directory`、`glob_search`、`bash`、`read_file`。closure reserve 又拒绝后 3 次调用，最终
  4 个 Provider calls、6 个工具调用、`max_turns`，没有回答 workflow run history。
- 根因：NZ-Coder 把 InfCodeX progressive disclosure 错抄成“删除整个工具 schema”。InfCodeX 的
  默认 deferred path 实际保留 name + parameters，只将 description 替换为包含 exact
  `tool_search` 路径的 compact hint；portable bridge/native deferred 可用时才进入更激进隐藏模式。
- 修复：19 个 audited deferred tools 在成本门触发后仍可调用，参数 schema 不变，长描述换成短用途
  hint；`tool_search` exact/keyword unlock 和 Session 隔离不变。实际 63-tool schema 的本地估算为
  9,532 → 9,245（约 3.0%），8 类 exact unlock 两回合总成本下界仍为正（1.3%–2.6%）。这是用较小
  节省换回可靠 reachability 的明确产品取舍。
- 真实验收：修复后相同 prompt 在 2 个 Provider calls 内完成，唯一工具调用为
  `workflow_runs(action=list, limit=50)`，无 policy block、无文件变更、终态 `completed`。Provider
  usage 为 input 15,863、output 166、reasoning 277、cache-read 8,704；不能把不同 cache 命中率直接
  当纯 schema A/B，但 Provider calls 4→2、工具调用 6→1、失败→完成是互不含糊的行为改善。
- 防回归：Core Capability Case D 现在同时检查 8 类 recall、最差 rank、unlock 恢复和 token 下界；
  Tool Platform 测试检查 hint 初始可调用、参数不变、解锁后完整描述恢复及跨 Session 隔离。

### TP-045：Subprocess 工作目录诊断只给概念，模型会再次猜错父目录层级

- 优先级：P0
- 状态：closed（确定性回归 + 真实 Provider 复测）
- 真实失败：`tp025-current-real-20260824` 在修完 cron 功能后把
  `cron_engine/tests/test_cli.py` 的硬编码 cwd 改成两层 `dirname`，实际落到包目录而不是
  workspace root。独立 suite 为 `86 passed, 13 failed`，16 次 Provider calls、27 次工具后
  `max_turns`。旧诊断虽说了“不要猜 parents index”，但没有提供可直接执行的表达式。
- 修复：Runtime 根据失败 helper 的 workspace-relative 路径计算精确的
  `Path(__file__).resolve().parents[N]`，同时写明它解析到的真实 workspace；
  `subprocess_workspace_drift` 和后备 `subprocess_package_root` 都消费同一提示，禁止再翻译成
  猜测的 `dirname` 链。
- 受控真实验收：在三层 helper 中故意放入 `parents[1]`，DeepSeek 首次 pytest 失败后
  收到精确 `parents[2]`，随后只读该 helper、修改一行并重跑通过。显式 4-turn 烟雾为
  4 calls、5 tools、37,320 tokens，exact contract 与独立 `1 passed` 都通过；终态
  `max_turns` 是这次人为 4-turn 上限且 semantic requirement 尚未由 sidecar 收口，不是 cwd 修复失败。
- 长任务复验：后续 `tp025-final-real-20260824` 在 exact suite 首次暴露旧硬编码 cwd 后，
  模型直接使用诊断给出的 `parents[2]`，第二次为 `93 passed`，证明修复不仅在小 fixture 生效。

## 2026-08-24 TP-025 当前 Runtime 长任务重跑

本轮使用同一 cron 多文件任务做了修复前/后两次真实 Provider 运行，并保留原始
Session 和 trace：

| Session | Provider calls | Tools | Provider total tokens | 独立验收 | 终态 |
| --- | ---: | ---: | ---: | --- | --- |
| `tp025-current-real-20260824` | 16 | 27 | 553,527 | `86 passed, 13 failed` | `max_turns` |
| `tp025-final-real-20260824` | 18（17 coding + 1 sidecar） | 27 | 607,150 | `93 passed` | `completed` |

- 首轮失败的核心不是 cron 实现，而是 CLI 测试 helper 的可移植 cwd 修复猜错一层。
  TP-045 把该恢复路径改为确定性提示后，第二轮代码、用户精确 suite 和独立 suite 全部通过。
- Planner TaskContract 已知道 5 个 expected artifacts，旧 retrieval policy 却仍把不带 `.py` 字面的
  中文任务标成 `unknown-location`。现在 contract artifact 作为受校验的 known paths 进入首轮路由；
  第二轮 trace 为 `known-location/read/read_file`、confidence 0.97，没有起手扫整个仓库。
- 正确的语义探针覆盖名称单值、大小写、名称/数字混合列表、升序范围、步长以及 0/7/SUN
  别名，为 `9/9 passed`；CLI 实际 `python -m cron_engine parse` 也返回正确展开值。
  `FRI-MON` 与旧数字 `5-1` 同样是降序范围，均拒绝是保持现有 API 语义，不记为漏实现。
- 正确性已收口，但数值仍超过 TP-025 的 `<=15 coding calls / <=25 tools` 门槛。首轮仍有
  11 次读/定位（包括猜错根目录 `README.md` 后再 glob），且模型另外做了 4 次 todo 状态更新。
  因此 **TP-025 继续 `verify`**：可以说长任务这次真实完成，不能说成本和调用次数已稳定达标。
- 本轮没有因 607K 累计 tokens 就盲目删除当前 run 的工具证据。每轮 provider view 仍在约 40K
  输入量级，未接近模型窗口；InfCodeX 的 microcompaction 默认关闭，infcode-dev 也在 token
  pressure 下才裁旧工具输出。当前成本主因是调用轮数与每轮 system/tool schema 基线，不是上下文溢出。
- 最终门禁：本轮 recovery/retrieval/runtime 聚焦回归 `130 passed`；完整仓库
  `2328 passed, 21 skipped, 7 known fork warnings`（142.83 秒）；Ruff、compileall 和
  `git diff --check` 全部通过。

## 2026-08-24 TP-025 DeepSeek 工具协议与重复读取收敛复测

在 A272 之后继续用同一 cron 多文件任务、同一 DeepSeek 模型和相同 15-turn 上限做可比运行。
本轮没有调整 TP-025 的统计口径，也没有把 sidecar 调用藏进 coding calls：

| Session | Coding + sidecar calls | Tools | Provider total tokens | 独立验收 | 终态 |
| --- | ---: | ---: | ---: | --- | --- |
| `tp025-contract-owner-real-20260824` | 15 + 1 | 28 | 683,602 | `95 passed`、semantic `9/9` | `completed` |
| `tp025-readcache-real-20260824` | 15 + 1 | 27 | 512,532 | `87 passed`、semantic `9/9` | `completed` |

- 前一轮 trace 里 DeepSeek 在同一响应连续发出 3 个 `apply_patch`，每个都漏掉
  `changes[].path`；canonical schema 的 nested `required` 实际存在，因此这不是 schema
  被适配器误删，而是该模型对“数组对象内重复必填字段”的遵循不稳定。InfCodeX 的
  `multi_edit` 使用顶层单文件 `path` 加 nested edits。NZ-Coder 现在只对 DeepSeek family
  投影同样的单文件扁平契约；canonical handler 和 GPT/OpenAI-facing 多文件契约保持不变。
  真实复测中 6 次 `apply_patch` 全部带顶层 path，`requires path` 从 3 次降为 0。
- InfCodeX FEATURE_177 的 per-task read state cache 被内化到现有 `ToolExecutor` 生命周期：
  只对成功的文本 `read_file(path, offset, limit)` 记录 mtime/ctime/size，不缓存目录、图片、
  PDF/DOCX 或失败结果；相同区间且文件未变时返回短提示。写入目标、文件外部变化、上下文
  compaction 和新 Agent run 都会使对应证据失效，状态属于 Agent 实例而不是模块全局。
  `NZ_READ_DEDUP_ENABLED=0` 可即时关闭。真实 trace 有 1 次重复全文读取命中短提示，并引导
  下一次改用正确的 offset/limit。
- todo 已由 Runtime TaskContract ledger 接管，真实 trace 中 todo 调用为 0。与前一轮相比，
  read_file 13→11、apply_patch 8→6、工具总数 28→27。累计 tokens 683,602→512,532，下降
  25.0%；但模型本轮 output/reasoning 也更短，不能把全部降幅都归因于一条 read-cache hit。
  可确定的因果证据是重复全文结果没有再次进入上下文、3 个 malformed patch 消失。
- 本轮剩余主要浪费转移到 Terminal Boundary：模型从旧 `test_cli.py` 抄出工作区外绝对 cwd，
  随后又从 package 目录运行 `python -c`，形成 policy deny、`ModuleNotFoundError`、`pwd`
  三步。Recovery 现在分别给出精确 active workspace root，并要求移除 `cd`/`workdir`；当
  `workdir` 恰好等于检测到的 package 目录时，明确从其父级 workspace root 重跑且禁止安装包、
  读源码或再次 cwd 探测。该项已走确定性红绿测试，未再购买第三次 500K-token 完整重跑。
- 正确性由产品外部独立执行：`python -m pytest -q cron_engine/tests` 为 `87 passed`，名称单值、
  大小写、混合列表、范围、步长及 0/7 Sunday 兼容的语义矩阵为 `9/9`，CLI 实际展开正确。
  本轮 15 次 coding calls 已达到 call 门，但 27 tools 仍高于 25，因此 **TP-025 继续
  `verify`**；不能用一次 token 下降宣称成本已跨任务稳定达标。
- 最终全仓门禁为 `2341 passed, 21 skipped, 7 known fork warnings`（143.14 秒）；Ruff、
  compileall 与 `git diff --check` 均为 exit 0。

## 2026-08-24 TP-025 语义 Oracle 与顺序编辑闭环

上一轮 `tp025-boundary-real-20260824` 虽然把工具数降到 23，但在第 15 个 coding call
才第一次运行完整测试，最终为 `max_turns`。产品外部验收是 `12 failed, 88 passed`：其中
8 项来自旧 CLI helper 的硬编码 cwd，另外 4 项是 Agent 自己新增的错误测试——把单值步长
`JAN/2` 当成通配步长、把旧数字 API 拒绝的降序范围 `FRI-SUN` 当成环形范围，并误读 cron
星期编号和既有 day-of-month/weekday AND 语义。

trace 还显示两次无效 patch。第一次在同一个顺序 `changes` 数组中让第二个 `old_text`
覆盖第一个变更已经消费的区域；第二次为了在测试文件末尾追加内容而猜测不精确 anchor。
这两项都不是模糊匹配问题。InfCodeX `multi_edit` 的顺序编辑合同明确要求后续 anchor 不得
与前序变更重叠，追加场景则应使用 append/anchor 能力。

本轮按该根因做最小修复：DeepSeek-facing `apply_patch` 描述明确顺序、非重叠和
`op=append`；Coding prompt 要求新增 syntax alias/named form 时先执行等价的现有数字形式，
以观察结果作为测试 oracle，保持原有拒绝行为，不发明新的 range/step/scheduler 语义。
canonical/GPT 工具 schema、handler 接口和 cron fixture 均未硬编码特殊规则。

修复后的真实 Session `tp025-oracle-real-20260824` 在全新 59-test 基线副本中完成：

| Coding + sidecar calls | Tools | Provider total tokens | Agent 内验收 | 产品外独立验收 | 终态 |
| ---: | ---: | ---: | --- | --- | --- |
| 13 + 1 | 21 | 422,892 | `90 passed` | `90 passed`、semantic `10/10`、真实 CLI parse 通过 | `completed` |

- 21 个工具为 11 `read_file`、2 `list_directory`、6 `apply_patch`、2 `bash`；所有 patch
  一次解析并执行成功，没有 overlapping/missing-anchor/schema 错误，也没有 todo 调用。
- 第一次整目录 pytest 暴露旧 helper cwd 漂移后，Runtime 诊断精确指向
  `Path(__file__).resolve().parents[2]`；Agent 只读目标文件、修改一处并立即重跑为 90 通过。
- 独立语义矩阵覆盖名称单值、大小写、名称/数字混合列表、升序范围、范围步长、SUN 与
  0/7 等价、scheduler 名称/数字等价和降序范围保持拒绝，共 `10/10`。
- 该结果同时满足 TP-025 原始验收的 `<=15 model calls / <=25 tools`、外部测试全绿和最终
  证据完整，因此本项关闭。单次成功不外推为所有仓库的固定 token 成本；当前仍有 62 个
  Provider-visible tools、每轮约 9.6K schema tokens，作为后续独立的 Tool/Context Scale 议题处理。
- NZ-Coder 自身最终质量门为聚焦 `59 passed`、Ruff/compileall/`git diff --check` 通过，
  完整仓库 `2342 passed, 21 skipped, 7 known fork warnings`（142.69 秒）。

### TP-046：Task 无关工具仍占满 Provider schema，动态撤回又不能阻止旧工具调用

- 优先级：P1
- 状态：closed（确定性可达性矩阵 + 三轮真实 Provider 反证/复验）
- 触发背景：TP-044 为避免工具不可达，把 19 个 deferred 工具恢复为 compact callable hint；真实
  coding catalog 因而仍有 62 个 Provider-visible tools、约 9.4K schema tokens。普通代码修改每轮
  都为 workflow、memory、web、MCP/LSP、project creation 等无关能力重复付费。
- 参考实现：InfCodeX 的 exposure planner 区分 resident、hint、portable bridge、native deferred
  与 hidden，并由 `tool_search/tool_describe/tool_call` 保证隐藏能力可重新到达；infcode-dev 则按
  Agent、模型和 Runtime 条件解析工具并屏蔽不合适或重复能力。NZ-Coder 沿用自己的 registry 和
  `tool_search`，没有复制 TypeScript Runtime，也没有按关键词删除核心读写工具。
- 修复：run-scoped exposure 读取真实首个用户任务，将 audited 稀有工具划分为 workflow、memory、
  web、MCP、LSP、repo intelligence、planning、project creation 与 orchestration family。核心
  read/edit/write/search/verification/process/task bridge 始终 resident；匹配 family 保留 compact
  callable hint，不匹配 family 从本轮 Provider schema 隐藏但仍存在于 catalog，可由
  `tool_search(select=...)` 解锁并在下一轮恢复完整 schema。空任务、无法分类和显式“所有工具”请求
  回退到旧的 all-hints 行为，避免未知意图下静默丢能力。
  已连接且小于 8 项的动态 MCP surface 继续直接可见；它不是静态稀有工具，不能因当前 prompt 未写
  `MCP` 就隐藏其唯一远端能力。
- 可观测性：每轮写入 `tool_exposure_planned`，包含 visible/deferred/hidden names、前后估算和节省量；
  生产 coding catalog 的离线门为 63 tools / 9,532 tokens → 23 visible / 3,850 tokens，40 个隐藏，
  核心工具全部可达。真实 DeepSeek 首轮为 61 个可用 catalog 项中的 22 visible、39 hidden，
  9,290 → 3,730 schema tokens。
- 首轮真实反证 `a275-tool-slicing-real-20260825` 没有出现缺失工具或 search failure，但 15 calls / 25
  tools 后为 `max_turns`，独立 suite `1 failed, 87 passed`；失败来自 Agent 新增的错误排序断言，不能
  用 schema 下降冒充完成。trace 同时暴露编辑前必拒绝 Bash、非 Git `verify_changed_files` 仍调用
  `git diff`、exact patch 连续锚点说明不足三项 Runtime 浪费。
- 第二轮 `a276-tool-slicing-followup-20260825` 独立 `88 passed`，但验收通过后的 `diff_status` 把
  satisfied requirements 重新降为 candidate，终态仍为 `max_turns`。RequirementLedger 现在把
  同 mutation generation 的 changed-path refresh 作为幂等观察；非 Git verifier 改用本 Session
  ChangeTracker；DeepSeek patch contract 明确 `old_text` 必须包含全部 intervening lines。
- 最终真实 Session `a277-tool-slicing-closure-20260825` 为 15 coding + 1 stall sidecar、26 tools、
  `completed`；Agent 内与产品外均为 `102 passed`，额外名称/数字/范围/步长/0/7 语义矩阵 `12/12`。
  编辑前 4 轮 schema 均不含 Bash，首次 Bash 仅在源码 mutation 后出现；全程无 dispatch/schema/
  permission failure。两次 nonzero pytest 是实现回归被真实发现并在同 run 修复。
- 成本结论保持克制：最终 run 总计 438,126 provider tokens，并未优于所有历史随机样本；可直接归因
  的改进是首轮 schema 每次约少 5.56K tokens 和无关工具不进入 wire。15 轮上下文重放、11 次读取与
  两轮测试修复仍是后续独立议题，不能宣称总体 token 已跨任务稳定降低。
- 首次全仓门禁为 `1 failed, 2350 passed, 21 skipped`：失败精确指出单个已连接 MCP tool 被 task
  family 隐藏。Planner 随后恢复 small-MCP direct surface，原集成测试与 Tool Platform 回归
  `17 passed`；最终全仓为 `2351 passed, 21 skipped, 7 known fork warnings`（145.53 秒），Ruff、
  compileall 与 `git diff --check` 全部通过。

### TP-047：长任务重复发送已确认写入 diff，1M 窗口下不会触发常规 compaction

- 优先级：P1
- 状态：closed（确定性协议测试 + 历史反事实回放 + 真实 DeepSeek 闭环）
- 现象：A277 虽然最终完成，但 15 个 coding calls 累计 428,732 tokens；最后一次 provider history
  约 33.7K tokens。成功 write tool 的完整 diff 在模型已消费后仍随每个后续请求重放，窗口没有接近
  1M，因此 pressure-only semantic compaction 永远不会介入。
- 参考边界：InfCodeX 明确默认关闭按年龄 microcompaction，prune 前抽取 artifact/file/search/bash
  evidence，且保护控制面、MCP、goal/todo 和 repo intelligence；infcode-dev 同样保护 recent tail 和
  provider capacity。因而没有把“单用户 turn 内旧结果全部清空”作为修复。
- 修复：成功 file-write result 只有在其后出现带 `_nz_usage` 的真实 provider assistant 后，才在下一次
  provider projection 中变为包含资源路径和 mutation generation 的短 receipt。Runtime-synthetic
  assistant 不构成确认；最新批次、失败、verification、read、attachment 和未知结果不受该规则影响。
  Durable Session/trace 保留完整内容，OpenAI/Anthropic/DeepSeek 所需的 tool pair 结构不变。
- 可观测性：`context_evidence_projected` 分别报告 `acknowledged_write_results_compacted`、
  `acknowledged_write_tokens_saved`、superseded read/failure counts 与 savings。A279 最后可观测投影为
  7 个 acknowledged writes，且 Session 中 7 个完整 write results 仍共 33,000 chars。
- 真实验收：无效的 A278 `acceptEdits`/EOF 权限轮被单独记录，不计成功证据。A279 使用全新 59-test
  fixture 与 headless `auto` 权限，15 coding + 1 sidecar、23 tools 后 `completed`；Agent exact
  acceptance 与外部 suite 都是 `94 passed`，semantic `12/12`。同 Session 反事实投影显示 15 轮
  累计少重放 48,332 history tokens，峰值 32,537 → 25,009；没有 replay 400、孤儿 tool result 或
  重读风暴。
- 最终门禁：`2355 passed, 21 skipped, 7 known fork warnings`（130.02 秒），全仓 Ruff、compileall
  与 `git diff --check` 通过。

### TP-048：1M 物理窗口掩盖历史重复计费，旧 reasoning/tool arguments 长期重放

- 优先级：P1
- 状态：closed（源码边界审计 + TDD + A279 反事实 + 真实 DeepSeek continuation）
- 现象：A279 在 A276 已压缩确认写回执后，后半程 provider-visible history 仍稳定在 22–25K
  tokens；其中旧 reasoning 约 10.7K、旧 tool-call arguments 约 5.3K。DeepSeek V4 的 1M
  物理窗口使 85% preflight 永远不会在普通 15-turn 编码任务中触发，因此“没有 overflow”不等于
  “成本正常”。
- 参考边界：InfCodeX `microcompaction.ts` 明确保留 thinking，因为 Kimi/同类 interleaved
  reasoning 协议可能在字段被清空后返回 400；`compaction.ts` 把 assistant tool-use 与相邻 result
  当作原子块，再进入 rolling semantic summary。infcode-dev `provider/transform.ts` 同样为每条历史
  assistant 补 reasoning 字段，`session/compaction.ts` 保护 recent tail。因而没有直接删除旧 reasoning，
  也没有把历史 tool arguments 改成不满足 schema 的伪参数。
- 修复：`PromptBudget` 新增独立 replay-cost 边界；默认
  `NZ_CONTEXT_REPLAY_COMPACTION_TOKENS=24000`，设置 `0` 可禁用。阈值只统计 provider projection 后的
  conversation history，不含 system prompt、instruction 和 tool schemas。超过阈值时复用现有语义摘要、
  transcript archive 和最多 8K recent tail；trace/marker 使用 `trigger=replay_cost, overflow=false`，不把
  成本控制伪装成物理溢出。
- 切分纠偏：A279 只有一个真实用户消息，其余 user 角色是 Runtime diagnostics。旧切分器会在这种长
  Agent turn 中留下空 tail，并把最新写入也全部摘要；同时 durable `_nz_parts` 会被错误计入 tail
  预算。现在即使只有初始 human turn，也会从合法 assistant/user 边界保留 provider-visible suffix，
  并在预算中排除 durable-only metadata。A279 首次触发时摘要前 15 条、保留最近 3 条
  （diagnostic + assistant write + tool result），tail 约 3,821 tokens。
- 真实验收：用 A279 前 18 条真实 Session 消息调用 DeepSeek V4 Flash。摘要输入估算 18,974 tokens，
  14.62 秒得到 4,775-char anchored summary；压缩后 provider history 为 5,090 tokens。随后携带保留的
  assistant tool-call/result 发起真实 continuation，prompt usage 5,355、completion 17，返回
  `CONTEXT_OK`，无 400、孤儿 tool result 或 reasoning replay 错误。
- 成本反事实：把该真实 summary 大小代回 A279 后续 9 个 coding calls，累计少重放 142,960 history
  tokens，峰值 25,009 → 10,490；扣除约 19K summary input 和约 1.2K summary output 后，净节省仍约
  12 万 tokens。该数字是同一 Session 前缀反事实，不外推为 SWE-bench 总体成绩。
- 最终门禁：首次全量暴露旧测试 Host 缺少 optional replay projector 的 1 项 adapter 回归；兼容层改为
  显式 `None`（禁用成本触发），没有错误回退到 full-request estimate。最终
  `2363 passed, 21 skipped, 7 known fork warnings`（142.85 秒），全仓 Ruff、compileall 与
  `git diff --check` 通过。

### TP-049：验收命令被误解为测试开发要求，完成后反而篡改用户禁止修改的测试

- 优先级：P0
- 状态：closed（真实失败轨迹 + 策略硬约束 + 五任务真实复测）
- 真实失败：Session `a280-provider-turn-1` 的用户任务明确“不修改测试”，但旧规则仅因文本包含
  `pytest` 和 `tests/test_text_utils.py` 就生成 R2“Add or update test coverage”。源码修复在第 3 轮
  exact `4 passed`，第 4 轮模型也已 final；CompletionGate 却因 R2 unresolved 强制继续，第 6 轮
  Agent 修改测试并新增用例。最终 7 coding + 1 sidecar、64,201 tokens 和 5 passed 不能掩盖约束违规。
- 新 Provider Turn Ledger 使链路可直接复原：`initial_investigation → investigation/mutation →
  verification → final_answer → requirement_repair → test mutation → verification`。旧 trace 只有
  `purpose=coding`，无法把额外三轮准确归因给错误 contract。
- 修复：test mutation intent 改为显式动作识别，不再把“run pytest”当成写测试；中英文测试不可变
  表述写入 `TaskContract.constraints`、RuntimeState 与 prompt。同时 Tool Runtime 的同步/异步唯一分发
  路径增加 `task_constraint` guardrail，结构化文件工具尝试写测试路径会在 dispatch 前失败，不能只靠
  模型遵循文字。
- 同任务全新 Session `a281-provider-turn-1` 为 3 coding calls、4 tools、22,115 tokens，只改源码，
  外部 4 passed；后续 query/TTL/三模块 queue/safe-path 四项也全部零测试改动、外部验收全绿。五项主
  calls 为 3/3/3/5/3，所有终态都是当前代 exact acceptance 后的 tool-boundary direct completion。
- 验证：轮次/Runner/ledger 聚焦 198 passed，TaskPolicy/TaskContract/RuntimeState/ToolPolicy/Runner
  聚焦 108 passed；完整仓库 `2375 passed, 21 skipped, 7 known fork warnings`（136.40 秒）；Ruff、
  compileall 与 `git diff --check` 全部通过。

### TP-050：严格SWE验证、Sidecar与仓库准备互相放大轮次和成本

- 优先级：P0
- 状态：code-fixed（真实A347 trace定位 + 确定性回归；等待下一批付费实例量化）
- 现象：12题开发窗口中，10项Agent运行累计205次coding请求、32次辅助sidecar请求和约366万token；
  出现无关required targeted test、不可修复环境错误反复进入repair、Sidecar accept后被strict gate复活，
  以及同仓库重复远程clone/TLS失败。内部终态与非空patch均未经过官方harness，不能算成绩。
- 根因：NZ在借鉴InfCodeX Sidecar时又增加了strict generation gate，却仍保留Sidecar-first组合；验证规划
  把通用Repo Intelligence边误当成最高相关测试；环境分类没有strict checkout所有权；repo cache只有
  consumer没有producer。这是多来源架构组装的边界问题，不是继续复制模块名就能解决。
- 修复：path-affine targeted排序；strict-only且changed-file-aware的缺依赖/pytest宿主冲突分类；pending
  deterministic verification期间Sidecar provider零调用；首次远程clone原子发布bare mirror并保留失败
  回退。A349继续补上environment authority注入、同命令失败诊断去重、重复implementation拒绝的二次
  terminal、8KB/12KB Sidecar diff证据预算，以及Pytest `testing/`测试目录识别。没有放宽联网、安装、
  Git历史、测试范围或写权限策略。
- 验证：A348相关组合`255 passed`；A349扩大组合`318 passed`；当前完整仓库
  在A350后为`2573 passed, 21 skipped in 189.35s`，Ruff、compileall与`git diff --check`通过。A347的5086字节
  `pytest-5103` patch现可完整进入Verifier；`pytest-5221`状态回放在第6次调查后进入implementation phase。
  另外5个历史窄pytest输出管道会被安全规范化，src-layout strict测试会消费checkout源码而非主机同名包，
  Provider schema也不再推荐被禁止的安装操作。这些是离线反事实，不是新Provider实测。真实token与完成率
  改善仍需下一次固定小批量复测，当前不标记为实测closed。

#### A351补充：同一失败与同一accepted patch被重复消费

- 原始A347中55次failure diagnostic有44次只是把已具备可执行指引的policy/strict-shell结果再次包装；
  这类包装现被去掉，但11次真实command failure及Doom-loop、编辑定位等专用恢复仍完整保留。
- 22次Sidecar provider调用中，18次为accept；其中11次accept之前没有新文件写入，属于同一patch证据的
  重审。Sidecar现对成功accepted evidence做mutation-scoped缓存，并用User要求、contract、diff和风险事实
  防止跨任务或跨修改误复用。按历史顺序反事实为22次降至最多11次，不宣称已产生真实账单节省。
- 完整回归更新为`2578 passed, 21 skipped in 238.38s`，静态门禁通过。TP-050继续保持`code-fixed`而非
  `closed`，直至下一批真实Provider轨迹证明轮次、token与patch质量同时改善。

#### A352补充：Issue证据路径被误当成用户硬目标

- A347的Runtime report显示`requested_paths`被traceback、pytest命令和最小复现文件占满，随后以
  “User named target files”高权限提示重新注入。该污染会影响closure路径许可和artifact合同，并可能把
  真正源码目标从5项有界列表中挤出。
- 修复后，mutation target、verification target和普通evidence path分别由`requested_paths`、
  `VerificationContract.targets`与Session/Repo evidence持有。TaskContract bootstrap只接受Runtime确认的
  literal mutation paths，同时保留基于裸业务surface的项目创建推断。
- 全仓回归更新为`2589 passed, 21 skipped in 243.50s`，静态门禁通过；TP-050仍等待新Provider窗口验证
  实际搜索轮次、误编辑率和token变化。

#### A353补充：Stall L2只收到空正文，且全部浪费在确定性闭环工具

- A347共10次stall L2，全部JSON空正文解析失败；6次由`diff_status`触发、4次由
  `verify_changed_files`触发，累计34.418秒且没有有效nudge。旧代码只复制了Sidecar模块形状，没有复制
  InfCodeX FEATURE_178/215的forced report tool协议，这是明确的源码级差距。
- Provider路径现强制`report_stall_judgment`并通过共享LLM Judge内核解析，兼容旧JSON、工具名轻微拼写
  错误和字符串布尔，超时/Provider错误继续fail-open。DeepSeek V4短判定关闭thinking以避免reasoning-only
  空content。
- 两个确定性闭环工具跳过付费L2，但没有跳过本地连续重复保护；第三次相同调用仍会被Doom-loop拒绝。
  A347控制流反事实因此消除10/10无效调用，不影响真实read/search卡死的语义二次判定。
- 全仓回归更新为`2591 passed, 21 skipped in 219.13s`，Ruff、compileall和diff check通过。TP-050仍为
  `code-fixed`，等待新的真实Provider固定窗口验证实际调用数、token和任务完成质量。

#### A354–A356补充：控制面用途混记、压缩调用漏账与Ctrl+C等待边界

- A347的32次可见辅助调用实际是22次Completion Verifier和10次Stall Judge，但旧adapter全部标为
  `stall_sidecar`。Verifier现使用独立`verifier` purpose，产品usage面板可以准确区分“完成审查”和“卡死
  判断”。
- A347还有11次自动压缩，旧`auto_compact`内部Gateway没有observer，导致trace、RuntimeState和headless
  usage均未记录。原237次可见调用应修正为至少248次逻辑调用，3,662,817 token只能视为下界。新压缩请求
  以`compaction`独立记录真实usage/attempts/latency，不通过估算补历史账。
- Context Runtime在async task取消时会设置压缩Gateway的cancel event，再等待工作线程结算并传播取消。
  这关闭了“用户已按Ctrl+C，但正好处于自动压缩时仍可能等待600秒”的终端生命周期缺口。
- 完整回归更新为`2595 passed, 21 skipped in 213.38s`，静态门禁通过。TP-050保持`code-fixed`，下一次
  真实窗口应同时检查`coding/verifier/stall_sidecar/compaction`四类purpose总账是否闭合。

### TP-051：可选Vision/Memory模型调用游离于产品账本和取消边界

- 优先级：P0
- 状态：code-fixed（源码审计 + TDD + 全仓回归；等待真实Provider观察）
- 现象：终端图片预处理与LLM记忆提取/重排会真实调用模型，但旧Gateway没有Agent observer；Memory还固定
  使用OpenAI-compatible bridge。更隐蔽的是Lifecycle先发`run_end`再执行Memory，即使后者开始记账，最终
  result仍可能漏掉调用。异步Memory Provider调用还能跨越run reset，把usage记到下一次用户请求。
- 修复：Vision/Memory复用当前Provider、capability snapshot与observer；终态事件延后到同步收尾完成；
  Provider-backed Memory禁止detach，纯本地写入仍可后台执行；Vision/Memory取消均下传Gateway。Memory
  cancel使用不可被普通提取fallback吞掉的内部信号，未完成窗口不写cursor。
- 附带修复：Focused Lifecycle的terminal admission evidence从host回写到run-owned state，防止第二次
  runtime summary被旧快照清空。
- 验证：相关生命周期/服务/Memory/Vision与产品路径组合先通过，最终全仓
  `2603 passed, 21 skipped in 219.68s`；Ruff、compileall和diff check通过。没有真实Provider运行，因此
  状态保持`code-fixed`，下一次终端/SWE trace需要确认`coding/verifier/stall_sidecar/compaction/vision/
  memory`按实际功能分账且`run_end`等于最终总账。

### TP-052：后台 Stall Sidecar 会越过 Run 终态并污染下一轮账本

- 优先级：P0
- 状态：code-fixed（并发确定性回归 + 全仓回归；等待真实Provider取消观察）
- 现象：L2 stall判断设计为后台线程，旧实现只在compaction时清历史，不向Provider调用传播取消。若
  `run_end`或下一轮reset先发生，迟到observer会把调用、token和耗时写入下一轮RuntimeState；两个
  Sidecar同时结算时，普通字典read-modify-write还可能丢一次增量。
- 修复：每个L2调用拥有cancel event；终态、下一轮初始化和显式`AgentLoop.close()`都执行
  `cancel_and_settle`。Gateway/LLM Judge合作取消并在发布终态账本前结算。RuntimeState Provider账本与
  typed RunContext usage/finish改为run-local lock保护，取消/失败/aborted终态不再启动新的Memory学习。
- 验证：确定性racing-dict测试在旧实现稳定丢增量，修复后并发账本、真实Gateway stall取消、终态顺序和
  close边界均通过；本轮完整仓库`2618 passed, 21 skipped in 213.17s`。

### TP-053：Provider token总数与美元成本在最终产品输出中不完整

- 优先级：P1
- 状态：closed（跨Provider归一化、fallback账本与Headless投影回归）
- 现象：Anthropic原生usage把cache read/write放在独立字段，但旧`total_tokens`只计算input+output，导致
  context pressure低估。Streaming失败转buffered fallback时，最终observer只记录后半段attempt和duration。
  同时Gateway已有逐调用cost，Runtime/headless却完全丢弃，未知价目看起来与零成本相同。
- 修复：normalized total至少等于互斥的input/output/reasoning/cache read/cache write之和；stream到
  buffered只发布一个合并后的finish记录；Runtime按purpose和provider/model汇总已知USD cost，并单列未知
  成本调用与provider/registry来源。旧版没有cost字段的headless payload保持兼容。
- 验证：OpenAI、Anthropic、Gemini、buffered、streaming、context budget、runtime持久化和headless JSON
  专项通过；完整仓库`2618 passed, 21 skipped in 213.17s`。

### TP-054：Repo Intelligence预热回调可在workspace关闭后复活Watcher

- 优先级：P1
- 状态：closed（失败预热与Watcher启动竞态回归）
- 现象：预热Future无论成功失败都调用`start_watching`。临时workspace已删除或SQLite目录已释放时，done
  callback抛出`sqlite3.OperationalError: unable to open database file`，形成后台日志污染并可能留下幽灵线程。
- 修复：只有`ready`预热结果才启动延迟Watcher；Watcher启动阶段重新确认workspace并捕获索引/文件系统
  失效，发布`repo_intelligence_watcher_failed`后保持`backend=none`，不从Future callback抛异常。
- 验证：失败预热、启动时索引失效、lease共享和完整Repo Intelligence服务18项通过；全仓回归通过且未再
  看到callback异常。

### TP-055：异常终态可持久化非法工具历史并泄漏运行时作用域

- 优先级：P0
- 状态：code-fixed（A380–A382 源码对照 + TDD；等待真实中断/Provider恢复观察）
- 现象：模型已发出一批 tool call、但分发或外层生命周期在结果齐备前异常时，旧 catch 路径可能把孤立
  assistant tool call 持久化。下一次 Provider 请求会因缺少相邻 tool result 返回协议错误。Native Runner
  还用 setter 修改 broad-test/declared-scope ContextVar，同一个 asyncio Task 继续执行时可继承上一轮权限；
  Session finalize 又在 durable save 之前把 RunContext 标为终态，瞬时存储失败后无法安全重试并可能重复计费。
- 参考：InfCodeX `history-cleanup.ts` 与 `catch-terminals.ts` 明确要求 catch 的第一步清理未配对工具块，
  再持久化可恢复快照，最后区分 interrupt 与 generic error；存储失败不得覆盖原始运行错误。
- 修复：新增纯函数协议清理器，只保留与紧邻结果一一匹配的非空 tool-call ID，同时保留 assistant 可见正文；
  resume、所有终态持久化以及每次 Provider wire 投影均执行防线。Native/Production host 用成对 context manager
  释放 broad-test、declared-scope 和 runtime override；legacy catch 与 native catch 统一结算 Session。
  Session finalize 改成“更新候选状态 → 原子保存 → 成功后 finish”，保存失败恢复 status/usage 并允许重试，
  原始异常不被 trace/save 次生错误覆盖。
- 数值边界：`ModelCall.timeout_seconds` 与 `ModelCallOutcome` 的 attempts、duration、first-token、cost 现在拒绝
  bool、NaN、Inf、负数或零次尝试，防止非法 Provider 指标进入 RuntimeState、JSON 和成本账本。
- 验证：覆盖部分批次保留、尾部孤儿移除、wire-only 修复不篡改 durable Session、同 Task 作用域恢复、
  fail-once 持久化后只计一次 usage，以及 legacy planning exception 终态。尚未执行真实 Provider、中断 PTY
  或 SWE 实例，因此这里记录为 `code-fixed` 而非 `closed`。

### TP-056：非有限数值和Python扩展JSON可击穿恢复、重试与远程协议

- 优先级：P0
- 状态：code-fixed（严格JSON、恢复fuzz和取消确定性回归；等待真实Provider/MCP观察）
- 现象：Python的`json.loads/dumps`默认接受并输出`NaN/Infinity`。损坏的RuntimeState、第三方Provider/MCP
  metadata或扩展事件因此能进入Session/Workflow JSON；后续`int(Inf)`在续跑、Bash分类、tool exposure或
  max-turns入口抛`OverflowError`。`Retry-After: inf/3600`还会让交互任务无限或超长睡眠，Ctrl+C只能等待
  sleep结束。事件在live内存中可用，但journal、SSE或headless消费者会在另一个边界失败，形成半闭环产品。
- 修复：控制状态和指标分层归一化；结构化输出与所有HTTP/MCP入站严格拒绝非标准常量，持久化/出站协议用
  共享`json_safety`去环并将非有限扩展值投影为`null`，再以`allow_nan=False`编码。Provider capability在
  创建client前验证。Retry-After只接受正有限值、header上限120秒，等待支持cancel event；工具timeout、
  continuation、context pressure和RunRequest数值边界完整捕获overflow。
- 验证：覆盖NaN/Inf RuntimeState与Memory恢复、严格Event JSONL/SSE、HTTP双向、headless JSONL、MCP请求、
  Child/Workflow持久化、结构化输出修复以及限流等待中取消。未进行真实网络或PTY实测，故仍为`code-fixed`。

#### A391–A395补充：恢复文件可降级风险、子Agent可越界resume、外部进程可先启动后失败

- Memory proposal过去直接接受磁盘risk/fingerprint；损坏或手工篡改文件能把非有限confidence配合low risk
  恢复。现在内容重新规范化、fingerprint必须一致，风险只能取重算值与持久化值中更高者。Lineage、call stack、
  child state、模型缓存和daemon state同步采用严格/原子JSON边界。
- 子Agent state中的worktree path过去只检查`exists()`。将其改成workspace外路径后，resume会在那里执行工具；
  fork与reviewed apply还会从该路径读取changed files。现在resume/fork/apply共用ownership authority，
  direct/git/copy分别验证父workspace或当前child专属managed目录；symlink、跨child和未知mode全部fail closed；
  WorktreeManager创建层也拒绝symlink target与不安全Git ref。
- HTTP/SSE/daemon/Provider/MCP/LSP入口过去各自用`max(..., float(timeout))`，`Infinity`可能进入socket、queue、
  thread或subprocess等待。现已在副作用前统一拒绝非有限/负/bool/超长值。Provider JSON/SSE还增加严格解析与
  64 MiB响应边界，避免第三方响应造成非标准状态或无界内存读取；`web_search`/`webfetch`非法timeout也在网络请求前拒绝。
- Workflow精确加载移除全目录扫描，project library阻止symlink逃逸并限制发现内存。最终全仓回归为
  `2831 passed, 21 skipped in 139.49s`。仍未进行真实Provider、PTY、第三方MCP
  或SWE验证，TP-056保持`code-fixed`而不是`closed`。

### TP-057：三回合任务只剩一个正常工具回合，读取链被过早收口

- 优先级：P0
- 状态：closed（真实Provider RED/A-B + 全仓回归）
- 现象：真实`openai-compatible/deepseek-v4-flash`只读任务显式使用`--max-turns 3`时，第1回合
  `glob_search README.md`成功；第2回合已经进入`closure_repair`，`read_file`被closure reserve拒绝；
  第3回合重复后以`max_turns`退出。任务未修改文件，但消耗3次Provider调用和17,511 tokens，连
  `glob → read → final`这条最短产品链都无法完成。
- 根因：预算控制器在3个nominal turns中仍预留2个closure turns，正常工作额度只有1。修复把预留量限制为
  `nominal_turns // 2`，从而让3回合边界拥有2个normal turns和1个closure turn；默认15回合与4回合边界
  仍保留原有双closure合同。
- 验证：TDD先稳定复现`normal_turns=1`；真实A/B会话`a396-provider-smoke-fixed-20260827`随后以
  `glob_search → read_file → final`完成并输出`# cron_engine`。trace中phase为
  `normal → normal → closure_repair`、`closure_tool_blocked=0`、终态`completed`、0编辑，账本为3次
  Provider调用和19,944 tokens。相关定向回归`167 passed`，fresh全仓
  `2832 passed, 21 skipped in 139.36s`，Ruff、compileall和diff check通过。
- 边界：这次实测关闭的是headless真实Provider短任务预算问题，不替代TP-016/TP-026的80×24交互PTY和
  Ctrl+C取消时延验收，也不构成SWE-bench成绩。

#### A397补充：Plan/取消真实PTY与终态notice持久化

- 真实Plan复测关闭TP-016/TP-026；10.97秒内完成长摘要审批，80×24下键盘可达末行，业务README字节不变。
- 真实取消首次复测虽在0.184秒结算、trace/session也正确，但暴露终态卡只闪现一帧：
  `TerminalRunRenderer`把`Run cancelled`写入临时`_run_output`，随后`surface.end_run()`立即清空它。
  TDD先得到idle屏仅剩`Session`的RED，再增加独立terminal notice通道；工具/stream临时卡仍在run end清除，
  不会与durable transcript重复。
- 修复后真实Session `session-20260827_120940-4707f0df`在0.204秒回到IDLE，完整重绘仍显示
  `Run cancelled`，trace无迟到事件。相关UI/renderer/CLI/headless/smoke组合`136 passed`，fresh全仓
  `2833 passed, 21 skipped in 139.61s`。
- 聚焦回归同时发现一条既有测试依赖其他文件先注册`bash`；测试现按项目副作用注册约定显式import目标工具，
  单独运行与组合运行一致。全量测试生成的4个零编辑夹具worktree已逐个核验并清理，数量恢复74，根分区约
  8.5 GB可用。
