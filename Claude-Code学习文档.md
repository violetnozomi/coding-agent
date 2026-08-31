# Claude Code 架构深度解析：面试学习文档

> 基于 Claude Code 源码（`Claude-Code-main/`）的深度阅读，聚焦 Agent Loop、工具系统、多 Agent 协同三个核心主题。

---

## 一、整体架构层次

```
用户输入
   ↓
QueryEngine.submitMessage()          ← 会话管理、transcript 持久化、USD 预算
   ↓
query() / queryLoop()                ← 核心 agent loop，while(true) 驱动
   ↓
claude.ts (API 调用层)               ← streaming、thinking、tool_use 事件流
   ↓
runTools() / StreamingToolExecutor   ← 并发工具执行
   ↓
Tool.call()                          ← 各具体工具实现
   ↓
tool_result 写回消息列表 → 下一轮
```

**关键设计**：整个调用链是 `AsyncGenerator`（`yield*` 链式传递），从 claude.ts 的 streaming 事件一路 yield 到 QueryEngine，再 yield 到 SDK 调用方。这意味着：
- 流式 token 对用户实时可见
- 工具执行结果即时插入对话历史
- 任意层都可以 `return` 终止循环（预算超限、max_turns 等）

---

## 二、Agent Loop 核心：`query.ts`

### 2.1 循环结构

```typescript
// query.ts - queryLoop()
while (true) {
  // 1. 可能触发 auto-compact（context 超限）
  // 2. 调用 claude.ts 发 API 请求（streaming）
  // 3. 遍历 streaming 事件：
  //    - message_start / message_delta / message_stop → 更新 token 计数
  //    - content_block_delta (text) → yield 给上层
  //    - content_block_delta (tool_use) → 积累参数
  //    - message_stop → 执行工具，写 tool_result 回 messages
  // 4. 若无工具调用 → Terminal（结束）
  // 5. 若有工具调用 → 继续循环
}
```

### 2.2 三类终止信号（Terminal）

| 原因 | 触发条件 |
|---|---|
| `end_turn` | 模型主动结束，无工具调用 |
| `max_turns` | 轮次超限，yield `error_max_turns` 结果 |
| `max_budget_usd` | 累计费用超过 `maxBudgetUsd` |
| `max_structured_output_retries` | 结构化输出重试超限 |

### 2.3 Context 压缩机制（三层）

```
micro_compact    → 把老 tool_result 替换为占位符（保留最近 N 条）
auto_compact     → 整个对话 LLM 总结成一条 summary，释放大段 context
reactive_compact → 触发条件：当前轮 assistant 消息 > 200k tokens（feature gate）
snip_compact     → 基于 snip boundary 截断历史（feature gate，SDK 模式）
```

**重要细节**：auto_compact 后会插入 `compact_boundary` system message，后续所有逻辑（session 恢复、tool result budget 等）通过 `getMessagesAfterCompactBoundary()` 只看 boundary 之后的消息。

### 2.4 Token Budget 自动续跑

```typescript
// query/tokenBudget.ts
// 用户写 "+500k" 或 "use 2M tokens" → parseTokenBudget() 解析
// 当模型消耗到 80% 预算时，注入：
"Stopped at 80% of token target (400,000 / 500,000). Keep working — do not summarize."
```

这是 Claude Code 让 agent 在 long-running 任务中不自作主张"总结并结束"的关键机制。

### 2.5 Stop Hooks（post-sampling）

每次 assistant 消息生成后，`handleStopHooks()` 可以注入额外的 user 消息，让 agent 在"即将结束"时执行额外检查（如 verification gate）。

---

## 三、工具系统设计

### 3.1 Tool 接口定义（`Tool.ts`）

每个工具是一个满足以下接口的对象：

```typescript
interface ToolDef<TInput, TOutput> {
  name: string
  description(input, context): Promise<string>   // 动态描述（用于权限提示）
  inputSchema: ZodSchema                          // 参数 schema（Zod v4）
  outputSchema?: ZodSchema                        // 可选输出 schema
  maxResultSizeChars: number                      // 超过此大小持久化到磁盘
  isEnabled(): boolean                            // 运行时开关
  prompt(context): Promise<string>                // 注入 system prompt 的工具描述
  call(input, context): AsyncGenerator<Progress | Output>  // 实现（async generator）
}
```

**核心设计**：`call()` 是 AsyncGenerator，工具可以在执行过程中 `yield` 进度事件（`Progress`），完成后 `return` 结果（`Output`）。这使得长时间运行的工具（如 bash 命令）可以流式更新 UI。

### 3.2 工具注册

```typescript
// tools.ts - getAllBaseTools()
// 工具列表是一个静态数组，通过 feature() 编译时 DCE 和 isEnabled() 运行时开关双重门控
export function getAllBaseTools(): Tools {
  return [
    AgentTool,
    BashTool,
    FileReadTool, FileEditTool, FileWriteTool,
    GlobTool, GrepTool,           // 有嵌入式 bfs/ugrep 时 DCE 掉这两个
    WebFetchTool, WebSearchTool,
    TodoWriteTool,
    ExitPlanModeV2Tool,
    ...(isTodoV2Enabled() ? [TaskCreateTool, TaskGetTool, ...] : []),
    ...cronTools,                  // AGENT_TRIGGERS feature gate
    ...(SleepTool ? [SleepTool] : []),  // KAIROS feature gate
    // ...
  ]
}
```

**Prompt Cache 稳定性**：工具列表排序是稳定的（built-in 在前，MCP 工具按名字排序追加），以保证 system prompt 的 cache prefix 不变，维持跨请求的 prompt cache 命中率。

### 3.3 BashTool 关键设计

```typescript
// BashTool.tsx 关键能力：
// 1. AST-level 命令安全检查（parseForSecurity）
// 2. 语义解析：区分 read/search/list/write 命令
// 3. 长时命令自动后台化：
//    - ASSISTANT_BLOCKING_BUDGET_MS = 15s（助手模式）
//    - PROGRESS_THRESHOLD_MS = 2s 后显示 BackgroundHint
// 4. 输出：EndTruncatingAccumulator（尾部截断，不是中间截断！）
// 5. sed 命令拦截：parseSedEditCommand() → 转为 FileEditTool 调用
// 6. 输出持久化：超过 maxResultSizeChars 写磁盘，返回预览+路径
// 7. 图片输出检测：isImageOutput() → buildImageToolResult()
```

**`sed` 拦截**是一个有趣的设计：`sed -i` 本质是文件编辑，BashTool 检测到 sed edit 命令后会将其转发给 FileEditTool，这样修改就能被 diff 追踪、权限管理、回滚保护。

### 3.4 工具执行并发（`toolOrchestration.ts`）

```typescript
// 工具执行是并发的，但有关键约束：
runTools(toolUseBlocks, canUseTool, toolUseContext)
// → StreamingToolExecutor 管理并发
// → 每个 tool_use block 独立执行
// → 所有结果收集后才进入下一轮 API 调用
```

**权限决策**（`canUseTool`）是 `async` 的——需要等用户确认时会 block，并发工具中有一个需要确认时，其他工具可以继续执行。

### 3.5 大输出持久化（`toolResultStorage.ts`）

这是 Claude Code 最精细的工程之一：

```typescript
// 双层保护：
// 1. 单工具结果超 maxResultSizeChars → 写磁盘，返回 <persisted-output> 占位
// 2. 单 API turn 中所有 tool_result 合计超 perMessageBudget → 选最大的持久化

// 关键：ContentReplacementState（seenIds + replacements Map）
// 每个 tool_use_id 的处理决策一旦做出就冻结（seenIds），
// 已替换的结果每次都用完全相同的字节重新替换（replacements Map）
// → 保证 prompt cache 命中（wire 字节完全相同）
```

---

## 四、多 Agent 协同

### 4.1 AgentTool：子 Agent 的入口

```typescript
// AgentTool.tsx - call() 内的路由逻辑：
if (isolation === 'worktree') {
  // 创建临时 git worktree，agent 在隔离副本上工作
  createAgentWorktree() → runAgent() → removeAgentWorktree()
}
if (isolation === 'remote') {
  // Ultraplan：发送到云端 CCR，Opus 远程执行
  teleportToRemote()
}
if (run_in_background) {
  // 注册为 LocalAgentTask，fire-and-forget
  // 父 agent 收到 {status: "async_launched", outputFile: "..."}
  // 通过 TaskOutputTool 轮询进度
}
if (isAgentSwarmsEnabled() && name) {
  // 多 Agent swarm：生成 teammate（tmux 或 in-process）
  spawnTeammate()
}
// 默认：同步子 agent
runAgent() → 等待完成 → 返回结果
```

### 4.2 四种 Agent 执行模式

| 模式 | 触发条件 | 共享状态 | 可见性 |
|---|---|---|---|
| **同步子 Agent** | 默认 | 父 context 副本 | 父 agent 等待，结果直接返回 |
| **Background Agent** | `run_in_background: true` | 独立 | 父 agent 继续，轮询 outputFile |
| **Worktree Agent** | `isolation: "worktree"` | 独立 git 工作树 | 同步，完成后 merge 可选 |
| **Teammate (Swarm)** | `name` 参数 + swarm 开启 | 共享邮箱文件 | 持久存活，等待下一条消息 |

### 4.3 Coordinator 模式（`coordinatorMode.ts`）

```
Coordinator（指挥官）                 Worker（执行者）
────────────────────                  ──────────────────
仅可用工具：                          完整工具集
  AgentTool                          （过滤掉内部工具）
  SendMessage
  TaskStop

标准四阶段流程：
  Research   → Worker 并行调查代码库
  Synthesis  → Coordinator 自己综合，写精确规格（含文件路径、行号）
  Implement  → Worker 按规格修改
  Verify     → Worker 验证测试
```

**铁律**：Coordinator 的 prompt 必须完全自包含——不能写 "based on your findings"，必须写明具体文件路径、行号、期望结果。Worker 看不到 Coordinator 的对话历史。

### 4.4 Teammate 通信机制（In-Process Swarm）

```typescript
// 文件邮箱系统：.claude/teams/<team-name>/mailbox/<agent-name>.json
// 消息格式：{ from, text, timestamp, color }

// 消息优先级：
// 1. Shutdown request（最高，防止被普通消息饿死）
// 2. 来自 Team Lead 的消息
// 3. 其他 peer 消息（FIFO）
// 4. 任务列表中未认领的任务
```

每个 Teammate 有两个 abort controller：
- **lifecycle abort**：kill 整个 teammate
- **per-turn abort**（Esc 键）：只停当前工作轮，teammate 进入 idle 状态等待下一条消息

### 4.5 Permission 跨 Agent 同步

```typescript
// Worker 需要权限时：
// 1. 先尝试 classifier auto-approval（bash 命令分类器）
// 2. 若需人工确认：
//    - 优先路径：直接弹出 Coordinator 的 ToolUseConfirm UI（带 worker badge）
//    - 降级路径：通过邮箱系统异步请求，Coordinator poll 响应
// 3. 权限更新同步回 Coordinator，但保留 Coordinator 自己的 mode
//    （preserveMode: true 防止 worker 的 acceptEdits mode 污染 coordinator）
```

---

## 五、System Prompt 分层架构

```typescript
// buildEffectiveSystemPrompt() — 优先级从高到低：
// 0. overrideSystemPrompt（loop mode，完全替换）
// 1. Coordinator system prompt（coordinator 模式）
// 2. Agent system prompt（自定义 agent 定义）
//    - Proactive 模式下：追加到 default，不替换
//    - 普通模式：替换 default
// 3. customSystemPrompt（--system-prompt CLI 参数）
// 4. defaultSystemPrompt（标准 Claude Code prompt）
// + appendSystemPrompt 永远追加在末尾
```

**Memory 注入**：`loadMemoryPrompt()` 在 SDK 模式下且有 `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` 时注入记忆机制 prompt，告诉模型如何读写 MEMORY.md。

---

## 六、会话持久化与恢复

### 6.1 Transcript 持久化策略

```typescript
// QueryEngine.submitMessage() 中：
// 用户消息：入队前立即写磁盘（确保 kill-mid-request 后可 --resume）
// Assistant 消息：fire-and-forget（不阻塞 generator yield）
// Compact boundary：等待前一批写完再写（维护 tailUuid 完整性）
// --bare 模式：所有写都 fire-and-forget（减少 critical path 延迟）
```

### 6.2 Crash 恢复（Bridge 模式）

```
bridge-pointer.json：
  { sessionId, environmentId }
  mtime = 最后活跃时间
  TTL = 4 小时
  clean exit 时删除，崩溃后遗留 → 下次启动提示 --resume
```

---

## 七、面试高频考点总结

### Q1: Claude Code 的 agent loop 如何工作？

**答**：核心是 `queryLoop()` 中的 `while(true)` 循环，由 `AsyncGenerator` 驱动：
1. 调用 Anthropic API，streaming 消费事件流
2. 遇到 `tool_use` block → 积累参数
3. `message_stop` 时并发执行所有工具（`runTools()`）
4. 把 `tool_result` 写回 messages，进入下一轮
5. 无工具调用时返回 Terminal

关键：整个链路是 `AsyncGenerator` 组合，streaming token 实时 yield 给用户；工具执行结果也通过 yield 传递进度。

---

### Q2: 工具系统的权限控制怎么设计的？

**答**：三层流水线：
1. **Deny rules**（静态规则）：`getDenyRuleForTool()` 在工具列表组装时过滤，模型根本看不到被 deny 的工具
2. **canUseTool**（异步决策）：每次工具调用前 await，可以 allow/deny/ask。`ask` 时弹出 UI 等用户确认
3. **Bash classifier**（ML 自动审批）：识别安全的 bash 命令，`awaitClassifierAutoApproval()` 自动放行

子 Agent 的权限由 Coordinator 批准后同步回来，但有 `preserveMode: true` 防止 worker 的宽松模式污染 coordinator。

---

### Q3: 多 Agent 协同中如何防止"甩锅式委派"？

**答**：Coordinator 的 system prompt 有强制约束：
- **禁止模糊指向**：不能写 "based on your findings"、"based on the research"
- **必须完全自包含**：每个 Worker prompt 必须包含具体文件路径、行号、期望做的改动
- **必须定义完成标准**：如"提交并报告 commit hash"

Worker 看不到 Coordinator 的任何历史，所以 Coordinator 不能偷懒——必须自己综合 Research 阶段的结果，写出足够具体的 Implementation 规格。

---

### Q4: Context 超限时 Claude Code 怎么处理？

**答**：三层渐进式压缩：
1. **micro_compact**：每轮开始时把旧 tool_result 替换为 `[Old tool result content cleared]`，只保留最近 N 条。**含 traceback/FAILURES 的结果不压缩**（保留修复信息）
2. **auto_compact**：整个对话 LLM 总结成一条 summary 消息，插入 `compact_boundary`，之后只看 boundary 之后的消息
3. **reactive_compact**（feature gate）：当前轮 assistant 消息 > 200k tokens 时触发，防止单轮爆炸

持久化层：大 tool result 写磁盘，context 里只放预览+路径。`ContentReplacementState` 跟踪每个 tool_use_id 的处理决策，保证同一 ID 的内容每次 API 调用产生完全相同的字节（prompt cache 稳定性）。

---

### Q5: Worktree isolation 如何保证 Agent 工作不污染主仓库？

**答**：`isolation: "worktree"` 模式：
1. `createAgentWorktree()` 创建临时 git worktree（`git worktree add`），Agent 在这个目录里工作
2. Agent 的所有文件操作都被 cwd override 到这个目录
3. 完成后，Coordinator 可以 review diff，选择是否合并
4. `removeAgentWorktree()` 清理

常用于：并行多个 Agent 独立修改不同功能，避免互相干扰；或者需要"试验性"修改时不污染主工作目录。

---

### Q6: 为什么工具列表的排序如此重要？

**答**：Prompt Cache 稳定性。Claude API 的 prompt cache 基于请求的精确字节前缀匹配，如果工具列表顺序每次不同，`cache_control: "ephemeral"` breakpoint 之前的部分就会 miss。

Claude Code 的策略：
- Built-in 工具按固定顺序（见 `getAllBaseTools()`），与 Statsig dynamic config 保持同步
- MCP 工具按名字字母序追加，不插入 built-in 之间
- `assembleToolPool()` 用 `uniqBy` 去重，built-in 优先

这样，无论 MCP 工具如何变化，built-in 工具的 cache prefix 始终稳定。

---

### Q7: AgentTool 的 Background 模式如何实现异步通知？

**答**：
1. AgentTool.call() 返回 `{ status: "async_launched", outputFile: "/path/to/output" }`
2. Agent 在 `LocalAgentTask` 里 fire-and-forget 执行（`startInProcessTeammate()`）
3. 完成时通过 `enqueuePendingNotification()` 注入一条 `<task-notification>` XML 消息
4. 父 agent 收到通知后可以用 `TaskOutputTool` 读取 outputFile 获取结果

关键设计：通知通过"消息队列"注入，不是直接回调。父 agent 在自己的下一轮 API 调用前才看到通知，保证消息顺序和 context 完整性。

---

## 八、与 NZ-Coder 的架构对比

| 维度 | Claude Code | NZ-Coder（你的代码） |
|---|---|---|
| Agent Loop | `while(true)` + AsyncGenerator | `for _ in range(max_turns)` |
| Tool 执行 | 并发（StreamingToolExecutor） | 顺序（for tc in tool_calls_raw） |
| Context 压缩 | 三层（micro/auto/reactive） | 两层（micro/auto） |
| 子 Agent 隔离 | Worktree / Remote / In-Process | 简单进程内隔离 |
| Tool Result 预算 | ContentReplacementState（cache 稳定） | persist_large_output（无状态跟踪） |
| 权限系统 | ML Classifier + 动态 UI | 规则匹配 + 终端交互 |
| 多 Agent | Coordinator/Teammate/Swarm | 单层 subagent |
| Token Budget | 用户可写 "+500k" 触发自动续跑 | 同样实现（本次已加） |

---

## 九、设计哲学提炼

1. **AsyncGenerator 贯穿始终**：流式响应不是"可选项"，是架构基础。工具执行结果、进度、错误都通过 yield 传递，不依赖回调地狱。

2. **Prompt Cache 是一等公民**：工具列表排序、ContentReplacementState、compact_boundary 的处理，每个地方都在为"相同请求产生相同字节前缀"服务。Cache miss 意味着延迟和成本。

3. **Feature Flag 作为编译时 DCE**：`feature('KAIROS')` 不是运行时 if，是编译时 dead code elimination。外部版本的 binary 里根本不存在 KAIROS 相关代码。

4. **双层 Abort 控制**：lifecycle abort（kill 整个 agent）和 per-turn abort（Esc 停当前工作），让用户有"软停"选项，不必每次都强行终止整个会话。

5. **工具是"观察者"**：每个 Tool 不直接写消息历史，而是 yield Progress/Output，由 toolOrchestration 统一管理。Tool 自身无状态，所有状态在 ToolUseContext 里。

---

## 十、记忆系统（Memory System）

### 10.1 记忆层次架构

Claude Code 有**五层记忆**，按生命周期从短到长：

| 层 | 位置 | 生命周期 | 用途 |
|---|---|---|---|
| Working Notes（Scratchpad） | 内存 | 单次 run() | 当前任务的推理状态、失败记录 |
| Session Context | messages 列表 | 单次会话 | 完整对话历史 |
| Auto Memory | `~/.claude/projects/<slug>/memory/` | 跨会话 | 用户偏好、项目事实、反馈 |
| Team Memory | `auto_mem_path/team/` | 团队共享 | 跨仓库的组织知识 |
| CLAUDE.md / CLAUDE.local.md | 项目根目录 | 项目级 | 项目约定、个人偏好 |

### 10.2 Auto Memory 的文件组织

```
~/.claude/projects/<project-slug>/memory/
├── MEMORY.md            ← 索引文件（≤200行，≤25KB）
├── user_role.md         ← 用户信息类 memory
├── feedback_testing.md  ← 反馈类 memory
├── project_auth.md      ← 项目类 memory
└── logs/
    └── 2025/05/
        └── 2025-05-13.md  ← KAIROS 模式的每日日志
```

**MEMORY.md 的两个关键约束**：
- 行数上限 200 行（超过截断并警告）
- 字节上限 25KB（防止长行绕过行数限制）

每条 memory 文件有 frontmatter：
```markdown
---
name: user_role
description: User is a senior backend engineer
type: user  # user | feedback | project | reference
---
Content here...
```

### 10.3 相关记忆召回（findRelevantMemories）

这是 Claude Code 最精妙的记忆设计：**不是直接把所有 memory 塞进 context，而是用 LLM 做相关性筛选**。

```typescript
// findRelevantMemories() 流程：
// 1. scanMemoryFiles() 读取所有 memory 文件的 header（frontmatter only，不读正文）
// 2. 构造 manifest：filename + description 列表
// 3. 用 Sonnet 做 sideQuery（独立 API 调用，不污染主 context）：
//    "Which of these memory files are relevant to: <当前用户查询>?"
//    → 返回最多 5 个文件名
// 4. 读取这 5 个文件的完整内容，作为 attachment 注入对话

// 关键规则：如果模型最近刚用过某工具，不召回该工具的参考文档
// 理由：模型已经在"用"这个工具，reference 文档是噪音；但 warnings/gotchas 仍然召回
```

**为什么不用关键词搜索？**
相关性判断需要理解语义，比如用户问"如何部署"，应该召回 `project_deploy_pipeline.md` 而不是只看关键词是否命中。

### 10.4 KAIROS 模式：append-only 日志 + 夜间 Dream 整合

普通模式：agent 主动用 Write 工具写入 memory 文件，同时更新 MEMORY.md 索引。

KAIROS 持久模式：
- **白天**：agent 把新信息 append 到当天的日志文件（`logs/YYYY/MM/YYYY-MM-DD.md`），不修改 MEMORY.md
- **夜间**：Dream 子系统（独立 agent）四阶段整合：
  1. **Orient**：读 MEMORY.md，了解已有记忆全貌
  2. **Gather**：从日志、已有 memory、transcript 中收集新信号
  3. **Consolidate**：合并到 topic 文件，转换相对日期为绝对日期，删除过时事实
  4. **Prune**：更新 MEMORY.md 索引，保持在行数和大小限制内

这个设计的优点：白天写入是 O(1) append，不需要读写 MEMORY.md 索引（避免索引膨胀），夜间 batch 整合成本摊薄。

### 10.5 memory 类型分类

Claude Code 强制 memory 使用四种类型，并明确规定什么**不应该**存：

| 类型 | 存什么 |
|---|---|
| `user` | 用户的角色、目标、知识背景 |
| `feedback` | 用户的偏好、纠正、工作方式 |
| `project` | 项目事实、决策、约束、截止日期 |
| `reference` | 外部系统指针（Linear 项目名、Grafana URL 等）|

**明确不存**：代码模式、架构、文件路径、git 历史（可从代码推导）；临时状态、当前进行中的工作（用 tasks 代替）。

---

## 十一、权限系统（Permission System）

### 11.1 五层权限决策流水线

```
工具调用请求
    ↓
Step 1: Deny rules（静态规则）
  → "Bash(rm -rf)" 直接 deny，模型根本看不到这个工具
    ↓
Step 2: Allow rules（静态规则）
  → 用户之前批准过 "Bash(prefix:git)" → allow
    ↓
Step 3: Tool 自身的 checkPermissions()
  → 每个工具可以实现自定义逻辑
  → BashTool: 分析命令语义（是否 mutating）
  → FileEditTool: 检查路径是否在工作目录内
    ↓
Step 4: 模式决策
  → default: ask
  → auto: 走 Classifier
  → plan: deny 所有写操作
  → acceptEdits: file edits allowed without asking
    ↓
Step 5: ML Classifier（auto 模式专用）
  → 快速路径1: acceptEdits 模式下会 allow 的 → 直接 allow（跳过 classifier）
  → 快速路径2: 安全工具白名单 → 直接 allow
  → 完整 classifier: LLM 判断当前动作是否安全（YOLO classifier）
  → Denial Tracking: 连续 deny 过多 → fallback 到交互式询问
```

### 11.2 规则语法（PermissionRule）

```
工具级别规则：
  "Bash"              → 匹配整个 Bash 工具
  "mcp__server1"      → 匹配某 MCP server 的所有工具
  "mcp__server1__*"   → 同上

工具内容级别规则（content-based）：
  "Bash(prefix:git)"  → 只允许 git 开头的 bash 命令
  "Bash(prefix:npm)"  → 只允许 npm 开头的命令
  "Agent(Explore)"    → deny 特定类型的 sub-agent

规则来源优先级（source）：
  policySettings > userSettings > projectSettings > cliArg > command > session
```

规则来源对应文件：
- `policySettings`：管理员下发的 managed settings（企业级）
- `userSettings`：`~/.claude/settings.json`
- `projectSettings`：`.claude/settings.json`（项目级）

### 11.3 YOLO Classifier（auto 模式核心）

auto 模式不是"允许一切"，而是用 LLM 判断：

```typescript
// classifyYoloAction() 的判断逻辑：
// 输入：工具名 + 当前对话历史（最近 N 条）
// 输出：allow / deny + 置信度 + 原因

// 特殊保护：
// 1. safetyCheck 类型的 deny 不可被 classifier override（绝对安全边界）
// 2. requiresUserInteraction() 的工具不走 classifier（如 AskUserQuestionTool）
// 3. REPL 工具不走 acceptEdits 快速路径（VM escape 风险）
// 4. PowerShell 默认不走 classifier（除非 ant 内部编译）
```

**Denial Tracking**：连续 deny 超过阈值时，即使在 auto 模式也 fallback 到交互式询问，防止 agent 在错误路径上反复尝试危险操作。

### 11.4 Hook 扩展点

```typescript
// 用户可以在 settings.json 中配置 hooks：
// {
//   "hooks": {
//     "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo $TOOL_INPUT"}]}],
//     "PostToolUse": [...],
//     "PermissionRequest": [{"matcher": "*", "hooks": [...]}]
//   }
// }

// PermissionRequest hook 在 hasPermissionsToUseTool() 中被调用：
// - 可以 allow（带 updatedInput + permissionUpdates）
// - 可以 deny（带 interrupt: true 终止整个 agent）
// - 可以 no-op（继续走默认流程）
```

---

## 十二、Skills 系统

### 12.1 Skill 是什么

Skill 是 Markdown 文件，通过 frontmatter 声明元数据，正文是注入 agent 的 prompt 内容。用户用 `/skill-name` 触发。

```markdown
---
description: Run the full test suite and summarize failures
when_to_use: Use when user wants to run tests
allowed-tools: Bash, FileReadTool  # 限制该 skill 可用的工具
model: sonnet                        # 可指定模型
effort: high                         # 可指定 effort 级别
paths: src/**                        # 条件激活：只在 src/ 下的文件被触发时生效
---

# Test Runner Skill

Run: `python -m pytest -v`
Summarize: failing tests and tracebacks.
```

### 12.2 Skill 加载路径（四级优先级）

```
policySettings (managed):  /path/to/managed/.claude/skills/
userSettings:               ~/.claude/skills/
projectSettings:            ./.claude/skills/       ← 最常用
additionalDirs (--add-dir): <dir>/.claude/skills/

加载顺序：managed > user > project > additional
同名 skill：先加载的优先（managed 最高）
```

每个 skill 必须是目录格式：`skill-name/SKILL.md`（单文件 `.md` 不被 `/skills/` 目录支持，但 legacy `/commands/` 目录支持）。

### 12.3 动态 Skill 发现（条件激活）

```typescript
// 两种动态机制：

// 1. 运行时目录发现：
// 当 agent 读写文件 src/components/Button.tsx 时，
// 自动检查 src/components/.claude/skills/ 是否存在，
// 存在则加载（比 cwd 级别更深的 skills）
// → 允许项目子目录有自己的专用 skills

// 2. 条件激活（paths frontmatter）：
// skill 在 paths 中声明 "src/**"
// 当 agent 首次操作 src/ 下的文件时，该 skill 被激活加入可用列表
// → "按需"加载，避免不相关 skill 占用 context
```

**gitignore 保护**：`node_modules/pkg/.claude/skills/` 不会被加载（通过 `git check-ignore` 检测）。

### 12.4 Skill 内置变量和 Shell 执行

```markdown
---
allowed-tools: Bash
---

Base dir: ${CLAUDE_SKILL_DIR}  ← 替换为 skill 文件所在目录
Session: ${CLAUDE_SESSION_ID}  ← 替换为当前 session ID

# 动态内容注入（!`...` 语法）
Current branch: !`git rev-parse --abbrev-ref HEAD`  ← 加载时执行并替换
```

**安全规则**：MCP skills（远程、不可信）不执行 `!`\`...\`` shell 命令，本地 skills 可以。

### 12.5 面试常问：Skill vs Memory vs CLAUDE.md

| 维度 | Skill | Memory | CLAUDE.md |
|---|---|---|---|
| 触发方式 | 用户 `/skill-name` 或 agent 主动调用 | 自动召回 | 每次会话自动加载 |
| 内容类型 | 任务指令、工作流程 | 用户偏好、项目事实 | 项目规范、编码约定 |
| 生命周期 | 永久（文件） | 永久（文件），可 cleanup | 永久（文件） |
| 谁写 | 用户手动编写 | agent 自动写入 | 用户手动编写 |
| 占用 context | 只在调用时注入 | 按需召回（≤5条） | 每次会话都加载 |

---

## 十三、面试追加考点

### Q8: Auto Memory 的相关性召回为什么用 LLM 而不用向量检索？

**答**：
1. **语义理解**：用户问"deploy the service"应该召回 `project_deploy_pipeline.md`，关键词匹配可能失败（deploy 可能不在文件名里）
2. **避免噪音**：召回时排除"模型已在使用的工具的参考文档"——这需要理解工具用途和文档内容的关系，向量相似度做不到这个语义判断
3. **规模适中**：memory 文件通常只有几十个，LLM 召回一次 sideQuery 成本远低于维护向量数据库
4. **Zero-shot 可用**：不需要任何预训练或 fine-tuning，新用户开箱即用

### Q9: 权限系统中 `acceptEdits` 模式是什么？

**答**：`acceptEdits` 是一个虚拟权限模式，用于 auto 模式的"快速路径"判断。在真正调用 YOLO classifier 之前，把 permission mode 改为 `acceptEdits` 重新运行工具的 `checkPermissions()`。如果在 acceptEdits 下工具会 allow，说明这是一个普通的文件编辑操作，不需要走 classifier（节省 API 调用延迟和成本）。

### Q10: Conditional Skill 的 paths frontmatter 和 CLAUDE.md 的 conditional rules 有什么关系？

**答**：都使用 gitignore-style 的 `ignore` 库做路径匹配，行为完全一致。CLAUDE.md 的 conditional rules 是"针对某些文件的特殊规则"，Conditional Skill 是"针对某些文件的额外指令集"。两者是同一匹配机制的不同应用场景。

---

## 十四、更新后的 NZ-Coder 对比表

| 维度 | Claude Code | NZ-Coder（已改进） |
|---|---|---|
| Agent Loop | `while(true)` + AsyncGenerator | `for _ in range(max_turns)`，token budget 已加 |
| Tool 执行 | 并发（StreamingToolExecutor） | **只读工具并发**（已加 ThreadPoolExecutor） |
| sed 拦截 | `parseSedEditCommand()` → FileEditTool | **已加**：`_apply_sed_via_edit()` |
| 中断恢复 | `yieldMissingToolResultBlocks()` | **已加**：`_inject_missing_tool_results()` |
| Memory 召回 | LLM sideQuery 相关性筛选 | Jaccard 关键词 + freshness 评分 |
| Memory 整合 | Dream 四阶段夜间整合 | 时间老化 cleanup，无语义合并 |
| 条件 Skill | `paths` frontmatter + gitignore 匹配 | 无 |
| 权限系统 | 5层流水线 + ML Classifier | 3层规则匹配 |
| Tool 空结果 | `(toolName completed with no output)` | **已加**：同样格式 |
