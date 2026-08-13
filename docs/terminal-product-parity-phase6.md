# 第六阶段：Terminal Product 源码审计与对齐报告

日期：2026-08-11

## 审计边界

本报告重新读取真实源码，不沿用旧矩阵，也不把 IDE、Web 或 cloud-only
功能算作缺失。源码样本包括 InfCodeX 的 `src/kodax_cli.ts`、
`src/cli_option_helpers.ts`、`packages/repl/src/{commands,paste,ui}`、
`src/runtime-daemon`；OpenCode 的 `cli/cmd/run.ts`、`cli/cmd/tui`、
`cli/cmd/{session,export,import,upgrade,uninstall,stats,mcp}.ts`；以及 NZ-Coder
的 `interface`、`runtime/runner.py`、`runtime/native_sdk.py`、`sdk.py` 与测试。

状态严格使用规定枚举。`X` 表示相对 InfCodeX，`O` 表示相对 OpenCode。
所有 Partial/Missing 均在“证据/差距”列解释。

## Terminal Product Capability Matrix（93 项）

| 类别 | 能力 | X | O | 证据/差距 |
|---|---|---|---|---|
| Startup | interactive start | Aligned | Aligned | 持久 fullscreen composer。 |
| Startup | first-run | Mostly aligned | Mostly aligned | 有 init/doctor；引导式 onboarding 较浅。 |
| Startup | doctor | Aligned | Mostly aligned | offline/json/strict；无远端联检。 |
| Startup | provider setup | Mostly aligned | Mostly aligned | `/connect` 已有；OAuth 覆盖较少。 |
| Startup | workspace init | Aligned | Mostly aligned | 安全 init；无项目迁移向导。 |
| Startup | continue | Aligned | Aligned | `run --continue` 恢复 workspace latest。 |
| Startup | resume | Aligned | Aligned | `run --resume ID` 校验 exact Session。 |
| Startup | cwd | Aligned | Aligned | scoped `--cwd`。 |
| Input | multiline | Aligned | Aligned | Enter/Alt+Enter。 |
| Input | history | Aligned | Aligned | 私有跨启动 FileHistory。 |
| Input | history search | Aligned | Aligned | prompt_toolkit search。 |
| Input | slash completion | Aligned | Aligned | `/` 菜单、palette、参数补全。 |
| Input | @file | Aligned | Aligned | workspace-bounded 补全与附件。 |
| Input | clipboard text | Aligned | Aligned | application/native/OSC52。 |
| Input | clipboard image | Aligned | Aligned | Linux/macOS/Windows/WSL，MIME/10MB 边界。 |
| Input | drag/drop | Mostly aligned | Mostly aligned | 路径 paste；无终端协议 drop event。 |
| Input | PDF | Aligned | Mostly aligned | PDF/DOCX 预处理与 FilePart。 |
| Input | external editor | Aligned | Aligned | Ctrl+X E、`/editor`。 |
| Input | shell command | Aligned | Aligned | `!` 经过 permissioned ToolExecutor。 |
| Input | prompt queue | Mostly aligned | Aligned | follow-up takeover；无队列编辑 UI。 |
| Model | provider picker | Aligned | Aligned | `/connect` 与 picker。 |
| Model | model picker | Aligned | Aligned | async fuzzy picker。 |
| Model | model search | Aligned | Aligned | discovery + offline catalog。 |
| Model | favorites | Aligned | Mostly aligned | workspace favorites。 |
| Model | recent | Aligned | Aligned | recent + F2。 |
| Model | variants | Aligned | Aligned | `/variants`、`--variant`。 |
| Model | effort | Aligned | Aligned | `--effort` 进入 RunRequest。 |
| Model | fallback | Partial | Mostly aligned | 只有图像 fallback，无通用 model failover。 |
| Session | new | Aligned | Aligned | `/new-session`。 |
| Session | list | Aligned | Aligned | table + picker。 |
| Session | resume | Aligned | Aligned | exact/latest。 |
| Session | rename | Aligned | Aligned | 手工 title 权威。 |
| Session | delete | Aligned | Aligned | 确认和 owned artifact cleanup。 |
| Session | fork | Aligned | Aligned | Message/Part rekey + child clone。 |
| Session | undo | Aligned | Aligned | turn + file state。 |
| Session | redo | Aligned | Mostly aligned | 最近 undo 可 redo。 |
| Session | timeline | Aligned | Aligned | user-turn timeline。 |
| Session | message inspect | Aligned | Aligned | overlay + anchors。 |
| Session | copy | Aligned | Aligned | `/copy`、OSC52/native。 |
| Session | export | Aligned | Aligned | `/export`；格式较少。 |
| Session | parent/child | Mostly aligned | Aligned | 可查看/续跑；缺树形导航面板。 |
| Runtime | cancel | Aligned | Aligned | input/run/tool/process-group cancellation。 |
| Runtime | retry display | Aligned | Aligned | transient backoff。 |
| Runtime | queued prompt | Mostly aligned | Aligned | 单 follow-up；无列表/重排。 |
| Runtime | daemon | Missing | Missing | HTTP service 不是 lease/owned daemon。 |
| Runtime | attach | Missing | Missing | 无 TUI attach runtime。 |
| Runtime | reconnect | Missing | Missing | 无 cursor/replay-gap reconnect。 |
| Runtime | remote | Missing | Missing | 本阶段明确延期。 |
| Agent | mode | Aligned | Aligned | default/acceptEdits/plan/auto。 |
| Agent | plan | Aligned | Aligned | plan policy + UI。 |
| Agent | subagent | Aligned | Aligned | child Session、scope/worktree。 |
| Agent | background | Mostly aligned | Aligned | runtime 有；产品控制面较浅。 |
| Agent | agents picker | Missing | Missing | child picker 不等于 AgentDefinition picker。 |
| Agent | steering | Partial | Mostly aligned | follow-up steering；缺多消息管理。 |
| Agent | handoff | Mostly aligned | Aligned | typed handoff；终端 owner transition 较弱。 |
| Tool UX | tool cards | Mostly aligned | Aligned | 通用卡片成熟；specialised 种类较少。 |
| Tool UX | live output | Aligned | Aligned | Bash preview + elapsed。 |
| Tool UX | diff | Mostly aligned | Aligned | `/diff` 已有；patch 展开不统一。 |
| Tool UX | expand | Aligned | Aligned | hidden/compact/full。 |
| Tool UX | permission | Aligned | Aligned | once/always/reject。 |
| Tool UX | question | Aligned | Aligned | choice/multi/custom/dismiss。 |
| Tool UX | tool search | Aligned | Mostly aligned | ranked budgeted unlock。 |
| Extensions | skills | Aligned | Aligned | provenance/scope/allowed_tools。 |
| Extensions | MCP | Mostly aligned | Aligned | transport/OAuth/catalog；公网互操作证据少。 |
| Extensions | hooks | Mostly aligned | Aligned | runtime hooks；缺完整 lifecycle UI。 |
| Extensions | tool packs | Aligned | Mostly aligned | optional run-owned unlock。 |
| Extensions | plugins | Different by design | Missing | 显式 tools/skills/MCP，不引入 Agent 框架。 |
| Extensions | custom commands | Missing | Missing | 按阶段明确延期。 |
| Extensions | reload | Partial | Mostly aligned | settings 可 reload；extension hot reload 未产品化。 |
| Extensions | enable/disable | Partial | Mostly aligned | 有 status；无统一 toggle。 |
| Memory | memory list | Aligned | Mostly aligned | `/memory` + ledger。 |
| Memory | proposal | Partial | Missing | backend 有；无 terminal inbox。 |
| Memory | review | Missing | Missing | 无 review queue UI。 |
| Memory | approve | Missing | Missing | API 有；终端未暴露。 |
| Memory | reject | Missing | Missing | API 有；终端未暴露。 |
| Memory | ledger | Mostly aligned | Mostly aligned | durable provenance；审计视图有限。 |
| Automation | run command | Aligned | Aligned | `nz-coder run`。 |
| Automation | stdin | Aligned | Aligned | pipe + positional 合并。 |
| Automation | text output | Aligned | Aligned | 仅 final text。 |
| Automation | JSON | Aligned | Aligned | 单稳定 envelope。 |
| Automation | JSONL | Aligned | Mostly aligned | canonical RuntimeEvent + result。 |
| Automation | exit codes | Aligned | Mostly aligned | 0/1/2/3/4。 |
| Automation | session flags | Aligned | Aligned | session/continue/resume/no-session。 |
| Automation | file arguments | Aligned | Aligned | repeated file/attach 共用 FilePart。 |
| Automation | shell completion | Mostly aligned | Mostly aligned | bash/zsh/fish；model 值未动态补全。 |
| Operations | doctor | Aligned | Mostly aligned | offline/json/strict。 |
| Operations | logs | Partial | Mostly aligned | trace 可查；无统一 logs command。 |
| Operations | stats | Aligned | Aligned | usage/model/tool/cost。 |
| Operations | trace | Aligned | Mostly aligned | `/trace` + JSONL。 |
| Operations | upgrade | Missing | Missing | 交给包管理器。 |
| Operations | uninstall | Missing | Missing | 交给包管理器。 |
| Operations | config | Mostly aligned | Aligned | 分层配置；无统一 config CLI。 |
| Operations | completion | Aligned | Aligned | 离线 shell completion。 |

表中 daemon/attach 等 `Missing` 是 NZ-Coder 相对参考项目缺失；两个参考项目的
真实源码均有对应实现。

## 本阶段闭环

```text
nz-coder run -> RunRequest -> AgentClient -> NativeSDKRunner
             -> AgentRunner -> Session/Model/Tool Runtime -> RunResult
```

`RunOptions.on_event` 只投影既有 RuntimeEvent；`--no-session` 使用
EphemeralSessionStore。Headless 与 SDK 都通过离线真实
Model→list_directory→Model→Final 测试；将 `AgentLoop.__init__` 设为失败也不影响
Headless。交互入口仍保留一个 maintenance facade，但它把执行交给共享 Runner。

文本/外部编辑器进入 User Message；`@file`、`/attach`、路径粘贴、clipboard
image、`run --file/--attach` 最终共用 `tag_file_attachments -> FilePart`。
`!command` 刻意不写 transcript，而是复用 PermissionManager/ToolExecutor/bash。

## 量化报告

| 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 有名 top-level commands（不计默认 TUI） | 8 | 10 |
| headless run modes | 0 | 3（fresh/continue/resume） |
| output modes | 0 | 3（text/json/jsonl） |
| headless semantic flags | 0 | 14（effort 是 variant alias） |
| Native SDK AgentLoop 运行依赖 | 0 | 0 |
| Interactive AgentLoop 兼容依赖 | 1 | 1 |
| attachment/file ingress | 3 | 7 |
| focused terminal product tests | 36 | 62 |
| platform clipboard contracts | Linux 部分 | Linux/macOS/Windows/WSL |

## Product parity 估算

按 Aligned=1、Mostly=0.75、Partial=0.4、Missing=0，Different-by-design 与
Out-of-scope 不进分母做保守估计：

- 对 InfCodeX terminal product：约 **76%**。Aligned 是输入、Session、Agent
  runtime 和 automation；Remaining P0 是真实跨平台 TTY/信号/provider-error
  回归；Remaining P1 是 daemon/attach/reconnect、memory inbox、agent picker。
- 对 OpenCode terminal product：约 **72%**。Aligned 是 headless、permissions、
  Session 基础和 Model UX；Remaining P0 同上；Remaining P1 还包括 specialised
  tool renderer、remote attach、配置/运维命令。
- Out of scope：IDE、Web、cloud sharing、account/organization、marketplace、
  auto-update，以及本阶段明确延期的 persistent PTY/daemon。

百分比是源码能力覆盖估计，不是体验分或 SWE-bench 成绩。Cluster A/B/C 已闭环；
下一阶段应优先做真实 Linux/macOS/Windows 终端兼容矩阵。
