# NZ-Coder 核心系统架构

> 范围说明：这份文档讲 NZ-Coder 主干 Agent；历史 Dodo/PySide 平行产品已在 A034 收敛删除，不再构成第二套架构。
> 目标是帮助你从“代码能跑”提升到“我知道它为什么这么设计、每层怎么协作、以后该怎么改”。

---

## 1. 先给结论：NZ-Coder 本质上是什么

NZ-Coder 不是“套了一个 Agent 框架的工作流配置”。

它的本质是一个**手写的、状态明确的 coding-agent runtime**：

1. 用一个明确的 `AgentRunner` 驱动所有 Main/child/background/workflow 循环。
2. 用 OpenAI function calling 风格把模型和工具连接起来。
3. 用事务、权限、trace、verification gate 把“会改代码的 LLM”约束成一个可控系统。
4. 用多层上下文和多层记忆，把 prompt 成本压住，同时尽量保留连续性。

换句话说，NZ-Coder 的核心不是“提示词”，而是：

- **显式状态机**
- **显式工具平台**
- **显式安全边界**
- **显式可观测性**

这也是它和很多“能跑 demo 但不可维护”的 Agent 项目最大的区别。

---

## 2. 设计原则

从代码上看，这个系统一直在坚持几条原则。

### 2.1 不依赖 Agent 框架

核心控制流、记忆、事务、工具注册、权限判定、恢复逻辑，全都在本仓库里。

这样做的收益：

- 运行机制透明
- 出 bug 时能直接定位
- 评测时更容易做针对性优化
- 不会被 LangChain / CrewAI 一类框架的抽象反噬

代价也很直接：

- 你要自己维护更多基础设施
- 你必须真的理解 LLM + tool calling + state management 的组合方式

NZ-Coder 选择了前者。

### 2.2 把“模型推理”与“系统事实”分开

这是整个项目最重要的思想之一。

模型会产生：

- 计划
- 假设
- 工具调用
- 最终回答

但系统自己也维护一套**客观事实状态**，比如：

- 当前 turn 数
- 是否已经有 diff
- 是否已经验证过
- 最近是否一直在空转搜索
- 当前 todo 是否还没更新

这些不交给模型“自己记住”，而是通过 `RuntimeState`、`VerificationManager`、`TransactionManager`、`TraceRecorder` 这类模块**系统化维护**。

这就是“state-as-message”而不是“全靠 prompt discipline”。

### 2.3 工具调用必须是平台，不是散函数

NZ-Coder 不是让模型随便拼 shell。

它有明确的：

- 工具注册表
- 工具 schema
- 工具参数校验
- 工具权限检查
- 工具结果分类
- 写工具事务回滚

也就是说，“工具”在这里不是辅助函数，而是系统边界。

### 2.4 安全是分层做的，不是单点做的

系统不是只靠一句“请小心修改文件”。

它有至少四层安全：

1. 文件路径安全 `_safe_path()`
2. shell 命令分类 `classify_bash()`
3. 权限模式和规则 `PermissionManager`
4. 多文件写事务 `TransactionManager`

再往上还有：

- `ChangeTracker` 记录 agent 改了什么
- `VerificationManager` 防止“没验证就宣布完成”

### 2.5 尽量让每个模块只做一件事

比如：

- `ToolExecutor` 负责执行单个工具，但不负责消息拼接
- `VerificationManager` 负责 gate 状态，但不直接调模型
- `TraceRecorder` 只记日志，不做控制流
- `MemoryManager` 管持久记忆，不替代 scratchpad

这种拆分让系统在扩展时比较稳。

---

## 3. 总体分层

如果只看主干，NZ-Coder 可以分成 8 层。

```text
CLI / Entry
  ->
Prompt assembly + Context layering
  ->
AgentLoop (compatibility/coding adapter)
  -> AgentRunner + ProductionRuntimeHost
  ->
ToolExecutor + Tool Registry
  ->
Permissions / Command policy
  ->
File / Shell / Search / Repo-intel tools
  ->
Transaction / Change tracking / Verification / Trace
  ->
Memory / Scratchpad / Skills / Sessions
```

对应目录大致是：

- `interface/`：CLI 入口
- `runtime/`：主循环与调度
- `tools/`：工具实现
- `tool_platform/`：权限和命令分类
- `state/`：记忆、会话、trace、事务、变更跟踪
- `intelligence/`：项目画像、验证规划、impact 分析
- `project_creation/`：Greenfield 模式
- `evaluation/`、`swebench/`：评测与基准

包根目录现在只保留正式公共入口；它们不是第二套实现，而是很薄的
façade。内部模块已经迁到 canonical 子包，不再保留通用兼容 wrapper。
典型对应关系是：

- `nz_coder.loop` -> `nz_coder.runtime.execution.loop`
- `nz_coder.permissions` -> `nz_coder.tool_platform.permissions`

所以读代码时应优先进入子目录里的真实实现；记忆等内部能力直接使用
`nz_coder.state.memory`，不存在 `nz_coder.memory` 兼容入口。

---

## 4. 一次任务到底是怎么跑完的

这部分最值得理解，因为它把所有模块串起来了。

### 4.1 CLI 启动

入口是：

- [cli.py](/home/pyh/nzcoder/nz_coder/interface/cli.py)
- [__main__.py](/home/pyh/nzcoder/nz_coder/__main__.py)

CLI 启动后会做几件事：

1. 检查 `API_KEY`
2. 加载持久 memory：`memory_mgr.load_all()`
3. 加载 skills：`skill_loader.descriptions()`
4. 构建 system prompt：`prompt.build(...)`
5. 通过 composition 初始化 `AgentLoop` capability host 与共享 `AgentRunner`
6. 进入 REPL，接收用户输入或 slash command

这一步的思想是：

- CLI 只负责“人机交互”
- 真正的 turn orchestration 在 `AgentRunner`；`AgentLoop` 提供成熟 coding policy adapter

所以 CLI 是薄的，runtime 是厚的。

### 4.2 AgentLoop 初始化

核心文件：

- [loop.py](/home/pyh/nzcoder/nz_coder/runtime/execution/loop.py)

`AgentLoop.__init__()` 会把整个系统的核心组件组起来：

- `PermissionManager`
- `RecoveryState`
- `TransactionManager`
- `TraceRecorder`
- `ChangeTracker`
- `VerificationManager`
- `ToolExecutor`
- `RuntimeState`
- `RunEvidence`
- `Scratchpad`
- `MemoryManager`

此外还会做一个很关键的动作：

- 把 `TransactionManager` 和 `ChangeTracker` 注入到 `tools/files.py`

这是依赖注入，不是全局单例硬编码。

也就是说，文件工具本身不知道“整个 agent 怎么运行”，但它能拿到当前 run 对应的事务和变更跟踪器。

### 4.3 run() 主循环

`AgentLoop.run()` 是兼容入口；`AgentRunner.run()` 与唯一的
`AgentRunner._run_turns()` 才是整个系统的执行心脏。`ProductionRuntimeHost`
负责在进入状态机前绑定 workspace、Session、MCP、Memory、Skill、工具状态和交互回调。

主流程可以简化为：

1. `_init_run()`
   重置 verification / scratchpad / runtime_state / evidence，记录 `run_start`

2. `_maybe_generate_plan()`
   如果任务复杂，先让模型生成 plan，并写进 scratchpad

3. 每轮循环：
   - `_compact_if_needed()`：必要时做 micro compact 或 auto compact
   - `_build_api_messages()`：构建这一轮发给模型的消息
   - `_call_llm()`：streaming 或 non-streaming 调模型
   - 如果模型没调工具：
     - `_check_verification_gate()` 看能不能结束
   - 如果模型调了工具：
     - `_execute_tools()` 执行工具
     - `_maybe_replan()` 必要时重规划

4. `_finalize()`
   记录状态、写 trace、保存 learnings、返回结果

### 4.4 主循环为什么这样设计

这里有几个关键思想。

#### A. “计划”不是硬编码必选项

系统会根据任务模式和复杂度决定要不要先规划。
复杂任务先生成 `## Plan`，简单任务直接做。

这是平衡：

- 不让所有任务都被 planning 拖慢
- 也不让复杂任务完全裸奔

#### B. 上下文不是每轮全量拼接

每轮模型请求前，系统会重新组装上下文层：

- 固定层：`system_prompt`
- 半固定层：memory block
- 动态层：runtime state + scratchpad

见：

- `_build_api_messages()`
- `_build_context_layers()`（`runtime/execution/loop.py` 里的模块级 helper）
- `_inject_dynamic_context()`（`runtime/execution/loop.py` 里的模块级 helper）

核心思想是：

- **让系统 prompt 尽量稳定**
- 把高变化、高噪音内容放在动态注入层
- 超预算时先截断 memory/scratchpad/state，而不是破坏核心 system prompt

#### C. completion 不是“模型说结束就结束”

如果刚做过写操作，但还没有通过验证，`VerificationManager` 会触发 gate：

- 注入 `<verification-required>`
- 强迫模型继续跑验证或修复

这让 NZ-Coder 从“能改”变成“尽量改完再结束”。

---

## 5. Prompt 与上下文层

核心文件：

- [prompt.py](/home/pyh/nzcoder/nz_coder/runtime/conversation/prompt.py)
- [context.py](/home/pyh/nzcoder/nz_coder/state/context.py)
- [runtime_state.py](/home/pyh/nzcoder/nz_coder/runtime/execution/runtime_state.py)
- [scratchpad.py](/home/pyh/nzcoder/nz_coder/tools/scratchpad.py)

### 5.1 System prompt 的角色

`prompt.build()` 不是一个花哨的人设模板，它更像**运行规则说明书**。

里面定义了：

- 什么时候该用 `todo`
- 什么时候该用 `task` 子 agent
- 文件修改后必须验证
- project creation 的标准工具序列
- 对 memory 的优先级约束
- 各个工具的语义

这份 prompt 的价值不在“语言优美”，而在于：

- 把运行策略显式写给模型
- 让工具选择和收敛习惯保持一致

### 5.2 Context compaction 的思想

`state/context.py` 里有三件事：

1. `persist_large_output()`
   大工具输出不直接塞回上下文，而是写入 `.nz-coder/tool-results/`，上下文里只保留 preview。

2. `micro_compact()`
   针对旧的 tool result 做轻量压缩，把巨大的历史输出替换成占位符。

3. `auto_compact()`
   当上下文超预算时，让模型对整个历史做总结，转成一条“continuation summary”。

设计思想：

- 工具输出往往比思考更占 token
- 真正需要保留的是“关键信息”，不是完整 stdout
- 上下文压缩不能只靠“截断最后几条消息”，要有结构化策略

### 5.3 RuntimeState：系统客观事实

`RuntimeState` 是这个项目里很值得学习的设计。

它记录的不是模型主观想法，而是系统事实，例如：

- `turn_count`
- `edits_this_run`
- `has_diff`
- `changed_files`
- `verification_attempts`
- `broad_test_attempts`
- `env_noise_seen`
- `acceptance_criteria`
- `task_mode`

然后每轮通过 `build_prompt_block()` 把这些事实注入给模型。

这解决的是一个常见问题：

> 模型自己不擅长稳定维护长时客观状态。

所以你把状态抽出来，系统维护，模型消费。

### 5.4 Scratchpad：工作记忆

`tools/scratchpad.py` 是第一层记忆，也就是**session 内 working memory**。

特点：

- 纯内存，不持久化
- agent 主动写入
- 每条 entry 有 `category`
  - `hypothesis`
  - `attempt`
  - `failure`
  - `finding`
  - `plan`
- 自动限制条数和总长度
- 每轮自动注入 prompt

它解决的是：

- 失败尝试别反复重复
- 计划和已知事实别丢
- auto compact 后 agent 仍保有一小块“当前工作笔记”

RuntimeState 和 Scratchpad 的区别非常关键：

- `RuntimeState` = 系统维护的客观事实
- `Scratchpad` = agent 维护的主观工作笔记

---

## 6. 记忆系统：三层结构

NZ-Coder 的记忆不是单一 memory。

它至少有三层：

### 6.1 第一层：Scratchpad

见上文。
作用是当前任务中的短期工作记忆。

### 6.2 第二层：Persistent Memory

核心文件：

- [memory.py](/home/pyh/nzcoder/nz_coder/state/memory.py)

这是跨 session 的持久知识库。

默认后端是 markdown 文件：

- 每条 memory 一个 `.md`
- 一个 `MEMORY.md` 作为索引

结构化字段包括：

- `name`
- `description`
- `type`
- `content`
- `created_at`
- `last_accessed`
- `access_count`

#### 记忆的核心操作

1. `load_all()`
   启动时加载所有 memory

2. `save()`
   保存一条新记忆或 merge 到现有记忆

3. `recall(query, top_k)`
   通过相关性搜索召回记忆

4. `build_prompt_block(query=...)`
   不是把所有记忆都注入，而是只把相关记忆转成 prompt block

5. `scan_headers()` / `load_content()`
   先看索引，再按需加载正文，减少成本

#### 为什么它默认是 markdown，而不是数据库

因为这个层的目标优先级不是“高并发检索”，而是：

- 可读
- 可调试
- 可迁移
- 低依赖

你直接打开 `MEMORY.md` 和对应文件就能知道系统记住了什么。

#### 召回策略的思想

它不是只做简单关键词匹配。

从常量和方法可以看出，它用了启发式混合评分：

- coverage
- Jaccard
- exact match
- freshness

这类策略的好处是：

- 不需要额外向量依赖就能工作
- 在 coding-agent 这种“文件名、函数名、错误关键词”密集的场景里通常够用

### 6.3 第三层：Conversation Context

这就是当前对话本身，由 `messages` 组成。

它不是“记忆系统”的一部分实现，但它是实际最昂贵的上下文层。
因此 `context.py` 负责压缩它，`sessions.py` 负责持久化它。

### 6.4 三层记忆为什么合理

这是一个很好的工程拆分：

- **Scratchpad**：解决当前任务的工作连续性
- **Persistent Memory**：解决跨 session 的长期知识
- **Conversation Context**：解决当前轮推理的完整上下文

如果把这三种东西混在一起，系统会非常难控：

- 该压缩的压不掉
- 该保留的保不住
- 该持久化的会丢

---

## 7. 工具系统：NZ-Coder 的“执行面”

核心文件：

- [tools/__init__.py](/home/pyh/nzcoder/nz_coder/tools/__init__.py)
- [tool_executor.py](/home/pyh/nzcoder/nz_coder/runtime/execution/tool_executor.py)
- `tools/` 目录下各具体工具模块

### 7.1 工具注册机制

所有工具通过下面这个真实签名注册：

```python
register(name, description, parameters, handler)
```

注册到全局注册表里：

- `TOOL_SPECS`
- `TOOL_HANDLERS`

然后由：

```python
dispatch(name, arguments)
```

统一分发。

这个设计非常重要，因为它把工具平台标准化了：

- 模型只看 schema
- runtime 只看名字和参数
- handler 只关心业务逻辑

### 7.2 为什么是副作用 import

`runtime/execution/loop.py` 顶部会 import 一堆工具模块：

- `nz_coder.tools.bash`
- `nz_coder.tools.files`
- `nz_coder.tools.search`
- `nz_coder.tools.repo_intel`
- ...

这些 import 的目的不是直接调用，而是**触发各模块里的 `register(...)`**

优点：

- 工具定义和工具装载分离
- 工具模块自己负责注册自己
- loop 不需要手写一大串 `register(...)`

### 7.3 ToolExecutor 的角色

`ToolExecutor` 是工具平台和主循环之间的适配层。

它负责：

1. 限制单轮工具调用数
2. 解析 JSON 参数
3. 做权限检查
4. 调用 `dispatch()`
5. 分类结果：
   - `dispatch_failed`
   - `command_failed`
   - `is_write`

这里的一个细节很关键：

- bash 非零退出码不一定是系统错误
- 它可能只是“测试失败”，这对 agent 是有价值的反馈

所以系统区分：

- `dispatch_failed`：工具层失败，应影响事务
- `command_failed`：命令执行了，但测试没过，不一定要回滚

这是很成熟的设计。

### 7.4 工具分类

工具大致分成几类。

#### A. 文件工具

核心文件：

- [files.py](/home/pyh/nzcoder/nz_coder/tools/files.py)

包括：

- `read_file`
- `write_file`
- `edit_file`
- `replace_lines`
- `apply_patch`
- `write_files_batch`

特点：

- 全部走 `_safe_path()`
- 写前 track before / 写后 track after
- 支持 diff 输出
- 支持批量写原子化
- 会检查敏感文件（`.env`、SSH key 等）写入

这套设计说明它把“文件写入”当成高风险操作来对待。

#### B. Shell 工具

核心文件：

- `tools/bash.py`
- [command_policy.py](/home/pyh/nzcoder/nz_coder/tool_platform/command_policy.py)

shell 不是直接裸放开的。

命令会被分成：

- dangerous
- mutating
- known read-only
- unknown

这是 NZ-Coder 权限系统的基础输入。

#### C. 搜索与仓库理解工具

包括：

- `grep_search`
- `glob_search`
- `smart_search`
- `read_symbol`
- `find_symbol_callers`
- `diff_status`
- `verify_changed_files`

这里的思想不是“给模型一把 grep”，而是给模型一组**低噪音仓库理解原语**。

例如：

- `smart_search`：针对 issue/traceback 做 grep-first 的候选文件排序
- `read_symbol`：AST 级别读取 Python symbol
- `find_symbol_callers`：AST 级别找引用
- `verify_changed_files`：针对改动文件跑低噪音验证

这部分是 NZ-Coder 面向 SWE-bench 和真实修 bug 场景优化最多的地方之一。

#### D. 会话辅助工具

包括：

- `todo`
- `update_scratchpad`
- `read_scratchpad`
- `load_skill`

这些工具不直接改代码，但会显著影响 agent 的行为质量。

---

## 8. 权限系统：不是一个 yes/no，而是一条决策管线

核心文件：

- [permissions.py](/home/pyh/nzcoder/nz_coder/tool_platform/permissions.py)
- [command_policy.py](/home/pyh/nzcoder/nz_coder/tool_platform/command_policy.py)

### 8.1 PermissionManager 的决策顺序

它不是单一 if/else，而是一条流水线：

1. deny rules
2. bash-specific classification
3. plan mode block writes
4. safe read tools auto allow
5. session allow rules
6. ask rules
7. auto / acceptEdits 模式处理
8. default mode 的最终判断

这说明权限系统的设计目标不是“简单”，而是“可扩展且可解释”。

### 8.2 支持的模式

模式包括：

- `default`
- `auto`
- `plan`
- `acceptEdits`

它们分别服务不同场景：

- `default`：写操作和危险 bash 要问
- `auto`：尽量自动化执行
- `plan`：只允许规划和只读
- `acceptEdits`：允许文件编辑，但不自动放开任意 bash

这个 `acceptEdits` 设计很有意思，因为它把“改代码”和“跑命令”拆开了。

### 8.3 规则系统

权限规则支持：

- `allow`
- `deny`
- `ask`

而且可以带内容匹配，例如：

- `bash(prefix:git )`

也就是说，它不是只按“工具名”授权，而是可以按“工具 + 内容前缀”授权。

这种设计明显比“允许 bash / 不允许 bash”更实用。

### 8.4 ask_user 为什么做摘要

`ask_user()` 不是把整个 JSON blob 打给用户看，而是通过 `_format_tool_summary()` 做人类可读摘要：

- bash：展示命令
- 写文件：展示 path
- memory：展示 name

这让权限确认变得像真实产品，而不是调试界面。

---

## 9. 事务、变更跟踪和回滚：保证“能撤”

核心文件：

- [transaction.py](/home/pyh/nzcoder/nz_coder/state/transaction.py)
- [changes.py](/home/pyh/nzcoder/nz_coder/state/changes.py)

### 9.1 TransactionManager：防止多文件半成功

设计流程很简单：

1. `begin()`
2. 写前 `track(path)`
3. 成功则 `commit()`
4. 失败则 `rollback()`

实现上，它会：

- 把原文件备份到临时目录
- 新文件记录为“之前不存在”
- rollback 时恢复旧文件或删除新文件

这个机制解决的是：

> 一个 LLM 回合里改了多个文件，结果中途某个工具失败，工作区不能留半套脏状态。

### 9.2 ChangeTracker：面向“可审查”的变更记录

`TransactionManager` 关心“能否回滚”，
`ChangeTracker` 关心“agent 到底改了什么”。

它会记录：

- before_exists
- before
- after_exists
- after

并写成 JSON 文件，支持：

- render diff
- revert latest

而且回滚时有一个保守条件：

> 只有当前文件内容仍然和“tracked after-state”一致，才允许回滚。

这避免了用户手动改过之后，agent 的回滚把用户新改的内容覆盖掉。

这是一个非常合理的安全保护。

---

## 10. 验证系统：让 agent 不容易“假完成”

核心文件：

- [verification.py](/home/pyh/nzcoder/nz_coder/intelligence/verification.py)
- `runtime/execution/loop.py` 里的 verification gate
- `verification_planner.py`
- `repo_intel.py` 里的 `verify_changed_files`

### 10.1 VerificationManager 管什么

它主要追踪三件事：

1. 是否做过写操作
2. 是否已经有通过的验证
3. 如果没有通过验证，模型是否还能结束

写工具一成功，`mark_write()` 就会把 gate 拉起来。
后面只有验证工具成功，gate 才会放下。

### 10.2 什么叫“验证”

它不是只认 pytest。

`_is_verification_command()` 会识别：

- pytest / unittest / tox / nox
- npm test / cargo test / go test
- py_compile / compileall
- tsc --noEmit
- 某些 Python 行为检查

也就是说，验证的概念是：

> 对改动行为做最小但可信的执行确认

### 10.3 为什么区分环境噪音

`RuntimeState` 和 `VerificationManager` 都会识别环境类失败，例如：

- 缺少依赖
- DISPLAY 问题
- 某些 pytest 配置噪音

原因是 coding agent 经常会遇到：

> 测试没过，但不是 patch 的问题，而是环境坏了。

系统需要把这种情况显式识别出来，否则模型会在错误方向上反复修。

### 10.4 gate 机制为什么重要

很多 agent 最大的问题不是不会改，而是**太早结束**。

NZ-Coder 的 gate 做的就是：

- 模型没调工具且想结束时
- 如果最近做过写操作但没验证通过
- 系统会自动插入 `<verification-required>`

这是一种非常有效的“收尾约束”。

---

## 11. 项目画像、验证规划与影响分析

核心文件：

- [project_profile.py](/home/pyh/nzcoder/nz_coder/intelligence/project_profile.py)
- `verification_planner.py`
- `impact_analyzer.py`

### 11.1 Project Profile：减少盲目探索

`project_profile.py` 会扫描仓库，提取：

- 语言
- 包管理器
- source roots
- test roots
- test/typecheck/lint/build commands

目的不是做“完美构建系统检测”，而是做**足够轻量、足够有用的仓库画像**。

这类信息一旦提前知道，模型的探索效率会高很多。

### 11.2 Verification Planner：推荐低噪音验证

它解决的问题是：

> 模型很容易动不动跑整个测试套件，既慢又噪音大。

所以 planner 会根据：

- 改动文件
- 项目类型
- failing tests

推荐一个尽量小的验证集合。

### 11.3 Impact Analyzer：风险感知

它会根据 patch 特征做风险总结，例如：

- 改了哪些区域
- 影响面可能多大
- 建议补哪些验证

这让 agent 不只会“改”，也开始有一点“代码审查视角”。

---

## 12. Skills 与 Subagent：两种扩展能力

### 12.1 Skills：静态知识扩展

核心文件：

- [skills.py](/home/pyh/nzcoder/nz_coder/state/skills.py)

Skill 机制本质上是：

- `SKILL.md` 驱动的知识片段
- 三层加载优先级：
  - project
  - user
  - bundled
- 支持条件激活（按路径 pattern）
- body lazy load

这意味着它不是插件系统，而是**受控的专业知识注入系统**。

适合做：

- 某类项目的固定工作流
- 某个领域的规则提示
- 某套代码库的局部约定

### 12.2 Subagent：上下文隔离扩展

核心文件：

- [subagent.py](/home/pyh/nzcoder/nz_coder/runtime/agent/subagent.py)

Subagent 的思想不是“多智能体很酷”，而是：

> 当主 agent 的上下文已经很重，或者某段探索天然适合隔离时，开一个小 agent 去做。

特点：

- 独立上下文
- 有自己的 turn budget / timeout
- 工具集受限
  - explore/review 多为只读
  - general 才允许部分写
- 支持共享 scratch file

这让主 agent 不必把所有搜索噪音都背在自己上下文里。

---

## 13. Session、Trace、Status：系统可观测性

### 13.1 Session 持久化

核心文件：

- [sessions.py](/home/pyh/nzcoder/nz_coder/state/sessions.py)

作用：

- 保存会话历史
- `/resume` 继续之前的消息
- 记录 workspace、model、mode

这不是 memory 的替代，而是 conversation 的持久化。

### 13.2 Trace

核心文件：

- [trace.py](/home/pyh/nzcoder/nz_coder/state/trace.py)

Trace 是 append-only JSONL。

会记录：

- run_start / run_end
- llm_request / llm_response
- tool_call
- api_error
- compact
- verification_result

它的价值在于：

- 真实复盘 agent 行为
- 分析失败模式
- 做 benchmark / eval 时可追踪

### 13.3 Workspace Status

核心文件：

- [workspace.py](/home/pyh/nzcoder/nz_coder/state/workspace.py)

这层主要服务 CLI 和用户可见性：

- git status
- project facts
- latest trace / latest change set

也就是说，它是操作层视角，而不是 LLM 推理层视角。

---

## 14. Greenfield、Evidence、Reviewer：主干上的高级能力

虽然你现在让文档聚焦“agent 核心”，但这几块已经是主干能力的一部分，值得单独理解。

### 14.1 Greenfield Project Creation

核心目录：

- `project_creation/`

它不是简单模板器，而是完整链路：

1. `requirement_analyzer.py`
2. `blueprint.py`
3. `templates.py`
4. `inspector.py`
5. `completeness.py`
6. `acceptance_planner.py`
7. `verifier.py`

这说明 NZ-Coder 不只做“仓库内修 bug”，也开始做“从需求到项目雏形”的结构化生成。

### 14.2 RunEvidence

核心文件：

- [run_evidence.py](/home/pyh/nzcoder/nz_coder/runtime/observability/run_evidence.py)

RunEvidence 不是审查器，而是**运行期证据收集器**。

它在代码里真实维护的核心字段包括：

- `created_files`
- `modified_files`
- `expected_files`
- `actual_output_paths`
- `verification_results`
- `build_results`
- `impact_review`
- `completeness_review`
- `limitations`
- `tool_failures`
- `notes`

它的定位是：

- 让一次 run 的结果不只剩自然语言回答
- 为 reviewer、eval、结果分级提供结构化输入
- 但**不直接改变 AgentLoop 控制流**

这一步很重要，因为它让 agent 的结果从“自然语言自述”走向“结构化可审查结果”。

### 14.3 Reviewer

核心文件：

- [reviewer.py](/home/pyh/nzcoder/nz_coder/intelligence/reviewer.py)

Reviewer 才是那个**只读审查器**：

- 不改变控制流
- 只根据 evidence 和 runtime 评估结果质量

这说明系统已经开始把“生成”和“审查”分层了。

---

## 15. CLI 层为什么保持很薄

`interface/cli.py` 你会看到：

- `/help`
- `/compact`
- `/todo`
- `/memory`
- `/profile`
- `/status`
- `/trace`
- `/diff`
- `/revert-last`
- `/save-session`
- `/resume`

CLI 做的事情大多是：

- 调 runtime
- 调 state 层已有能力
- 负责展示

它没有大量业务逻辑。

这是对的，因为：

- CLI 是可替换的
- 未来你可以有 TUI、Web、服务端入口
- 核心行为应该留在 runtime/state/tool 层

---

## 16. 你应该怎么读这个代码库

如果你是为了学习，我建议按下面顺序读。

### 第一轮：先抓主循环

1. [runtime/execution/loop.py](/home/pyh/nzcoder/nz_coder/runtime/execution/loop.py)
2. [runtime/conversation/prompt.py](/home/pyh/nzcoder/nz_coder/runtime/conversation/prompt.py)
3. [interface/cli.py](/home/pyh/nzcoder/nz_coder/interface/cli.py)

先弄清：

- 用户输入怎么变成一轮模型调用
- 工具结果怎么回到对话
- 什么时候结束

### 第二轮：看执行面和安全面

1. [tools/__init__.py](/home/pyh/nzcoder/nz_coder/tools/__init__.py)
2. [runtime/execution/tool_executor.py](/home/pyh/nzcoder/nz_coder/runtime/execution/tool_executor.py)
3. [tool_platform/permissions.py](/home/pyh/nzcoder/nz_coder/tool_platform/permissions.py)
4. [tool_platform/command_policy.py](/home/pyh/nzcoder/nz_coder/tool_platform/command_policy.py)
5. [tools/files.py](/home/pyh/nzcoder/nz_coder/tools/files.py)

先搞懂：

- 工具是怎么注册和调度的
- 权限为什么要分层
- 文件写入为什么一定要挂事务和变更跟踪

### 第三轮：看状态和记忆

1. [state/memory.py](/home/pyh/nzcoder/nz_coder/state/memory.py)
2. [tools/scratchpad.py](/home/pyh/nzcoder/nz_coder/tools/scratchpad.py)
3. [state/context.py](/home/pyh/nzcoder/nz_coder/state/context.py)
4. [runtime/execution/runtime_state.py](/home/pyh/nzcoder/nz_coder/runtime/execution/runtime_state.py)
5. [state/sessions.py](/home/pyh/nzcoder/nz_coder/state/sessions.py)

这轮重点理解：

- 三层记忆为什么拆开
- context budget 为什么不能粗暴截断
- RuntimeState 和 scratchpad 的边界

### 第四轮：看收尾和质量控制

1. [intelligence/verification.py](/home/pyh/nzcoder/nz_coder/intelligence/verification.py)
2. [state/transaction.py](/home/pyh/nzcoder/nz_coder/state/transaction.py)
3. [state/changes.py](/home/pyh/nzcoder/nz_coder/state/changes.py)
4. [state/trace.py](/home/pyh/nzcoder/nz_coder/state/trace.py)
5. [run_evidence.py](/home/pyh/nzcoder/nz_coder/runtime/observability/run_evidence.py)
6. [reviewer.py](/home/pyh/nzcoder/nz_coder/intelligence/reviewer.py)

这轮重点看：

- 系统怎么防止假完成
- 出错后怎么回滚和复盘
- 为什么 evidence/reviewer 是自然演进，而不是额外花活

---

## 17. 这个架构最强的地方

如果从系统设计角度评价，NZ-Coder 主干现在最强的地方有四个。

### 17.1 主循环是显式的

你可以真正回答：

- 每轮发生了什么
- 为什么重试
- 为什么结束
- 为什么回滚

这是非常大的优势。

### 17.2 安全和执行耦合得刚好

很多项目会出现两种问题：

- 要么完全裸奔
- 要么安全规则太重，把 agent 卡死

NZ-Coder 现在的权限 + 命令分类 + 事务 + gate，平衡得不错。

### 17.3 上下文设计比较成熟

它不是“消息越来越长直到爆掉”。

它已经有：

- 大输出持久化
- micro compact
- auto compact
- dynamic context layering
- scratchpad
- relevant memory recall

这一套组合起来，才像一个长期运行的 coding agent。

### 17.4 评测友好

这个项目从设计上就考虑了：

- streaming / non-streaming 分离
- trace 可复盘
- verification / impact / reviewer 可结构化评估
- SWE-bench 适配

这让它比很多“只能交互 demo”的项目更适合继续打磨。

---

## 18. 这个架构当前的局限

理解优点也要理解边界。

### 18.1 仍然很依赖 prompt 纪律

虽然系统状态已经很多了，但计划质量、搜索策略、是否过度试探，还是受模型本身影响很大。

### 18.2 Memory 仍然偏启发式

默认 markdown memory 很工程友好，但相关性搜索和 merge 规则还是启发式，不是更强的语义检索系统。

### 18.3 工具仍以 Python 代码库为主优化

`read_symbol`、`find_symbol_callers`、`python_structural_edit` 这些都是代码里真实注册的工具名；它们对 Python 很强，但对其他语言的结构化支持还比较弱。

### 18.4 Verification 仍然是启发式 gate

它已经比“无验证”强很多，但还不是严格的“分层验证流水线”。

---

## 19. 你可以从这个系统学到什么

如果你把 NZ-Coder 当成学习样本，最值得学的不是“怎么写一个聊天机器人”，而是下面这些工程思想。

### 19.1 LLM 系统必须有状态层

不要把所有状态都寄托在对话历史里。
系统自己要维护客观事实。

### 19.2 工具调用必须平台化

要有：

- schema
- registry
- dispatch
- permission
- result classification

否则工具一多就失控。

### 19.3 写操作必须可回滚

只要 agent 会改文件，就必须考虑：

- 中途失败怎么办
- 多文件半成功怎么办
- 用户后续又改了怎么办

### 19.4 “完成”必须可验证

agent 最容易骗你的地方就是：

> patch 已经写了，所以它说“完成了”。

工程上必须反过来：

> 没验证通过，就不算完成。

### 19.5 可观测性不是锦上添花

trace、change set、run evidence、review summary，这些不是“以后再加”。
对 agent 系统来说，它们是能否持续优化的基础。

---

## 20. 最后给你的读码建议

如果你接下来真要深入学习，不要一上来试图“看懂所有文件”。

更好的方法是：

1. 先跟一遍 `AgentLoop.run()`
2. 看一遍一次 `write_file -> verify -> finalize` 的完整链路
3. 看一遍一次 `tool error -> recovery diagnostic -> retry` 的链路
4. 看一遍 `memory_block + scratchpad + runtime_state` 是怎么注入 prompt 的
5. 最后再看 Greenfield / reviewer / swebench 这些扩展层

这样你会先理解“主干骨架”，而不是先淹死在功能点里。

---

## 21. 一句话总结

**NZ-Coder 的核心，不是一个会调工具的 LLM，而是一套围绕 LLM 构建的、显式状态驱动的、安全可回滚、可验证、可观测的代码执行系统。**

这也是它最值得学的地方。
# Runtime publication boundaries

Each user submission owns a fresh `interaction_run_id`. Provider text and tool
arguments remain attempt-private until their output/tool guardrails approve
them. `ApprovedModelResult` and `ApprovedToolBatch` are the only values allowed
to enter Message/Part state. HTTP attach snapshots separate the complete
`timeline` from the current `run`, and both local and remote terminal paths
reduce current-run events through `RunViewReducer`.

Public errors pass through `PublicError`; guardrail reasons, unapproved model
text, raw tool envelopes, internal agent-as-tool answers, and retired attempt
deltas are excluded from Session events and the event journal. The legacy SDK
`on_token` callback is final-text-only and deprecated in favor of
`on_final_text` or structured `on_event` consumption.
