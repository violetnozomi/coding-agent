# 距离“通用 agent”主要还差什么？

## 1. 最大差距：执行环境还不够“生产级安全”

你现在有 shell command safety classifier，会识别 `sudo`、`shutdown`、`mkfs`、`rm -rf /`、写设备、包管理写操作、git 写操作等危险或 mutating 命令，并区分 read-only command / mutating command。

这是必要的，但还不够。

因为通用 agent 不是只防正则能看到的命令。真正危险的是：

```
python -c 执行危险代码
node -e 执行危险代码
curl | sh
bash -c 动态拼接
make 脚本里执行写操作
npm script 里跑 postinstall
测试命令偷偷访问网络
读取 .env / ssh key / token
把私有代码发到外部服务
```

你现在偏向 **命令字符串分类**，但通用 agent 需要 **执行环境隔离**。

还需要补：

```
Docker / Firecracker / sandbox runner
网络访问策略
secret masking
文件系统 mount 白名单
CPU / 内存 / 时间限制
进程树 kill
命令审计日志
工具级 permission scope
危险命令人类确认
```

换句话说，现在是：

```
安全策略 = regex + permission mode
```

通用 agent 需要：

```
安全策略 = sandbox + policy engine + audit + approval + resource quota
```

------

## 2. 多语言代码智能还不够

你现在的 repo intelligence 很强，但明显 Python 优先。

例如你已有：

```
read_symbol
find_symbol_callers
smart_search
python_symbol_check
verify_changed_files
```

`RuntimeState` 也主要跟踪 `read_file / read_symbol / grep_search / smart_search / verify_changed_files / bash` 这些 coding 行为。

问题是，通用 coding agent 要覆盖：

```
Python
TypeScript / JavaScript
Go
Rust
Java
C#
C++
Ruby
PHP
Swift
Kotlin
```

现在你的 AST symbol 能力主要是 Python。对 TS/Go/Rust，你大概只能搜索、读文件、跑 typecheck/check，缺少：

```
跨语言 read_symbol
跨语言 find_references
import / export graph
call graph
类型定义跳转
接口实现查找
测试到源码映射
```

建议下一步做：

```
tree-sitter 多语言 parser
LSP adapter
ripgrep + tree-sitter hybrid search
symbol index cache
dependency graph
test selector
```

优先级最高的是：

```
TypeScript/JavaScript
Go
Rust
```

因为这三类项目最常见，也最适合自动验证。

------

## 3. 验证系统还偏“轻量 sanity check”

你现在的 `verify_changed_files` 很实用：Python 用 `py_compile`，JS/TS 找 typecheck 或 local tsc，Go 用 `go test -run '^$'` 编译包，Rust 用 `cargo check`。前面那段 repo intelligence 工具就是这样设计的。

但它只能回答：

```
这次改动是否明显编译/语法坏了？
```

它不能回答：

```
这个 bug 是否真的修好了？
相关回归测试是否通过？
有没有破坏别的模块？
用户需求是否满足？
```

通用 coding agent 需要分层验证：

```
L0: changed-file compile / syntax check
L1: target unit test
L2: related test selection
L3: package/module test
L4: full CI
```

你现在有 L0，部分支持 L1/L2，但还缺一个 **验证策略选择器**。

建议加一个 `VerificationPlanner`：

```
输入：
  changed files
  failing tests
  traceback
  package metadata
  task mode
  available scripts

输出：
  最小验证命令
  次级验证命令
  full check 命令
  失败时判断是否环境噪音
```

例如：

```
Python:
  pytest tests/foo/test_bar.py::test_x
  python -m py_compile changed.py
  ruff check changed.py

TS:
  pnpm test path/to/test.spec.ts
  pnpm typecheck
  pnpm lint path

Go:
  go test ./pkg/foo -run TestX
  go test ./pkg/foo

Rust:
  cargo test test_name
  cargo check -p crate_name
```

你的 `VerificationManager` 已经有 gate 机制；下一步应该让它不只是“要求验证”，而是能“推荐最佳验证”。

------

## 4. Planner 还比较浅

你已经有 planning / replanning。复杂任务会先生成最多 5 步的 plan；如果长时间没编辑、验证多次失败、复杂度升级，就触发 replan。

这很好，但还不是通用 agent 级别的 planning。

现在更像：

```
一次性短计划 + 失败后重写计划
```

通用 agent 更需要：

```
任务图 DAG
子任务状态
依赖关系
验收标准绑定
风险分级
人类确认点
可暂停 / 可恢复 / 可审计
```

例如一个 feature 任务：

```
1. 修改 API schema
2. 更新 backend handler
3. 更新 frontend callsite
4. 加 migration
5. 更新 tests
6. 更新 docs
7. 跑 CI subset
```

这些步骤不是普通 list，而是有依赖、有验收、有文件范围、有失败恢复策略。

你现在的 RuntimeState 有 `acceptance_criteria`、`task_mode`、`plan_text`、`replan_count`，这是基础。
 下一步应该把 plan 从纯文本升级成结构化对象：

```
{
  "steps": [
    {
      "id": "locate",
      "status": "done",
      "target_files": ["..."],
      "evidence": "...",
      "verification": "..."
    }
  ]
}
```

然后每轮自动更新，而不是只存在 scratchpad 文本里。

------

## 5. Memory 还缺“项目级知识图谱”

你的 memory 已经能保存 user / project / feedback / reference，并支持 query recall。

但通用 coding agent 需要的不只是“记住几条 markdown”。它还要有项目知识：

```
这个 repo 用什么包管理器
测试怎么跑
lint 怎么跑
哪些目录是生成物
哪些模块不要碰
常见失败原因
CI 和本地差异
代码风格约束
架构边界
owner / review 习惯
```

你现在的 memory 更像：

```
经验笔记库
```

还不是：

```
项目画像 / repo profile
```

建议加一个 `ProjectProfile`：

```
{
  "languages": ["python", "typescript"],
  "package_managers": ["poetry", "pnpm"],
  "test_commands": {
    "python": "pytest",
    "typescript": "pnpm test"
  },
  "lint_commands": ["ruff check", "pnpm lint"],
  "source_roots": ["src", "packages/*/src"],
  "test_roots": ["tests", "__tests__"],
  "generated_dirs": ["dist", "build"],
  "known_env_noise": ["DISPLAY", "missing optional deps"]
}
```

这个可以通过首次扫描生成，后续由 memory 更新。

------

## 6. 工具体系还不够“动态”

你的工具现在通过模块 import 触发注册，`AgentLoop` import 了 bash、files、python_ast、search、todo、repo_intel、subagent、memory、skills、scratchpad。

这对本地 coding agent 很好，但通用 agent 需要动态工具生态：

```
MCP tools
OpenAPI tools
GitHub / GitLab / Jira / Linear
Browser
Package registry
Docs search
Database introspection
Cloud logs
CI logs
Sentry
Datadog
```

现在你的工具注册是静态的：

```
代码里 import 哪些工具，agent 就有哪些工具
```

通用 agent 需要：

```
按任务动态发现工具
按权限暴露工具
按项目加载工具
按 workspace 加载技能
按 risk 降级工具
```

你已经有 `skills` 和 path-based activation 的影子：写工具成功后会根据 edited path 激活相关 skill。
 下一步可以把它扩展成：

```
Tool Registry
Capability Discovery
Tool Permission Scope
Tool Versioning
Tool Usage Analytics
```

------

## 7. 子 agent 目前还是“辅助探索”，不是完整多 agent 协作

你有 `task` 子 agent，很好。它支持只读探索、review、test、general 写入模式，还会继承 parent runtime/scratchpad，上下文隔离。

但通用多 agent 系统还需要：

```
任务分解
角色分工
共享 blackboard
冲突检测
结果合并
并行执行
子 agent 之间的引用证据
父 agent 审核子 agent patch
```

现在子 agent 更像：

```
fresh context worker
```

还不是：

```
multi-agent orchestration system
```

尤其是 general 子 agent 可以写文件，虽然你有 transaction 和 verification rollback，但如果多个子 agent 并发写同一 repo，就需要：

```
file lock
branch per subagent
patch merge
conflict resolver
parent approval
```

建议短期内保持子 agent 默认 read-only，只让主 agent 写；等 patch merge 能力强了再放开 general 子 agent。

------

## 8. 上下文压缩不错，但还不是 DeepAgent 式 memory folding

你已经有 `micro_compact`、time-based compact、large output persistence、auto_compact，并且 auto_compact 会把 git diff stat 加进摘要，避免 compact 后忘记已编辑状态。

这很有价值。

但 DeepAgent 式 memory folding 更强调：

```
agent 自主决定什么时候 fold
fold 后形成结构化 episodic / working / tool memory
折叠结果直接成为下一阶段状态
```

你现在更像：

```
系统根据 token 阈值或用户命令 compact
```

建议你把 compact 输出结构化：

```
{
  "task_summary": "...",
  "current_hypothesis": "...",
  "files_inspected": [],
  "files_modified": [],
  "failed_attempts": [],
  "tool_lessons": [],
  "next_actions": []
}
```

然后喂回 RuntimeState / Scratchpad / Memory，而不是只作为一段自然语言 summary。

------

## 9. 缺少“测试选择器”和“影响面分析器”的系统化闭环

你已经有 `find_symbol_callers`、`smart_search`、`diff_status`、`verify_changed_files` 这类 repo intelligence 工具。

但通用 coding agent 真正强的地方是：

```
我改了 A.py
哪些 tests 最可能覆盖它？
哪些调用方风险最高？
哪些 public API 可能被破坏？
是否需要更新 docs / typing / migration？
```

你需要一个 `ImpactAnalyzer`：

```
输入：
  changed_files
  changed_symbols
  imports
  callers
  tests map
  package graph

输出：
  affected modules
  likely tests
  risk score
  suggested verification
  docs update needs
```

目前你的工具是分散的，agent 自己要把这些信号拼起来。通用 agent 应该系统自动拼。

------

## 10. 缺少评测闭环

这是你现在离“通用 agent”最大的非代码差距之一。

你有 trace、status、runtime summary，但还需要系统化评测：

```
任务成功率
平均 tool calls
平均 tokens
平均耗时
验证通过率
误改测试比例
无 diff 结束比例
max_turns 失败比例
重复搜索比例
回滚次数
环境噪音误判率
```

并且要有 benchmark harness：

```
SWE-bench Lite
自建 bugfix set
多语言小任务集
真实 repo regression set
工具调用 replay
A/B prompt 比较
不同模型比较
```

没有评测闭环，agent 越加功能越难知道是不是真的变强。

你已经有 `TraceRecorder` 和 runtime summary，这说明基础在；下一步应该做 dashboard / eval runner。

------

# 我建议的优先级路线

## 第一阶段：从“能跑”变成“稳定修 bug”

优先做这 5 个：

```
1. VerificationPlanner：自动推荐最小验证命令
2. ProjectProfile：扫描 repo，记住测试/lint/build 命令
3. Tree-sitter/LSP：至少支持 TS/Go/Rust symbol search
4. Patch risk analyzer：diff + callers + tests_modified + changed symbols
5. Eval harness：跑一批固定任务，记录成功率和成本
```

这一阶段做完，你会从：

```
single-repo coding agent
```

升级到：

```
稳定的自动 bugfix agent
```

------

## 第二阶段：从“修 bug”变成“通用 coding agent”

再做：

```
1. GitHub issue / PR / review comment 集成
2. CI log ingestion
3. test selection
4. branch / commit / patch lifecycle
5. 人类 approval workflow
6. IDE / CLI 双入口
```

这一阶段做完，才像真正能日常使用的 coding agent。

------

## 第三阶段：从“coding agent”变成“通用工程 agent”

最后做：

```
1. 动态工具发现：MCP / OpenAPI / internal tools
2. 多 agent 任务图
3. 长期项目记忆和 repo profile
4. 安全沙箱和权限审计
5. 多任务队列和后台运行
6. 组织级 policy
```

这才接近 Devin / Claude Code / Codex CLI 那类产品方向。

------

# 最关键的短板总结

我按影响排序：

| 优先级 | 缺口                        | 为什么重要                        |
| ------ | --------------------------- | --------------------------------- |
| P0     | 项目级验证策略              | 没有可靠验证，就不能自动交付      |
| P0     | 沙箱与权限系统              | 通用 agent 一定会执行危险命令     |
| P1     | 多语言 symbol / call graph  | 否则只能算 Python 强、其他语言弱  |
| P1     | ProjectProfile              | 每个 repo 的构建/测试方式不同     |
| P1     | Eval harness                | 不评测就不知道改动是否提升        |
| P2     | 结构化 planner              | 长任务需要任务图，不是纯文本 plan |
| P2     | ImpactAnalyzer              | 改代码必须知道影响面              |
| P2     | PR / CI / GitHub 集成       | 通用 coding agent 必须接工程流    |
| P3     | 多 agent 协同               | 复杂任务才需要，先别过早复杂化    |
| P3     | DeepAgent 式 memory folding | 有价值，但不是当前最大瓶颈        |

------

# 我的最终判断

你的代码距离“通用 agent”大概还差两层：

```
你现在：
  强 ReAct/CodeAct coding agent 骨架
  repo intelligence 初具规模
  有 memory / runtime state / verification / subagent / rollback

下一步要补：
  项目画像 + 验证策略 + 多语言代码智能 + 安全沙箱 + 评测闭环

再下一步才是：
  GitHub/CI/IDE 产品化 + 动态工具生态 + 多 agent 编排
```

最建议你不要先追 DeepAgent 式“统一推理流”。你现在的方向更像 SWE-agent / Claude Code，是对的。
 短期最有价值的不是再改 agent loop，而是做：

> **ProjectProfile + VerificationPlanner + ImpactAnalyzer + Eval Harness**

这四个做完，你的 agent 会从“能自主改代码”明显升级到“能更可靠地交付代码”。