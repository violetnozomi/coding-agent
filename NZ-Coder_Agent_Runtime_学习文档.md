# NZ-Coder Agent Runtime 学习文档

> 面向“对 Agent 一窍不通”的学习版。目标不是背代码，而是理解：一个 coding agent 为什么需要 loop、tool、权限、trace、session、diff、benchmark，以及这些模块在 NZ-Coder 里怎么配合。

---

## 0. 先建立直觉：Agent 到底是什么？

普通聊天机器人是：

```text
用户问一句 -> 模型答一句
```

Coding Agent 是：

```text
用户给目标
  -> 模型判断下一步要做什么
  -> 调工具读文件/搜代码/写文件/跑测试
  -> 把工具结果再喂给模型
  -> 模型继续决定下一步
  -> 直到任务完成
```

所以 Agent 的核心不是“模型很聪明”，而是一个后端 runtime：

```text
LLM + Tool System + Permission + State + Recovery + Observability
```

NZ-Coder 今天新增的东西，基本都围绕这句话展开：

- `benchmark.py`：证明 Agent 能力，不靠嘴说。
- `trace.py`：记录 Agent 每一步做了什么，方便 debug。
- `tests/test_loop_fake.py`：不用真实模型，也能测试 Agent Loop。
- `changes.py`：记录 Agent 自己改了哪些文件，可 diff、可回滚。
- `sessions.py`：保存和恢复对话。
- `workspace.py`：让 Agent 了解当前项目和 git 状态。
- CLI 命令：`/status`、`/diff`、`/revert-last`、`/save-session`、`/resume`。

---

## 1. 整体架构：一次对话在后端怎么跑？

核心入口：

- CLI：`nz_coder/cli.py`
- Agent 主循环：`nz_coder/loop.py`
- 工具注册：`nz_coder/tools/__init__.py`

完整流程：

```text
用户输入自然语言
  -> cli.py 把输入追加到 history
  -> AgentLoop.run(history)
  -> 调 LLM
  -> LLM 返回文本或 tool_calls
  -> 如果有 tool_calls：
       PermissionManager 检查权限
       dispatch 找到本地工具函数
       工具执行，返回 tool result
       tool result 追加到 history
       继续调 LLM
  -> 如果没有 tool_calls：
       输出最终回答
       本轮结束
```

这里最关键的数据结构是 `history`：

```python
[
    {"role": "user", "content": "帮我修复 bug"},
    {"role": "assistant", "content": "", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "call_xxx", "content": "文件内容..."},
    {"role": "assistant", "content": "已修复，并通过测试。"},
]
```

你可以把 `history` 理解成 Agent 的短期记忆。模型每一轮都看这个列表，然后决定下一步。

---

## 2. 今天新增能力一：Benchmark 扩展和结果报告

文件：

- `nz_coder/benchmark.py`
- `docs/evaluation.md`

### 2.1 为什么 Agent 需要 benchmark？

如果你面试时只说：

> 我的 Agent 可以写代码。

面试官很难相信。更好的说法是：

> 我设计了 13 个自动化任务，覆盖 bugfix、测试修复、多文件编辑、CLI 修改、JSON 编辑、重构和文档更新，并用可执行验证统计 pass rate、工具调用数和失败原因。

这就是 benchmark 的价值：**把 Agent 能力变成可量化结果**。

### 2.2 当前 benchmark 覆盖什么？

当前有 13 个任务，类型包括：

| 类型 | 例子 | 验证方式 |
|------|------|----------|
| 文件创建 | 创建 FizzBuzz 文件 | 运行 Python，检查输出 |
| Bugfix | 修复 sum_list / 边界条件 | import 或 subprocess 验证 |
| 功能添加 | 添加 factorial 函数 | assert 行为 |
| 测试生成 | 为字符串工具写 pytest | 运行 pytest |
| 测试修复 | 根据失败测试修代码 | 跑 pytest |
| 多文件修改 | app.py + helpers.py | 运行入口脚本 |
| CLI 行为 | 增加 `--name` 参数 | subprocess 检查输出 |
| JSON 编辑 | 修改 settings.json | json.loads + 字段检查 |
| 重构 | 保留 public API | import + assert |
| 文档更新 | README_TASK.md | 文本检查 |

### 2.3 Benchmark 的代码结构

每个任务都是一个类：

```python
class BenchTask:
    task_id = ""
    description = ""
    difficulty = "easy"
    task_type = "general"

    def setup(self):
        pass

    def verify(self):
        return {"passed": False, "reason": "Not implemented"}

    def cleanup(self):
        pass
```

三阶段：

```text
setup()   -> 准备测试文件
agent.run -> 让 Agent 解决问题
verify()  -> 用程序验证结果是否正确
cleanup() -> 清理文件
```

### 2.4 结果报告

运行：

```bash
python -m nz_coder.benchmark
python -m nz_coder.benchmark --report
```

会生成：

```text
.nz-coder/benchmark/report.json
.nz-coder/benchmark/report.md
.nz-coder/benchmark/runs/*.jsonl
```

报告包含：

- 总 pass rate
- 平均 turns
- 平均 tool calls
- 平均耗时
- 按难度统计
- 按任务类型统计
- 失败原因分类
- 每个任务的 trace 路径

面试时可以这样说：

> 我没有只做 demo，而是实现了 benchmark harness。每个任务都有 setup/verify/cleanup，并且 verify 尽量使用可执行验证，比如运行 pytest 或 import 后 assert，避免只看字符串。

---

## 3. 今天新增能力二：Trace Logging

文件：

- `nz_coder/trace.py`
- `nz_coder/loop.py`
- CLI 命令：`/trace`

### 3.1 Trace 是什么？

Trace 就是 Agent 的运行日志，但它不是普通 print，而是结构化 JSONL。

每一行是一个事件：

```json
{"event": "run_start", "run_id": "...", "message_count": 1}
{"event": "llm_request", "token_estimate": 2034}
{"event": "llm_response", "tool_calls": 1}
{"event": "tool_call", "name": "read_file", "status": "ok"}
{"event": "run_end", "status": "completed"}
```

### 3.2 为什么 Agent 需要 trace？

Agent 的失败经常不是一句报错能解释的，比如：

- 模型没有调用工具。
- 工具参数 JSON 坏了。
- shell 命令失败了。
- 文件改了，但测试没跑。
- 上下文压缩后丢了关键信息。

Trace 可以回答：

```text
这一轮模型看到了多少消息？
模型返回了几个 tool call？
调用了哪个工具？
工具返回多长输出？
是否发生 API retry？
是否触发 transaction rollback？
```

### 3.3 TraceRecorder 的核心设计

```python
class TraceRecorder:
    def __init__(self, run_id=None, trace_dir=None, enabled=True):
        self.run_id = run_id or timestamp + uuid
        self.path = trace_dir / f"{run_id}.jsonl"

    def log(self, event, **payload):
        row = {
            "ts": time.time(),
            "run_id": self.run_id,
            "event": event,
            **payload,
        }
        append_jsonl(row)
```

关键点：

- JSONL 适合追加写入。
- 每个 run 一个 `run_id`。
- 大字段会截断，避免 trace 文件爆炸。
- CLI 可以用 `/trace` 展示最近一次摘要。

### 3.4 loop.py 里记录了哪些事件？

```text
run_start
llm_request
llm_response
tool_call
transaction_rollback
compact
api_error
run_end
```

这就是可观测性。面试时这点很加分，因为它体现你不是只写功能，还考虑 debug 和线上排障。

---

## 4. 今天新增能力三：Fake LLM Loop Tests

文件：

- `tests/test_loop_fake.py`

### 4.1 为什么要 Fake LLM？

真实 LLM 有几个问题：

- 调用要钱。
- 输出不稳定。
- 网络可能失败。
- 测试速度慢。

但是 Agent Loop 又必须测试。解决方案是：写一个假的 OpenAI-compatible client。

### 4.2 FakeClient 模拟什么？

真实 OpenAI SDK 调用长这样：

```python
client.chat.completions.create(...)
```

测试里 FakeClient 也提供同样接口：

```python
class FakeClient:
    def __init__(self, items):
        self.chat = FakeChat(FakeCompletions(items))
```

然后可以预设模型返回：

```python
fake = FakeClient([
    FakeResponse(FakeMessage(tool_calls=[
        FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"})
    ])),
    FakeResponse(FakeMessage("done")),
])
```

这表示：

```text
第一次模型返回：我要调用 write_file
第二次模型返回：任务完成
```

### 4.3 现在测了哪些核心行为？

```text
1. LLM 返回 tool_call -> Agent 执行工具 -> 再调 LLM -> final answer
2. LLM 返回坏 JSON -> Agent 把错误作为 tool result 反馈
3. API 前两次失败 -> Recovery retry -> 第三次成功
4. Trace 文件包含 run_start / llm_response / run_end
```

面试话术：

> 我把 AgentLoop 和真实模型解耦，注入 FakeClient 来做确定性测试。这样可以验证 tool loop、错误恢复、trace 等 runtime 行为，而不依赖外部 API。

---

## 5. 今天新增能力四：Change Tracking、Diff 和 Revert

文件：

- `nz_coder/changes.py`
- `nz_coder/tools/files.py`
- CLI 命令：`/diff`、`/revert-last`

### 5.1 为什么需要 Change Tracking？

Coding Agent 最危险的地方是：它会改你的代码。

如果没有追踪，你不知道：

- 它改了哪些文件。
- 每个文件改了什么。
- 能不能只撤销 Agent 的改动。
- 用户后续又改了文件时，是否还能安全回滚。

所以我们增加了 `ChangeTracker`。

### 5.2 ChangeTracker 记录什么？

每个被 Agent 修改的文件，会记录：

```json
{
  "path": "app.py",
  "before_exists": true,
  "before": "原始内容",
  "after_exists": true,
  "after": "修改后内容"
}
```

保存位置：

```text
.nz-coder/changes/{run_id}.json
```

### 5.3 它怎么接入文件工具？

在 `AgentLoop.__init__()` 中创建：

```python
self.change_tracker = ChangeTracker(run_id=self.tracer.run_id)
set_change_tracker(self.change_tracker)
```

然后文件工具在写入前后记录：

```python
_track_before(path, fp, before, existed)
fp.write_text(content)
_track_after(path, content, True)
```

### 5.4 `/diff` 做什么？

`/diff` 读取 change set，用 `difflib.unified_diff` 生成统一 diff：

```diff
--- a/app.py
+++ b/app.py
@@
- total = 1
+ total = 0
```

这让用户能审查 Agent 的真实改动。

### 5.5 `/revert-last` 为什么是安全回滚？

回滚前会检查当前文件是否仍然等于 Agent 写入后的内容：

```text
当前文件内容 == tracked after
  -> 可以回滚到 before
当前文件内容 != tracked after
  -> 拒绝回滚，避免覆盖用户后续改动
```

这个设计很重要，因为用户可能在 Agent 修改后又手动改了文件。如果直接回滚，会覆盖用户的新修改。

面试话术：

> 我实现了 agent-authored change set，记录每个文件的 before/after。回滚时不是盲目写回 before，而是先确认当前内容仍等于 tracked after-state，避免覆盖用户后续手动修改。

---

## 6. 今天新增能力五：Session Save / Resume

文件：

- `nz_coder/sessions.py`
- CLI 命令：`/save-session`、`/sessions`、`/resume`

### 6.1 为什么需要 Session？

Agent 对话可能很长。用户可能：

- 中途关闭终端。
- 想明天继续。
- 想保存某次任务上下文。
- 想恢复最近一次 autosave。

所以需要把 `history` 持久化。

### 6.2 Session 保存了什么？

```json
{
  "session_id": "demo",
  "timestamp": "2026-05-01 12:00:00",
  "workspace": "C:/.../nz-coder",
  "model": "qwen-plus",
  "mode": "default",
  "messages": [...]
}
```

保存位置：

```text
.nz-coder/sessions/{session_id}.json
.nz-coder/sessions/latest.json
```

### 6.3 CLI 怎么用？

```text
/save-session demo
/sessions
/resume demo
```

REPL 每轮完成后也会自动保存：

```python
save_session(history, mode=agent.permissions.mode, session_id="autosave")
```

`save_session()` 还会同步写 `latest.json`，所以 `/resume` 默认可以恢复最近会话。

### 6.4 为什么恢复时检查 workspace？

如果 session 是在 A 项目保存的，却在 B 项目恢复，就很危险。模型会以为自己在旧项目里，可能乱读乱改。

所以 CLI 恢复时检查：

```python
if payload["workspace"] != str(config.WORKDIR):
    refuse_resume()
```

---

## 7. 今天新增能力六：Workspace / Git Awareness

文件：

- `nz_coder/workspace.py`
- CLI 命令：`/status`
- 文件工具中的 git dirty warning

### 7.1 为什么 Agent 要知道 workspace 状态？

真实 coding agent 不能只会改文件，还要知道：

- 当前项目是什么类型？
- 是不是 git repo？
- 哪些文件已经 dirty？
- 最近一次 trace 在哪里？
- 最近一次 change set 在哪里？

否则它可能覆盖用户已有改动。

### 7.2 `/status` 输出什么？

`status_report()` 会展示：

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

### 7.3 Project Profile 怎么判断？

```python
if pyproject.toml exists -> Python project
if requirements.txt exists -> Python dependencies
if package.json exists -> Node project
if Cargo.toml exists -> Rust project
if go.mod exists -> Go module
```

这很简单，但足够作为第一版项目感知。

### 7.4 Git dirty warning

文件工具写入前会调用：

```python
git status --short -- path
```

如果目标文件已有 git 改动，工具输出会带 warning：

```text
Warning: app.py already has git changes: M app.py
```

这不是强制阻止，但能提醒 Agent 和用户：这个文件已经不干净。

---

## 8. 今天新增能力七：Patch 和 Shell 加固

文件：

- `nz_coder/tools/files.py`
- `nz_coder/tools/bash.py`
- `nz_coder/command_policy.py`

### 8.1 apply_patch 现在支持什么？

原来只支持 exact replacement。现在支持：

```text
replace: 精确替换
create: 创建文件
delete: 删除文件
dry_run: 只预览 diff，不落盘
```

示例：

```json
{
  "changes": [
    {
      "op": "replace",
      "path": "app.py",
      "old_text": "total = 1",
      "new_text": "total = 0"
    },
    {
      "op": "create",
      "path": "README_TASK.md",
      "content": "# Task"
    }
  ],
  "dry_run": false
}
```

### 8.2 为什么先完整预检再写？

多文件 patch 最大的问题是：

```text
第 1 个文件改成功
第 2 个文件 old_text 找不到
```

如果不预检，代码库就处于半修改状态。

现在逻辑是：

```text
先检查所有 change 是否合法
所有 old_text 是否唯一匹配
所有 create/delete 是否可执行
全部通过后才写文件
```

再配合 transaction，可以进一步减少半失败风险。

### 8.3 bash 加固

`bash` 现在会：

- 通过 `classify_bash()` 判断危险命令。
- read-only 子代理阻止 mutating shell。
- 支持 timeout 参数。
- 返回非 0 exit code。

面试时可以强调：

> 我没有只靠 prompt 告诉模型“不要执行危险命令”，而是在执行层加了 command policy。模型即使请求危险命令，也会被代码拦截。

---

## 9. Hard Refactor 失败案例：怎么一步步工程化解决？

这次 `refactor_class` benchmark 很适合学习 Agent 工程，因为它不是简单语法 bug，而是典型的 coding agent 难点：

```text
任务要求：
1. 从 UserManager.create_user 里提取 email 校验逻辑
2. 新增模块级函数 validate_email(email)
3. create_user 必须调用 validate_email
4. 原始行为不能变
```

### 9.1 第一次失败：只看最终错误

benchmark 报错：

```text
ImportError: cannot import name 'validate_email' from 'user_manager'
```

这说明最终文件里没有导出模块级 `validate_email`。但这只是表象，不能只修 verify。工程上第一步应该看 trace/tool log：

```text
read_file -> apply_patch old_text not found
read_file -> apply_patch old_text not found
edit_file -> old_text not found
write_file -> 覆盖文件
最终 import validate_email 失败
```

结论：模型不是完全不会重构，而是被“字符串精确编辑”卡住了。

### 9.2 根因一：exact text patch 对 refactor 太脆

`edit_file` / `apply_patch` 要求：

```text
old_text 必须在文件中唯一匹配
```

这个设计很安全，但重构类任务容易失败，因为模型经常写出“差一点一样”的 old_text，比如空格、引号、上下文行不完全一致。于是工具返回：

```text
Error: old_text not found
```

我们先做了一个小修复：当 old_text 找不到时，工具返回 nearby context。这样模型至少能看到附近真实代码，而不是盲猜。

### 9.3 根因二：只检查符号不等于行为正确

后来模型能生成 `validate_email`，但出现过这类错误：

```python
return email and "@" in email and "." in email.split("@")[-1]
```

它对有效邮箱返回 `True`，但对空字符串返回 `""`，不是严格的 `False`。所以 hard refactor 的验证不能只看：

```text
有没有 validate_email？
create_user 有没有调用它？
```

还必须跑行为测试：

```bash
python -c "from user_manager import UserManager, validate_email; assert validate_email('') is False; ..."
```

这就是为什么 benchmark 描述里加入了“必须运行 exact behavior check”。

### 9.4 新增 AST 符号检查工具

文件：`nz_coder/tools/python_ast.py`

新增：

```text
python_symbol_check
```

它用 Python `ast` 解析源码，检查：

```text
模块级函数：validate_email
类：UserManager
类方法：UserManager.create_user
调用关系：UserManager.create_user -> validate_email
```

这比字符串搜索更可靠，因为它理解 Python 结构。

### 9.5 关键修复：新增结构化编辑工具

真正解决 repeated `old_text not found` 的，是：

```text
python_structural_edit
```

它不是靠 old_text 猜位置，而是：

```text
1. ast.parse(source)
2. 找到目标 symbol 的 lineno / end_lineno
3. 按行号替换整个函数或方法
4. 可以在某个模块级 symbol 前插入新函数
5. 写回文件并返回 diff
```

对 `refactor_class`，理想工具调用是：

```json
{
  "path": "user_manager.py",
  "insertions": [{
    "before_symbol": "UserManager",
    "code": "def validate_email(email):\n    return bool(email and \"@\" in email and \".\" in email.split(\"@\")[-1])\n"
  }],
  "replacements": [{
    "target": "UserManager.create_user",
    "code": "def create_user(self, name, email):\n    if not validate_email(email):\n        raise ValueError(f\"Invalid email: {email}\")\n    ..."
  }]
}
```

这里的工程思想是：**简单编辑用字符串 patch，结构化 Python 重构用 AST 定位**。

### 9.6 接入时必须改哪些地方？

新增一个工具函数还不够，Agent runtime 里至少要接四处：

```text
1. tools/python_ast.py 注册 tool spec 和 handler
2. loop.py import 该模块，触发工具注册
3. permissions.py 把 python_structural_edit 加入 WRITE_TOOLS
4. loop.py 的 has_write 判断加入 python_structural_edit，确保事务覆盖
5. prompt.py 告诉模型：Python 函数/方法级重构优先用 python_structural_edit
6. benchmark.py 在 refactor_class 描述中明确推荐该工具，并要求行为验证
```

如果只做第 1 步，模型可能根本不知道怎么用；如果忘了第 3/4 步，权限和事务语义就不完整。

### 9.7 如何验证修复？

这次验证分两层。

第一层是普通测试：

```bash
python -m pytest -q
```

结果：

```text
23 passed
```

第二层是 fake LLM loop 测试。真实 API 会受网络、余额、模型随机性影响，所以我们新增了一个离线测试：

```text
Fake LLM -> tool_call(python_structural_edit)
         -> tool_call(python_symbol_check)
         -> final response
```

测试会真正生成 `user_manager.py`，执行结构化编辑，然后用 `exec` 检查：

```text
validate_email("a@b.c") is True
validate_email("") is False
validate_email("noat") is False
UserManager().create_user(...) 行为正确
```

这保证了 runtime 链路本身是可靠的。

### 9.8 为什么这次真实 benchmark 没重新得到 PASS？

我们重新跑了：

```bash
python -m nz_coder.benchmark --task refactor_class
```

但 API 返回：

```text
Arrearage: Access denied, please make sure your account is in good standing.
```

也就是账号欠费/不可用，Agent 没有进入模型推理阶段。所以这次不能声称“真实 benchmark 已通过”，只能说：

```text
代码层修复已完成；
离线 fake loop 回归测试已覆盖；
真实 benchmark 需要 API 恢复后再跑。
```

这也是工程表达上很重要的一点：不要把外部服务失败伪装成模型能力通过。

---

## 10. 这些模块之间怎么配合？

一次“修 bug”的完整后端链路：

```text
1. 用户输入：修复 bug
2. cli.py 追加 user message 到 history
3. AgentLoop 记录 run_start trace
4. LLM 返回 read_file tool call
5. PermissionManager 放行读操作
6. read_file 返回文件内容
7. TraceRecorder 记录 tool_call
8. LLM 返回 apply_patch
9. PermissionManager 检查写权限
10. TransactionManager begin
11. ChangeTracker record_before
12. apply_patch 预检并写入
13. ChangeTracker record_after
14. Transaction commit
15. LLM 返回最终回答
16. TraceRecorder 记录 run_end
17. sessions.py 自动保存 autosave/latest
18. 用户可用 /diff 查看改动，用 /revert-last 回滚
```

这就是一个比较完整的 coding-agent runtime。

---

## 11. 面试回答模板

### Q1：你的 Agent 后端架构怎么设计？

可以答：

> NZ-Coder 是同步 terminal agent runtime。核心是 AgentLoop，维护 conversation history，每轮调用 OpenAI-compatible function calling API。如果模型返回 tool_calls，就经过权限检查后 dispatch 到本地工具，把 tool result 追加回 history，再继续下一轮。围绕这个 loop，我做了 transaction、trace、session、change tracking、benchmark 等工程化模块。

### Q2：为什么不用协程？

可以答：

> 目前是单用户 terminal 场景，主要阻塞点是模型调用和本地工具执行，同步架构更容易保证状态一致性、事务回滚和 trace 顺序。未来如果服务化成 WebSocket 多用户后端，可以把 AgentRun 放到后台任务，模型 streaming 用 async，阻塞工具放线程池。

### Q3：你怎么证明 Agent 不是 demo？

可以答：

> 我做了 benchmark harness，目前 13 个任务，覆盖 bugfix、测试修复、多文件修改、CLI、JSON 编辑、重构和文档更新。每个任务都有 setup/verify/cleanup，并尽量用可执行验证。同时我用 Fake LLM 测 AgentLoop，不依赖真实 API，也能测试 tool loop、坏 JSON、API retry 和 trace。

### Q4：Agent 改坏代码怎么办？

可以答：

> 有两层保护。第一层是 transaction：同一轮多文件写入只要有工具失败就回滚。第二层是 change tracking：记录 agent-authored before/after，用户可以 `/diff` 审查，也可以 `/revert-last` 回滚。回滚前会检查当前内容仍然等于 tracked after-state，避免覆盖用户后续改动。

### Q5：你做了哪些安全设计？

可以答：

> 文件路径用 `resolve + is_relative_to(WORKDIR)` 防路径穿越。shell 命令先由 `command_policy` 分类，危险命令直接拒绝，plan 模式禁止写操作和 unknown/mutating shell。写文件返回 unified diff，dirty git 文件会 warning。记忆内容也被标记为 untrusted context，避免把 memory 当高优先级指令。

---

## 12. 学习路线建议

如果你刚开始学 Agent，建议按这个顺序看代码：

1. `tools/__init__.py`：先理解工具注册和 dispatch。
2. `tools/files.py`：理解工具函数如何读写文件。
3. `loop.py`：理解 user -> model -> tool -> model 的主循环。
4. `permissions.py` + `command_policy.py`：理解为什么不能让模型直接执行一切。
5. `transaction.py` + `changes.py`：理解写代码时怎么保证可回滚。
6. `trace.py`：理解怎么 debug Agent。
7. `sessions.py`：理解对话状态怎么持久化。
8. `benchmark.py`：理解怎么评估 Agent 能力。
9. `tests/test_loop_fake.py`：理解怎么在不调用模型的情况下测试 Agent。

看完这些，你对 coding agent runtime 的理解会比“只会调用 ChatGPT API”高很多。
