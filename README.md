# NZ-Coder

一个类似 Claude Code 的终端 AI 编程助手，从零实现 agent harness 核心机制。

## 核心机制

| 模块 | 对标 | 说明 |
|------|------|------|
| Agent Loop | s01 | 最小可运行循环: user → model → tool_use → tool_result → continue |
| Tool Dispatch | s02 | bash / read_file / write_file / edit_file / grep / list_dir |
| Todo/Planning | s03 | 会话级任务清单，保持一个 in_progress |
| Subagent | s04 | 隔离上下文的子代理委派 |
| Skill Loading | s05 | 按需加载领域知识 |
| Context Compact | s06 | 大输出持久化 + 微压缩 + 自动摘要 |
| Permission | s07 | deny → mode → allow → ask 安全管道 |
| Memory | s09 | .memory/ 跨会话持久信息 |
| System Prompt | s10 | 提示词组装流水线 |
| Error Recovery | s11 | 错误恢复与续行 |
| Patch/Diff | s12 | 原子化精确替换，写入后返回 unified diff |
| Command Policy | s13 | shell 危险命令拦截，只读子代理命令白名单 |
| Benchmark | s14 | 多类型 coding-agent 任务评测，JSON/Markdown 报告 |
| Trace Logging | s15 | 每次 agent run 的 JSONL 调试链路 |
| Session Resume | s16 | 会话保存、自动保存和恢复 |
| Change Tracking | s17 | agent-authored diff 审查和安全回滚 |
| Python AST Tools | s18 | 符号/调用关系检查，函数和方法级结构化编辑 |

## 快速开始

```bash
cd nz-coder
pip install -e .
cp .env.example .env
# 编辑 .env 填入 API_KEY 和 MODEL_ID

nz-coder
```

## 支持的模型

任何 OpenAI 兼容的 function calling 接口均可使用：

- 通义千问 (qwen-plus / qwen-max)
- DeepSeek (deepseek-chat)
- OpenAI (gpt-4o)
- 其他 OpenAI 兼容接口

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/compact` | 手动压缩上下文 |
| `/todo` | 查看当前任务清单 |
| `/memory` | 查看已保存的记忆 |
| `/mode <default\|auto\|plan>` | 切换权限模式 |
| `/status` | 查看 workspace、git、trace 和 change set 状态 |
| `/trace` | 查看最近一次 agent trace 摘要 |
| `/diff` | 查看最近一次 agent 文件改动 diff |
| `/revert-last` | 回滚最近一次 agent 记录的文件改动 |
| `/save-session [id]` | 保存当前对话 |
| `/sessions` | 列出已保存对话 |
| `/resume [id]` | 恢复已保存对话，默认 latest |
| `/clear` | 清空对话历史 |
| `exit` / `q` / Ctrl+C | 退出 |

## Benchmark

```bash
python -m nz_coder.benchmark --list
python -m nz_coder.benchmark
python -m nz_coder.benchmark --report
```

Benchmark 覆盖文件创建、bugfix、测试修复、多文件修改、CLI、结构化 JSON 编辑、重构和文档更新等任务。运行后会生成：

- `.nz-coder/benchmark/report.json`
- `.nz-coder/benchmark/report.md`
- `.nz-coder/benchmark/runs/*.jsonl`

报告包含 pass rate、平均 turns/tools/time、按难度和任务类型分组统计，以及失败原因分类。

### Latest Benchmark Result

Latest run: `2026-05-01 23:37:18`, model `qwen-plus`.

- Pass rate: **92%** (12/13)
- Avg turns/tools/time: 6.38 / 5.38 / 23.1s
- Easy: 5/5, Medium: 5/5, Hard: 2/3
- Failure category: `incorrect_behavior` (1)

| Task | Type | Difficulty | Result | Turns | Tools | Time | Reason |
|---|---|---:|---:|---:|---:|---:|---|
| `fizzbuzz` | file_create | easy | PASS | 2 | 1 | 4.1s | All checks passed |
| `bugfix_sum` | bugfix | easy | PASS | 3 | 2 | 7.4s | Output contains 15 |
| `add_function` | feature_add | medium | PASS | 3 | 2 | 7.7s | All assertions passed |
| `write_tests` | test_authoring | medium | PASS | 7 | 6 | 16.0s | All 3 tests passed |
| `refactor_class` | refactor | hard | FAIL | 26 | 25 | 142.1s | Missing exported `validate_email` after refactor |
| `multi_file` | multi_file | hard | PASS | 7 | 6 | 23.6s | All assertions passed |
| `boundary_bugfix` | bugfix | easy | PASS | 3 | 2 | 7.5s | All assertions passed |
| `pytest_repair` | test_repair | medium | PASS | 9 | 8 | 20.8s | pytest passed |
| `multi_file_bugfix` | multi_file | medium | PASS | 6 | 5 | 16.0s | app output is correct |
| `cli_argparse` | cli | medium | PASS | 4 | 3 | 12.9s | CLI behavior correct |
| `json_config_update` | structured_edit | easy | PASS | 3 | 2 | 8.1s | settings updated |
| `public_api_preserve` | refactor | hard | PASS | 7 | 6 | 27.5s | All assertions passed |
| `documentation_update` | documentation | easy | PASS | 3 | 2 | 6.6s | docs updated |

Follow-up: the hard refactor failure has been addressed with `python_symbol_check`, `python_structural_edit`, stronger behavior verification, and a Fake LLM regression test. A fresh real benchmark run is still pending because the API provider returned an `Arrearage` account-status error during rerun.

## 安全与可靠性

- `bash` 执行前会进行命令分类：危险命令直接拒绝，默认模式下未知或写入型 shell 命令需要确认。
- `plan` 模式只允许明确的只读工具和只读 shell 命令。
- `task` 的 `explore` 子代理会以只读 shell 策略运行。
- `write_file`、`edit_file` 和 `apply_patch` 会返回 unified diff，便于审查实际改动。
- Python 函数/方法级重构可使用 `python_structural_edit` 按 AST symbol 定位，减少 `old_text not found` 式失败。
- 多文件 patch 会先验证全部 hunk，再写入；失败时配合事务系统回滚。
- Agent loop 增加 API 重试、单轮工具调用上限和最大循环轮数，避免静默退出或无限循环。
- 设置 `TRACE_ENABLED=1` 后，每次 agent run 会写入 `.nz-coder/runs/*.jsonl`，可用 `/trace` 查看摘要。
- 文件写入会记录 agent-authored change set，可用 `/diff` 审查，用 `/revert-last` 回滚。回滚前会确认当前内容仍等于 agent 写入后的状态，避免覆盖用户后续改动。
- `/status` 会展示 project profile、git dirty files、最近 trace 和最近 change set，帮助 agent 避免盲改用户已有改动。

## Session Resume

```bash
/save-session demo
/sessions
/resume demo
```

会话保存在 `.nz-coder/sessions/`。REPL 每轮完成后也会写入 `autosave` 和 `latest`，便于中断后恢复。

## Tests

```bash
python -m pytest -q
```

测试覆盖工具注册、权限、事务回滚、patch create/replace/delete/dry-run、Python AST 结构化编辑、memory CRUD、benchmark 报告聚合，以及不依赖真实模型的 Fake LLM agent loop。

## 学习资料

- [NZ-Coder_Agent_Runtime_学习文档.md](NZ-Coder_Agent_Runtime_学习文档.md)：面向 Agent 零基础，解释今天新增的 benchmark、trace、session、diff/revert、workspace 等 runtime 能力。
- [NZ-Coder_面试学习文档.md](NZ-Coder_面试学习文档.md)：面试复习版，按模块梳理架构、设计决策和高频问答。
- [docs/architecture.md](docs/architecture.md)：英文架构速览。
- [docs/evaluation.md](docs/evaluation.md)：英文评测设计速览。

## 项目结构

```
nz-coder/
├── .env.example
├── requirements.txt
├── README.md
├── nz_coder/
│   ├── __init__.py
│   ├── __main__.py          # 入口
│   ├── changes.py           # agent 文件改动记录与回滚
│   ├── command_policy.py    # shell 命令安全分类
│   ├── cli.py               # Rich CLI REPL
│   ├── config.py            # 配置管理
│   ├── loop.py              # Agent Loop 核心
│   ├── context.py           # 上下文压缩
│   ├── permissions.py       # 权限系统
│   ├── subagent.py          # 子代理
│   ├── memory.py            # 记忆系统
│   ├── skills.py            # 技能加载
│   ├── prompt.py            # 系统提示词构建
│   ├── recovery.py          # 错误恢复
│   ├── sessions.py          # 会话保存与恢复
│   ├── trace.py             # JSONL 运行链路
│   ├── workspace.py         # workspace/git 状态
│   └── tools/
│       ├── __init__.py
│       ├── registry.py      # 工具注册与分发
│       ├── bash.py          # Shell 命令
│       ├── files.py         # 文件读写编辑
│       ├── search.py        # 搜索 (grep/glob)
│       └── todo.py          # 任务清单
├── skills/
│   └── code-review/
│       └── SKILL.md
└── tests/
    └── test_smoke.py
```
