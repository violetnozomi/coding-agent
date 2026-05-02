# NZ-Coder 面试学习文档

> 一个从零实现的终端 AI 编程助手（类 Claude Code / Cursor Agent），涵盖 Agent Loop、Tool Dispatch、流式输出、事务回滚、权限沙箱、Trace Logging、Session Resume、Agent Diff/Revert、自动化评测等核心机制。

---

## 目录

1. [项目总览](#1-项目总览)
2. [架构设计](#2-架构设计)
3. [核心模块详解](#3-核心模块详解)
   - 3.1 Agent Loop（loop.py）
   - 3.2 Tool Registry & Dispatch（tools/\_\_init\_\_.py）
   - 3.3 流式输出（Streaming）
   - 3.4 事务编辑与回滚（Transaction）
   - 3.5 上下文压缩（Context Compaction）
   - 3.6 权限系统（Permissions）
   - 3.7 错误恢复（Recovery）
   - 3.8 子代理（Subagent）
   - 3.9 记忆系统（Memory）
   - 3.10 技能加载（Skills）
   - 3.11 自动化评测（Benchmark）
   - 3.12 Trace Logging（trace.py）
   - 3.13 Change Tracking / Diff / Revert（changes.py）
   - 3.14 Session Resume（sessions.py）
   - 3.15 Workspace & Git Awareness（workspace.py）
   - 3.16 Fake LLM Loop Tests（tests/test_loop_fake.py）
   - 3.17 Patch / Shell 加固
   - 3.18 Hard Refactor 失败复盘：AST 工具化修复
4. [关键设计决策与面试高频考点](#4-关键设计决策与面试高频考点)
5. [数据流 & 时序图](#5-数据流--时序图)
6. [面试问答精选（Q&A）](#6-面试问答精选qa)
7. [与工业级系统的对比](#7-与工业级系统的对比)
8. [扩展方向](#8-扩展方向)

---

## 1. 项目总览

### 定位

NZ-Coder 是一个 **终端 AI 编程代理**，对标 Anthropic Claude Code、Cursor Agent、Aider 等产品。用户在终端输入自然语言指令，Agent 自主调用工具（读文件、写文件、执行命令、搜索代码等）完成编程任务。

### 技术栈

| 层级 | 技术 |
|------|------|
| LLM 接口 | OpenAI SDK（function calling），兼容 qwen/deepseek/gpt-4o |
| 终端 UI | Rich（Live 流式渲染、Markdown、Panel） |
| 语言 | Python 3.9+，纯标准库 + 3 个依赖 |
| 测试 | 烟雾测试 + Fake LLM Loop 测试 + 自动化 Benchmark |

### 代码量

- **核心模块**：~2900 行 Python（含 tools 子模块）
- **Benchmark**：13 个评测任务，输出 JSON/Markdown 报告
- **测试**：23 个 pytest 测试，包含 Fake LLM Agent Loop 测试

### 项目结构

```
nz-coder/
├── nz_coder/
│   ├── __init__.py          # 版本号
│   ├── __main__.py          # python -m nz_coder 入口
│   ├── changes.py           # ★ Agent-authored diff/revert 变更追踪
│   ├── cli.py               # Rich REPL + StreamingRenderer
│   ├── command_policy.py    # ★ shell 命令安全分类
│   ├── config.py            # 环境变量配置
│   ├── loop.py              # ★ Agent Loop 核心（流式/非流式双模式）
│   ├── context.py           # 上下文压缩（persist + micro_compact + auto_compact）
│   ├── permissions.py       # 权限管道（deny→mode→allow→ask）
│   ├── recovery.py          # 错误恢复（重试 + 指数退避）
│   ├── sessions.py          # ★ 会话保存与恢复
│   ├── transaction.py       # ★ 事务编辑（backup → track → commit/rollback）
│   ├── trace.py             # ★ JSONL 运行链路
│   ├── subagent.py          # 子代理隔离执行
│   ├── memory.py            # 跨会话持久记忆
│   ├── skills.py            # SKILL.md 按需加载
│   ├── prompt.py            # 系统提示词组装
│   ├── workspace.py         # ★ workspace/git 状态感知
│   ├── benchmark.py         # ★ 自动化评测（13 任务 + JSON/Markdown 报告）
│   └── tools/
│       ├── __init__.py      # 注册表 + dispatch
│       ├── bash.py          # Shell 命令执行
│       ├── files.py         # 文件 CRUD + 事务追踪
│       ├── python_ast.py    # ★ AST 符号检查与结构化 Python 编辑
│       ├── search.py        # grep + glob
│       └── todo.py          # 会话任务清单
├── skills/code-review/SKILL.md
├── tests/test_smoke.py
├── requirements.txt
└── .env.example
```

---

## 2. 架构设计

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────┐
│                   CLI (cli.py)                   │
│  Rich REPL ← StreamingRenderer (Live + Markdown) │
└────────────────────┬────────────────────────────┘
                     │ history[] + callbacks
                     ▼
┌─────────────────────────────────────────────────┐
│              Agent Loop (loop.py)                │
│                                                  │
│  while True:                                     │
│    ① micro_compact(messages)                     │
│    ② if tokens > MAX → auto_compact()            │
│    ③ LLM call (streaming / non-streaming)        │
│    ④ if no tool_calls → return text              │
│    ⑤ txn.begin() if has write ops                │
│    ⑥ for each tool_call:                         │
│       permissions.check() → dispatch() → output  │
│    ⑦ txn.commit() or txn.rollback()             │
│    ⑧ todo reminder check                        │
│    → loop back to ①                              │
└──────┬──────────┬──────────┬────────────────────┘
       │          │          │
       ▼          ▼          ▼
┌──────────┐ ┌─────────┐ ┌──────────────────┐
│ Tools    │ │ Context │ │  Transaction Mgr │
│ Registry │ │ Manager │ │  (backup/rollback)│
│ dispatch │ │ compact │ │                  │
└──────────┘ └─────────┘ └──────────────────┘
       │
       ├── bash.py       (subprocess)
       ├── files.py      (read/write/edit + txn.track)
       ├── search.py     (grep/glob)
       ├── todo.py       (session planning)
       ├── subagent.py   (child agent)
       ├── memory.py     (persistent .memory/)
       └── skills.py     (SKILL.md loader)
```

### 2.2 核心设计原则

1. **单循环驱动**：1 个 `while True` 循环处理所有交互，而不是 LangGraph 式的 DAG
2. **回调解耦**：`on_tool`/`on_text`/`on_token` 回调分离关注点
3. **注册表模式**：工具通过 `register()` 自注册，loop.py 不硬编码任何工具
4. **事务安全**：写操作包裹在 transaction 中，失败自动回滚
5. **可观测性**：每次 run 写 JSONL trace，方便定位失败
6. **可恢复性**：会话、trace、change set 都持久化到 `.nz-coder/`

---

## 3. 核心模块详解

### 3.1 Agent Loop（loop.py）— 最核心

#### 面试考点

> **"请描述你的 Agent Loop 是如何工作的？"**

Agent Loop 是整个系统的心脏。它实现了一个标准的 **ReAct（Reasoning + Acting）循环**：

```
User Input → LLM → [Tool Call(s)] → Tool Result(s) → LLM → ... → Final Text
```

#### 关键代码流程

```python
class AgentLoop:
    def run(self, messages, on_tool, on_text, on_token, stream):
        while True:
            # 1. 上下文压缩
            micro_compact(messages)
            if estimate_tokens(messages) > MAX_CONTEXT_TOKENS:
                messages[:] = auto_compact(messages, ...)

            # 2. 调用 LLM（流式或非流式）
            if stream:
                result = self._call_streaming(api_messages, on_token)
            else:
                result = self._call_non_streaming(api_messages)

            content_text, tool_calls = result

            # 3. 无工具调用 → 返回文本，退出循环
            if not tool_calls:
                return

            # 4. 有写操作 → 开启事务
            if has_write:
                self.txn.begin()

            # 5. 逐个执行工具调用
            for tc in tool_calls:
                decision = self.permissions.check(fn_name, fn_args)
                output = dispatch(fn_name, fn_args)

            # 6. 事务提交或回滚
            if all_succeeded:
                self.txn.commit()
            else:
                self.txn.rollback()
```

#### 为什么用 `while True` 而不是递归？

- **避免栈溢出**：复杂任务可能 50+ 轮工具调用
- **上下文在列表中原地修改**：`messages[:] = ...` 实现无拷贝压缩
- **流式兼容**：循环内可以在每轮切换 stream/non-stream

#### 流式 vs 非流式双模式

| 特性 | `_call_streaming` | `_call_non_streaming` |
|------|-------------------|-----------------------|
| API 参数 | `stream=True` | `stream=False` |
| Token 接收 | chunk-by-chunk | 一次性 |
| 工具调用拼接 | `tool_calls_map[index]` 累积 delta | `msg.tool_calls` 直接取 |
| 使用场景 | CLI 交互 | Benchmark 评测 |

#### 流式 Tool Call 拼接（面试重点）

```python
# 流式响应中，tool call 是分片到达的：
# chunk1: {index: 0, id: "call_xxx", function: {name: "write_fi"}}
# chunk2: {index: 0, function: {name: "le", arguments: '{"path":'}}
# chunk3: {index: 0, function: {arguments: '"test.py", "content": "hello"}'}}

tool_calls_map = {}  # index → accumulated tool call

for chunk in stream:
    for tc_delta in chunk.choices[0].delta.tool_calls:
        idx = tc_delta.index
        if idx not in tool_calls_map:
            tool_calls_map[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
        entry = tool_calls_map[idx]
        if tc_delta.id:
            entry["id"] = tc_delta.id
        if tc_delta.function.name:
            entry["function"]["name"] += tc_delta.function.name  # 拼接
        if tc_delta.function.arguments:
            entry["function"]["arguments"] += tc_delta.function.arguments  # 拼接
```

**核心原理**：OpenAI streaming API 将一个 tool call 分成多个 delta chunk 发送，用 `index` 标识是哪个 tool call，`name` 和 `arguments` 都需要字符串拼接。

---

### 3.2 Tool Registry & Dispatch（tools/\_\_init\_\_.py）

#### 面试考点

> **"你的工具系统是怎么设计的？如何添加新工具？"**

采用 **自注册模式**（Self-Registration Pattern）：

```python
# tools/__init__.py - 核心只有 3 个数据结构
TOOL_SPECS: list[dict] = []     # OpenAI function calling 格式
TOOL_HANDLERS: dict[str, Callable] = {}

def register(name, description, parameters, handler):
    TOOL_SPECS.append({"type": "function", "function": {...}})
    TOOL_HANDLERS[name] = handler

def dispatch(name, arguments) -> str:
    handler = TOOL_HANDLERS[name]
    return str(handler(**arguments))
```

#### 添加新工具只需 3 步

```python
# 1. 实现函数
def my_tool(param1: str) -> str:
    return "result"

# 2. 注册
register(name="my_tool", description="...", parameters={...}, handler=my_tool)

# 3. 在 loop.py 顶部 import 以触发注册
import nz_coder.tools.my_module  # noqa: F401
```

#### 设计优势

- **零耦合**：loop.py 不知道有哪些具体工具
- **统一错误处理**：dispatch 捕获所有异常统一返回 `"Error: ..."`
- **动态扩展**：运行时 import 即注册

#### 与 LangChain/LangGraph 对比

| | NZ-Coder | LangChain |
|---|---|---|
| 工具定义 | `register()` 自注册 | `@tool` 装饰器 + BaseTool |
| 调度 | 简单 dict 查找 | 复杂 Agent Executor |
| 复杂度 | ~50 行 | 数千行 |

---

### 3.3 流式输出（Streaming）

#### 面试考点

> **"你是如何实现流式输出的？用户看到 token 逐字出现的体验是怎么实现的？"**

#### 三层架构

```
LLM (stream=True) → AgentLoop._call_streaming() → on_token callback → StreamingRenderer
```

**第一层：LLM 流式调用**
```python
stream = client.chat.completions.create(model=..., stream=True)
for chunk in stream:
    if chunk.choices[0].delta.content:
        on_token(chunk.choices[0].delta.content)  # 回调
```

**第二层：StreamingRenderer**
```python
class StreamingRenderer:
    def __init__(self):
        self._buffer = []
        self._live = None

    def start(self):
        self._live = Live("", console=console, refresh_per_second=8)
        self._live.start()

    def on_token(self, token):
        if token is None:       # End-of-stream 信号
            self._finish()
            return
        self._buffer.append(token)
        text = "".join(self._buffer)
        self._live.update(Markdown(text))  # 实时更新终端
```

**第三层：Rich Live**
- `Rich.Live` 使用 ANSI 转义序列在终端原地刷新
- `refresh_per_second=8` 限制刷新率，避免过度渲染
- `Markdown(text)` 将累积文本解析为格式化 Markdown

#### 为什么选 Rich.Live 而不是逐字 print？

| 方案 | 优点 | 缺点 |
|------|------|------|
| 逐字 `print(end="")` | 简单 | 无法渲染 Markdown，代码块会乱 |
| Rich.Live + Markdown | 实时 Markdown 渲染 | 短暂闪烁，需要 buffer |
| Textual TUI | 完整 UI | 过重，不适合 CLI agent |

#### 关键设计：None 信号

`on_token(None)` 表示流结束，触发 `_finish()` 停止 Live 并做最终渲染。这确保了流式中途如果 agent 要调用工具，Live 会正确停止。

---

### 3.4 事务编辑与回滚（Transaction）

#### 面试考点

> **"多文件编辑时，如果中途失败了怎么办？你是怎么保证原子性的？"**

#### 核心思路

**备份-恢复模式（Snapshot-Restore）**：

```
begin()  → 创建临时目录
track()  → 在写入前拷贝原文件到临时目录
commit() → 删除临时目录（丢弃备份）
rollback() → 从临时目录恢复原文件
```

#### 代码关键路径

```python
class TransactionManager:
    def begin(self):
        self._active = True
        self._backup_dir = Path(tempfile.mkdtemp(prefix="nzcoder_txn_"))

    def track(self, file_path):
        """在文件被修改前调用"""
        if abs_path in self._backups:
            return  # 已追踪，只备份第一次
        if source.exists():
            shutil.copy2(source, backup)    # 已有文件 → 备份
            self._backups[abs_path] = backup
        else:
            self._backups[abs_path] = None  # 新文件 → 标记

    def rollback(self):
        for abs_path, backup in self._backups.items():
            if backup is None:
                Path(abs_path).unlink()     # 新文件 → 删除
            else:
                shutil.copy2(backup, abs_path)  # 已有文件 → 恢复
```

#### 与 files.py 的集成（依赖注入）

```python
# files.py
_txn_manager = None

def set_txn_manager(txn):
    global _txn_manager
    _txn_manager = txn

def write_file(path, content):
    txn = _txn_manager
    if txn and txn.active:
        txn.track(path)          # ← 写入前自动追踪
    fp.write_text(content)
```

**为什么用全局注入而不是参数传递？**
- 工具函数的签名由 OpenAI function calling schema 定义
- 不能给 `write_file(path, content)` 加额外参数
- 全局注入在 `AgentLoop.__init__()` 中完成，避免循环导入

#### 与数据库事务的对比

| 特性 | 数据库事务 | NZ-Coder 事务 |
|------|-----------|---------------|
| 原子性 | WAL / redo log | 文件备份到 tmpdir |
| 隔离性 | MVCC / 锁 | 无（单线程，不需要） |
| 持久性 | fsync | shutil.copy2 |
| 嵌套 | SAVEPOINT | 忽略内层 begin（保持外层） |
| 粒度 | 行级/页级 | 文件级 |

---

### 3.5 上下文压缩（Context Compaction）

#### 面试考点

> **"长对话超过模型上下文窗口怎么办？"**

三级压缩策略：

**第一级：persist_large_output**（30KB 触发）
```python
def persist_large_output(tool_call_id, output):
    if len(output) > 30000:
        # 写入 .nz-coder/tool-results/{id}.txt
        # 返回 preview (前 2000 字符) + 文件路径
```

**第二级：micro_compact**（每轮自动）
```python
def micro_compact(messages):
    # 只保留最近 3 个 tool result 原文
    # 旧的 tool result → "[Earlier tool result compacted. Re-run if needed.]"
```

**第三级：auto_compact**（token 超限时）
```python
def auto_compact(messages, client, model):
    # 1. 保存完整对话到 transcript_{timestamp}.jsonl
    # 2. 用 LLM 生成结构化摘要
    # 3. 替换整个 messages 为 1 条摘要消息
```

#### 为什么是三级而不是一级？

- 一级和二级**无 API 调用**，零成本
- 三级**消耗 1 次 API call**，但能将 100K→4K tokens
- 逼近窗口时才触发三级，最大化利用上下文

#### token 估算

```python
def estimate_tokens(messages):
    return len(json.dumps(messages)) // 4  # 粗略估计：4 chars ≈ 1 token
```

不使用 tiktoken 是因为：
- 需要兼容多个模型（qwen/deepseek/gpt-4o），各模型 tokenizer 不同
- 4 字符估算误差 <20%，足够触发压缩

---

### 3.6 权限系统（Permissions）

#### 面试考点

> **"如何防止 Agent 执行危险操作？"**

四级管道：

```
deny（硬规则）→ mode（模式策略）→ allow（安全白名单）→ ask（用户确认）
```

```python
class PermissionManager:
    def check(self, tool_name, tool_input):
        # Step 0: bash 危险命令硬拦截（sudo, rm -rf, mkfs, dd）
        if tool_name == "bash":
            for pattern, label in DANGEROUS_BASH:
                if re.search(pattern, command):
                    return {"behavior": "deny"}

        # Step 1: plan 模式 → 所有写操作拦截
        if self.mode == "plan" and tool_name in WRITE_TOOLS:
            return {"behavior": "deny"}

        # Step 2: 读操作 → 永远放行
        if tool_name in READ_TOOLS:
            return {"behavior": "allow"}

        # Step 3: auto 模式 → 写操作也放行
        if self.mode == "auto":
            return {"behavior": "allow"}

        # Step 4: default 模式 → 写操作需要确认
        return {"behavior": "ask"}
```

三种模式：

| 模式 | 读操作 | 写操作 | 适用场景 |
|------|--------|--------|----------|
| `default` | ✅ 放行 | ❓ 逐个确认 | 日常使用 |
| `auto` | ✅ 放行 | ✅ 自动放行 | Benchmark / 信任场景 |
| `plan` | ✅ 放行 | ❌ 全部拒绝 | 只看不改 |

---

### 3.7 错误恢复（Recovery）

#### 面试考点

> **"API 调用失败了怎么办？"**

```python
class RecoveryState:
    max_retries = 3
    backoff_base = 2.0

    def record_error(self, error):
        self.consecutive_errors += 1
        return {
            "should_retry": self.consecutive_errors <= 3,
            "should_abort": self.consecutive_errors > 3,
        }

    def backoff_wait(self):
        wait = min(2 ** consecutive_errors, 30)  # 2s, 4s, 8s, max 30s
        time.sleep(wait)
```

**指数退避**：重试间隔 2→4→8→30s，避免 rate limit 雪崩
**连续成功清零**：`record_success()` 重置计数器
**中止保护**：超过 3 次连续失败 → agent 主动停止，避免无限循环

---

### 3.8 子代理（Subagent）

#### 面试考点

> **"为什么需要子代理？和主循环有什么区别？"**

**核心价值：上下文隔离**

```
主 Agent（上下文很长，有很多历史）
  ↓
  用 task 工具委派："研究一下这个 API 的用法"
  ↓
子 Agent（全新上下文，只有 1 条任务描述）
  → 自主执行工具调用（最多 30 轮）
  → 返回一条总结
  ↓
主 Agent 继续（上下文中只增加了总结部分）
```

**优势**：
- 避免探索性操作污染主上下文
- 子代理有独立的工具集（默认只读）
- 失败不影响

---

### 3.9 记忆系统（Memory）

```python
class MemoryManager:
    # 持久化到 .nz-coder/memory/{name}.md
    # YAML frontmatter 格式：
    # ---
    # name: python-style
    # description: 用户偏好 Python 代码风格
    # type: user
    # ---
    # 偏好 type hints，4 空格缩进 ...
```

四种记忆类型：`user`（用户偏好）、`project`（项目约定）、`feedback`（反馈历史）、`reference`（参考资料）

---

### 3.10 技能加载（Skills）

```python
class SkillLoader:
    # 扫描 skills/{name}/SKILL.md
    # YAML frontmatter 提取 name + description
    # 通过 load_skill 工具按需注入到对话中
```

**与 Cursor Rules / Claude Instructions 对标**：将领域知识打包为可复用模块。

---

### 3.11 自动化评测（Benchmark）

#### 面试考点

> **"怎么评估你的 Agent 的能力？用了什么评测方法？"**

当前 13 个任务覆盖 3 个难度级别和多种 coding-agent 能力：

| 任务 | 难度 | 能力 | 验证方式 |
|------|------|------|----------|
| FizzBuzz | Easy | 文件创建 + 代码生成 | subprocess 运行，检查输出 |
| BugFix | Easy | 读代码 + 定位 bug + 修复 | subprocess 运行，检查结果 |
| BoundaryBugFix | Easy | 边界条件修复 | import + assert |
| JsonConfigUpdate | Easy | 结构化 JSON 编辑 | json.loads + 字段检查 |
| DocumentationUpdate | Easy | 文档更新 | 文本检查 |
| AddFunction | Medium | 读现有代码 + 追加函数 | import + assert |
| WriteTests | Medium | 理解代码 + 生成测试 | pytest 运行 |
| PytestRepair | Medium | 根据失败测试修代码 | pytest 运行 |
| MultiFileBugFix | Medium | 多文件定位和修复 | 运行入口脚本 |
| CliArgparse | Medium | CLI 参数行为修改 | subprocess 检查输出 |
| RefactorClass | Hard | 重构 + 提取函数 + 保持行为 | 原始功能 + 新接口双验证 |
| MultiFileCreate | Hard | 创建多文件包 + 导出 | import 验证整个包 |
| PublicApiPreserve | Hard | 保留 public API 的重构 | import + assert |

#### 评测架构

```python
def run_task(task):
    # 1. 临时 WORKDIR 切换到 .nz-coder/benchmark/
    config.WORKDIR = BENCH_DIR

    # 2. setup() 准备初始文件
    task.setup()

    # 3. 创建 AgentLoop（auto 权限，非流式）
    agent = AgentLoop(system_prompt, permission_mode="auto")

    # 4. 运行 agent
    agent.run(messages, on_tool=log_tool, stream=False)

    # 5. verify() 检查结果
    result = task.verify()

    # 6. 返回结构化 JSON，并写入 Markdown 报告
    return {
        "task_id": ..., "passed": ..., "reason": ...,
        "duration": ..., "turns": ..., "tool_calls": ...,
        "task_type": ..., "failure_category": ..., "trace": ...,
    }
```

#### 验证策略

所有验证都是**执行验证**（不是字符串匹配）：
- 启动子进程运行生成的代码
- 用 `assert` 检查行为正确性
- 超时保护（10s/30s）
- 输出 `.nz-coder/benchmark/report.json` 和 `.nz-coder/benchmark/report.md`

---

### 3.12 Trace Logging（trace.py）

#### 面试考点

> **"Agent 出问题时你怎么定位？有没有可观测性？"**

Trace Logging 是把 Agent 每一次运行中的关键事件写成 JSONL：

```json
{"event": "run_start", "run_id": "...", "message_count": 1}
{"event": "llm_request", "token_estimate": 1200}
{"event": "llm_response", "tool_calls": 1}
{"event": "tool_call", "name": "read_file", "status": "ok"}
{"event": "run_end", "status": "completed"}
```

核心文件：`trace.py`

```python
class TraceRecorder:
    def log(self, event: str, **payload):
        row = {
            "ts": time.time(),
            "run_id": self.run_id,
            "event": event,
            **payload,
        }
        append_jsonl(row)
```

#### 为什么用 JSONL？

- 追加写入简单，不需要一次性加载完整文件
- 每行一个事件，适合流式日志
- 后续可以接入 UI、分析脚本、benchmark 报告

#### 记录了哪些事件？

| 事件 | 含义 |
|------|------|
| `run_start` | 一次 agent run 开始 |
| `llm_request` | 即将请求模型 |
| `llm_response` | 模型返回文本/tool calls |
| `tool_call` | 执行本地工具 |
| `transaction_rollback` | 多文件写入失败后回滚 |
| `compact` | 上下文压缩 |
| `api_error` | API 调用失败 |
| `run_end` | run 完成/中止/达到最大轮数 |

CLI 里可以用 `/trace` 查看最近一次摘要。

---

### 3.13 Change Tracking / Diff / Revert（changes.py）

#### 面试考点

> **"Agent 改坏代码怎么办？如何只回滚 Agent 自己的修改？"**

`TransactionManager` 解决的是“同一轮工具调用中失败要回滚”。  
`ChangeTracker` 解决的是“本轮 Agent 已经成功改完了，但用户后来想审查或撤销”。

保存位置：

```text
.nz-coder/changes/{run_id}.json
```

记录格式：

```json
{
  "path": "app.py",
  "before_exists": true,
  "before": "原始内容",
  "after_exists": true,
  "after": "修改后内容"
}
```

#### `/diff`

读取 change set，用 `difflib.unified_diff` 生成统一 diff：

```diff
--- a/app.py
+++ b/app.py
@@
- total = 1
+ total = 0
```

#### `/revert-last`

回滚前检查：

```text
当前文件内容 == tracked after-state
  -> 可以回滚到 before
当前文件内容 != tracked after-state
  -> 拒绝回滚，避免覆盖用户后续改动
```

这点是面试亮点：不是简单写回旧文件，而是保护用户在 Agent 之后的手动修改。

---

### 3.14 Session Resume（sessions.py）

#### 面试考点

> **"长任务中断后怎么继续？"**

对话历史 `history` 是 Agent 的短期记忆。Session Resume 就是把这个列表持久化：

```json
{
  "session_id": "demo",
  "timestamp": "2026-05-01 12:00:00",
  "workspace": ".../nz-coder",
  "model": "qwen-plus",
  "mode": "default",
  "messages": [...]
}
```

CLI：

```text
/save-session demo
/sessions
/resume demo
```

REPL 每轮结束后自动写 `autosave` 和 `latest`，所以用户关闭终端后可以恢复。

恢复时会检查 workspace，防止把 A 项目的会话恢复到 B 项目里。

---

### 3.15 Workspace & Git Awareness（workspace.py）

#### 面试考点

> **"Agent 怎么知道当前项目状态？怎么避免覆盖用户已有改动？"**

`workspace.py` 提供：

- `project_profile()`：根据 `pyproject.toml`、`requirements.txt`、`package.json` 等识别项目类型
- `git_status_short()`：展示 dirty files
- `git_file_status(path)`：写文件前检查目标文件是否已有 git 改动
- `status_report()`：给 `/status` 使用

`/status` 输出：

```text
Version
Model
Workspace
Permission mode
Conversation messages
Latest trace
Latest change set
Project profile
Git dirty files
```

文件工具写入前如果发现目标文件已经 dirty，会在工具结果中给 warning。

---

### 3.16 Fake LLM Loop Tests（tests/test_loop_fake.py）

#### 面试考点

> **"怎么测试 AgentLoop？真实 LLM 输出不稳定怎么办？"**

用 Fake OpenAI Client 模拟：

```python
fake = FakeClient([
    FakeResponse(FakeMessage(tool_calls=[
        FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"})
    ])),
    FakeResponse(FakeMessage("done")),
])
```

这样可以不用真实 API，也能测试：

- tool call 执行后继续下一轮 LLM
- 工具参数 JSON 坏了时会反馈错误
- API 短暂失败会 retry
- trace 文件能记录 run_start / llm_response / run_end

这是工程化亮点：核心 runtime 有确定性单元测试。

---

### 3.17 Patch / Shell 加固

#### apply_patch

现在支持：

| op | 含义 |
|----|------|
| `replace` | 精确替换，`old_text` 必须唯一匹配 |
| `create` | 创建文件 |
| `delete` | 删除文件，可用 `old_text` 做 guard |
| `dry_run` | 只预览 diff，不写入 |

关键策略：

```text
先完整预检所有 hunks
全部合法后才开始写文件
写入后返回 unified diff
```

#### shell

`bash` 执行前通过 `command_policy.py` 分类：

```text
read-only -> 允许
mutating -> default 模式询问 / plan 模式拒绝
dangerous -> 直接拒绝
```

此外支持 timeout 参数，避免命令卡死。

---

### 3.18 Hard Refactor 失败复盘：AST 工具化修复

#### 背景

`refactor_class` 是 benchmark 里的 hard 任务，要求：

```text
从 UserManager.create_user 中提取模块级 validate_email(email)
create_user 必须调用 validate_email
原有 create/get/delete 行为必须保持
```

第一次真实 benchmark 结果是 12/13，失败点是：

```text
ImportError: cannot import name 'validate_email' from 'user_manager'
```

这不是普通语法错误，而是 Agent 没有稳定完成“函数提取 + public API 导出 + 行为保持”这个重构闭环。

#### 排查路径

工程上不能只看最终 ImportError，要看 tool log / trace：

```text
1. 模型读到了 user_manager.py
2. 多次 apply_patch / edit_file 返回 old_text not found
3. 模型改用 write_file 覆盖文件
4. 最终文件没有正确导出 validate_email，或者 validate_email 行为不是严格 bool
```

根因分两类：

```text
工具层：exact text patch 对函数级重构太脆，空格/上下文稍微不一致就失败
验证层：只检查“有没有函数名”不够，还要验证 AST 调用关系和真实行为
```

#### 修复一：patch 失败时返回 nearby context

`edit_file` / `apply_patch` 保持精确匹配，这是安全设计。但当 `old_text not found` 时，工具现在会返回附近真实上下文，帮助模型下一轮基于实际文件重试。

面试表达：

> 我没有取消 exact match 的安全约束，而是在失败反馈里增加 nearby context，让模型可以自我修正，同时保留误编辑防护。

#### 修复二：新增 `python_symbol_check`

文件：`nz_coder/tools/python_ast.py`

它用 Python AST 检查：

```text
validate_email 是否是模块级函数
UserManager 是否存在
UserManager.create_user 是否存在
create_user 是否调用 validate_email
```

这比字符串搜索可靠，因为 AST 能区分模块函数、类、方法和调用节点。

#### 修复三：新增 `python_structural_edit`

这是关键修复。它按 AST symbol 定位，不依赖模型猜 `old_text`：

```text
1. ast.parse(source)
2. 找到 UserManager.create_user 的 lineno/end_lineno
3. 替换整个方法
4. 在 UserManager class 前插入 validate_email
5. 写回文件并返回 unified diff
```

适用边界：

```text
简单局部修改 -> edit_file/apply_patch
Python 函数/方法级重构 -> python_structural_edit
语义验证 -> python_symbol_check + 行为命令
```

#### 修复四：接入 runtime，而不是只写工具函数

新增工具后，还必须接入这些地方：

```text
loop.py：import nz_coder.tools.python_ast 触发注册
permissions.py：把 python_structural_edit 放入 WRITE_TOOLS
loop.py：has_write 包含 python_structural_edit，确保事务覆盖
prompt.py：提示模型函数/方法级重构优先使用 structural edit
benchmark.py：refactor_class 描述中要求 symbol check 和 exact behavior check
tests：增加结构化编辑单测和 fake LLM hard refactor 回归测试
```

这个点面试很重要：**工具能力必须穿透注册、权限、事务、提示词、评测、测试整个链路**。

#### 验证结果

本地测试：

```text
23 passed
```

并且新增 fake LLM loop 测试：

```text
Fake LLM 返回 python_structural_edit tool_call
AgentLoop 执行工具
Fake LLM 返回 python_symbol_check tool_call
测试验证 validate_email 和 UserManager 行为
```

真实 benchmark 重新运行时，API 返回：

```text
Arrearage: Access denied
```

这是账号欠费/服务不可用，不是本轮代码失败。因此当前严谨结论是：

```text
hard refactor 的工程修复已实现并有离线回归测试；
真实 benchmark 需要 API 恢复后重新跑，不能伪称已经 13/13。
```

#### 面试话术

> 我在 benchmark 中发现 hard refactor 失败，不是简单调 prompt，而是按 trace 定位到 exact text edit 对结构化重构不稳定。然后我补了 AST 级 symbol check 和 structural edit，把 Python 函数/方法替换从字符串匹配升级为 AST 行号定位，并接入权限、事务、prompt 和 fake LLM 回归测试。这样既保留了普通 patch 的安全性，又让复杂重构有更稳定的工具路径。

---

## 4. 关键设计决策与面试高频考点

### 4.1 为什么不用 LangChain / LangGraph？

| 因素 | 框架方案 | 自己实现 |
|------|----------|----------|
| 学习/面试价值 | 调 API | 深刻理解原理 |
| 代码量 | ~50 行胶水 | ~2900 行，但完全可控 |
| 定制性 | 受限于抽象 | 想改什么改什么 |
| 调试 | 黑盒 | 全透明 |
| 依赖 | 数十个包 | 3 个包 |

**面试话术**："我选择从零实现是为了深入理解 Agent 的核心机制。框架隐藏了太多细节——流式 tool call 的拼接、上下文窗口的管理、事务回滚——这些都是工程中的关键难点。自己实现让我对每一层都有完整的理解。"

### 4.2 为什么用 OpenAI Function Calling 而不是 prompt-based tool use？

```
# Function Calling（本项目使用）
tools=[{"type": "function", "function": {"name": "write_file", ...}}]
→ 模型返回结构化 JSON：{"name": "write_file", "arguments": {"path": "x", "content": "y"}}

# Prompt-based（某些开源模型方案）
system: "When you want to write a file, respond with <tool>write_file</tool>..."
→ 需要自己解析 XML/JSON，容易出错
```

**优势**：结构化输出、原生支持、多个模型兼容（qwen/deepseek/gpt-4o 都支持）

### 4.3 为什么 edit_file 要求精确匹配？

```python
def edit_file(path, old_text, new_text):
    count = content.count(old_text)
    if count == 0:
        return "Error: old_text not found"
    if count > 1:
        return "Error: old_text matches multiple locations"
    content.replace(old_text, new_text, 1)
```

**原因**：LLM 可能幻觉出错误的代码位置。精确匹配 + 唯一性检查 = 防止误编辑。
**对标**：Claude Code 的 `replace_string_in_file` 也是同样策略。

### 4.4 安全设计（OWASP 考量）

| 威胁 | 防护 |
|------|------|
| 路径穿越 | `_safe_path()` 用 `is_relative_to(WORKDIR)` 检查 |
| 命令注入 | `DANGEROUS_BASH` 正则拦截 sudo/rm -rf/mkfs/dd |
| 无限循环 | `RecoveryState` 3 次失败后中止 |
| 资源耗尽 | bash 超时 120s，上下文压缩防 OOM |
| 提权 | plan 模式禁止所有写操作 |

---

## 5. 数据流 & 时序图

### 5.1 一次完整的编辑交互

```
User: "帮我修复 app.py 中的 bug"
  │
  ▼ history.append({"role": "user", "content": "..."})
  │
AgentLoop.run()
  │
  ├─→ LLM call (stream=True)
  │     chunk1: "我来看看 app.py..."   → on_token("我来看看")
  │     chunk2: ""                     → on_token(" app.py...")
  │     chunk3: tool_call(read_file)   → 累积到 tool_calls_map
  │
  ├─→ dispatch("read_file", {"path": "app.py"})
  │     → permissions.check → allow
  │     → read_file("app.py") → 文件内容
  │     → on_tool("read_file", "文件内容...")
  │
  ├─→ LLM call #2 (with tool result in context)
  │     → 返回 tool_call(edit_file, {old_text, new_text})
  │
  ├─→ txn.begin()
  ├─→ txn.track("app.py")     ← 备份原文件
  ├─→ dispatch("edit_file", {...})
  │     → permissions.check → ask → 用户确认 y
  │     → edit_file() → "Edited app.py"
  ├─→ txn.commit()            ← 丢弃备份
  │
  ├─→ LLM call #3
  │     → "已修复 bug，将 total=1 改为 total=0"
  │     → on_token("已修复...")
  │
  └─→ return (循环结束)
```

### 5.2 事务回滚流程

```
LLM → edit_file(A.py) ✅ → edit_file(B.py) ❌ Error
  │
  ├─ txn.begin()
  ├─ txn.track(A.py) → backup A.py
  ├─ edit_file(A.py) → OK
  ├─ txn.track(B.py) → backup B.py
  ├─ edit_file(B.py) → Error: old_text not found
  │
  ├─ all_succeeded = False
  ├─ txn.rollback()
  │    → shutil.copy2(backup_A, A.py)  ← A.py 恢复
  │    → shutil.copy2(backup_B, B.py)  ← B.py 恢复
  │
  └─ 向 messages 注入 <transaction-rollback> 报告
     → LLM 看到报告后自动重试
```

---

## 6. 面试问答精选（Q&A）

### Q1: 你做了什么项目？

> NZ-Coder 是一个类 Claude Code 的终端 AI 编程助手，我从零实现了 Agent Loop、工具注册与调度、流式输出、权限沙箱、文件事务回滚、上下文压缩、Trace Logging、Session Resume、Agent-authored Diff/Revert、Benchmark Evaluation 和 Fake LLM Loop Tests。它不仅能自主读写代码、执行命令和搜索代码，还能记录运行链路、保存恢复会话、审查并回滚 Agent 自己的改动。我还实现了 13 个自动化评测任务来量化 Agent 的编程能力。

### Q2: Agent Loop 和普通的 API 调用有什么区别？

> Agent Loop 实现了一个多轮闭环。普通 API 调用是 "问一次答一次"，而 Agent Loop 是 "问→答→调工具→把工具结果喂回模型→再答→再调工具→..." 直到模型不再需要调用工具为止。这使得 Agent 能自主分解任务、收集信息、执行操作，而不需要人工逐步引导。

### Q3: 流式输出的技术难点在哪？

> 最大的难点不是文字的流式输出，而是 **tool call 的流式拼接**。OpenAI 的 streaming 接口会把一个 tool call 分成多个 delta chunk 发送——id、name、arguments 可能分散在不同 chunk 中。我用一个 `tool_calls_map[index]` 字典来按 index 累积各个 delta，最后拼接成完整的 tool call。这比文字流式要复杂得多。

### Q4: 为什么需要事务（Transaction）？

> 当 Agent 需要同时修改多个文件时（比如重构），如果编辑第二个文件失败了，第一个文件已经被改了，代码库处于不一致状态。Transaction 在编辑前备份所有涉及的文件，如果任何编辑失败就全部回滚到编辑前的状态。这借鉴了数据库 ACID 中的 Atomicity（原子性）思想。

### Q5: 上下文窗口满了怎么办？

> 我实现了三级压缩：(1) 大于 30KB 的工具输出自动持久化到磁盘，只保留前 2000 字符预览；(2) 只保留最近 3 个工具结果的原文，旧的替换为占位符；(3) 当 token 估算超过阈值时，用 LLM 自动生成对话摘要替换整个历史。三级逐步生效，尽量延迟代价最高的摘要操作。

### Q6: 如何保证安全？

> 我实现了四级权限管道：(1) 硬拦截危险 bash 命令（sudo、rm -rf 等正则匹配）；(2) 模式策略（plan 模式禁止所有写操作）；(3) 安全白名单（读操作永远放行）；(4) 用户交互确认（default 模式下写操作需要用户 y/n）。另外所有文件路径都经过 `is_relative_to(WORKDIR)` 检查，防止路径穿越。

### Q7: Benchmark 是怎么设计的？

> 13 个任务覆盖文件创建、bugfix、边界条件修复、测试生成、测试修复、多文件调试、CLI 行为修改、JSON 结构化编辑、重构和文档更新。每个任务有 setup/verify/cleanup 三阶段。验证使用 **执行验证** 而非字符串匹配，例如运行 pytest、subprocess 或 import 后 assert。报告会输出 pass rate、平均 turns/tools/time、按难度和任务类型统计，以及失败原因分类。

### Q8: 和 LangChain 比，你的实现有什么优劣？

> **优势**：完全理解每一行代码，能针对 coding agent 场景做深度定制（比如 tool call 流式拼接、文件事务），依赖只有 3 个包。**劣势**：不支持多 agent 编排图、没有内置 RAG、缺少生产级可观测性。但对于终端 coding agent 这个特定场景，轻量级实现足够且更灵活。

### Q9: 如果让你改进，你会做什么？

> (1) **并行工具调用**：目前工具是串行执行的，但多个读操作可以并行；(2) **更完整的 AST/tree-sitter 编辑**：现在已有 Python 函数/方法级 structural edit，后续可以扩展到 import 管理、跨文件重命名和更多语言；(3) **Model Router**：简单任务用小模型，复杂任务用大模型，降低成本；(4) **Web 服务化**：用 FastAPI + WebSocket/SSE 支持多用户和流式输出；(5) **更强 git-aware editing**：写入前更严格识别用户 dirty files，并提供分文件确认。

### Q10: 你遇到过什么比较难解决的问题？

> 最典型的是 **循环导入问题**。`loop.py` 依赖 `files.py`（通过 import），`files.py` 又需要 `TransactionManager`（在 `loop.py` 中创建）。如果直接 import 会形成循环。我用 **依赖注入** 解决：`files.py` 暴露一个 `set_txn_manager()` 函数，`loop.py` 在初始化时调用它注入实例。这避免了模块级循环导入，同时保持了类型安全。

---

## 7. 与工业级系统的对比

| 特性 | NZ-Coder | Claude Code | Cursor Agent |
|------|----------|-------------|--------------|
| Agent Loop | ✅ while True | ✅ 类似 | ✅ 类似 |
| Tool Dispatch | ✅ register+dispatch | ✅ 内置 | ✅ 内置 |
| Streaming | ✅ Rich Live | ✅ Terminal | ✅ Editor inline |
| Transaction | ✅ backup/rollback | ✅ file versioning | ✅ undo stack |
| Context Mgmt | ✅ 三级压缩 | ✅ 自动摘要 | ✅ 窗口管理 |
| Permission | ✅ 四级管道 | ✅ 类似 | ✅ UI 确认 |
| Subagent | ✅ 探索型子代理 | ✅ Task tool | ❌ |
| Memory | ✅ 文件持久化 | ✅ MEMORY.md | ❌ |
| Benchmark | ✅ 13 任务 + 报告 | ✅ SWE-bench | ❌ |
| Trace Logging | ✅ JSONL trace | ✅ 内部 tracing | ✅ 内部 tracing |
| Session Resume | ✅ save/resume/autosave | ✅ resume | ✅ chat history |
| Agent Diff/Revert | ✅ change set + safe revert | ✅ file versioning | ✅ undo stack |
| Fake LLM Tests | ✅ deterministic loop tests | 内部测试 | 内部测试 |
| 并行工具 | ❌ 串行 | ✅ 并行 | ✅ 并行 |
| IDE 集成 | ❌ 终端 | ❌ 终端 | ✅ 原生 |
| diff 预览 | ✅ unified diff | ✅ | ✅ |

---

## 8. 扩展方向

### 近期可做

1. **并行工具执行**：`asyncio.gather` 同时执行多个 read_file
2. **统一 diff patch parser**：从 exact replacement 扩展到真正的 unified diff parser
3. **CI + benchmark gate**：GitHub Actions 跑 pytest 和 benchmark smoke

### 中长期

4. **Model Router**：小任务→qwen-turbo，大任务→gpt-4o，节省成本
5. **更完整的 AST-aware Edit**：把当前 Python symbol-level edit 扩展到 import 管理、跨文件重命名和 tree-sitter 多语言支持
6. **MCP Protocol**：支持 Model Context Protocol 动态工具注册
7. **Web UI**：用 Gradio/Streamlit 提供浏览器界面
8. **Web 后端协程化**：FastAPI + WebSocket/SSE + 后台 AgentRun + ToolExecutor 线程池

---

## 附录：关键代码速查

| 概念 | 文件 | 行号/函数 |
|------|------|-----------|
| Agent Loop 主循环 | loop.py | `AgentLoop.run()` |
| 流式 Tool Call 拼接 | loop.py | `_call_streaming()` |
| 工具注册 | tools/\_\_init\_\_.py | `register()` |
| 工具分发 | tools/\_\_init\_\_.py | `dispatch()` |
| 事务开始 | transaction.py | `begin()` |
| 文件追踪 | transaction.py | `track()` |
| 事务回滚 | transaction.py | `rollback()` |
| 权限检查 | permissions.py | `check()` |
| 三级压缩 | context.py | `persist_large_output` / `micro_compact` / `auto_compact` |
| 流式渲染 | cli.py | `StreamingRenderer` |
| 路径安全 | files.py | `_safe_path()` |
| Benchmark 运行 | benchmark.py | `run_task()` / `run_all()` |
| Benchmark 报告 | benchmark.py | `render_markdown_report()` |
| Trace 记录 | trace.py | `TraceRecorder.log()` |
| Change Tracking | changes.py | `ChangeTracker.record_before/after()` |
| 安全回滚 | changes.py | `revert_change_file()` |
| Session 保存 | sessions.py | `save_session()` / `load_session()` |
| Workspace 状态 | workspace.py | `status_report()` |
| Shell 安全分类 | command_policy.py | `classify_bash()` |
| 子代理 | subagent.py | `run_subagent()` |
| 指数退避 | recovery.py | `backoff_wait()` |
