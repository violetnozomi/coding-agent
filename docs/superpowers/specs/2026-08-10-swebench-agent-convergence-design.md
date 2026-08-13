# SWE-bench Agent Convergence Design

## Goal

修复 r3 轨迹暴露的运行器假超时，并让 strict SWE-bench Agent 在调查、命令执行和验证后终止三个阶段形成有界闭环。改动只影响严格评测路径；普通终端产品继续保留自然结束、交互式权限和宽松工具能力。

## Evidence and reference behavior

- NZ 当前父进程先 `join(timeout)`，随后才读取包含全部工具输出的 `multiprocessing.Queue`。超过 pipe buffer 后子进程无法退出，造成 22 个已完成实例被误判为超时。
- InfCodeX 评测 runner 持续排空子进程输出，完整轨迹单独落盘，只通过进程边界返回有界终态。
- infcode-dev 的 Bash schema 显式提供 `workdir`；InfCodeX 的终端工具结果可以直接结束 Runner。
- r3 中变化参数的 grep/read 游走不会触发三次完全相同调用检测，需要基于“自上次 mutation 以来的调查预算”补充确定性收敛状态。

## Design

### 1. Bounded subprocess result protocol

父进程以单调时钟 deadline 等待 Queue 结果，而不是等待子进程先退出。收到 typed payload 后再短暂 join；超时则终止进程并抛出 `AgentRunTimeout`。子进程只返回 Agent 状态和每个工具的有界摘要，完整输出继续由现有 trace 文件负责。

终态互斥：成功 payload、错误 payload、真实 timeout、无 payload 异常退出只能出现一种。不得使用 `Queue.empty()` 判断正确性。

### 2. Model-visible strict shell contract

`bash` 增加可选 `workdir`，路径必须解析在 workspace 内。strict prompt 明确列出允许的 executable、Git 子命令、Python `-m` 模块和禁止项；拒绝消息给出可执行替代方案。网络、Git history、任意 Python 和间接 shell 仍保持 fail-closed。

过程 policy rejection 与 patch semantic risk 分开记录，避免把一次被成功纠正的 strict 命令误当成补丁语义风险。

### 3. Deterministic phase-progress control

RuntimeState 记录自最近一次成功 mutation 以来的调查调用数，以及已经发出的收敛提醒次数。strict 模式达到软阈值时注入一次具体 nudge；达到硬阈值后拒绝新的纯调查调用，要求基于已有证据编辑、查看 diff 或明确结束。成功写入会开启新 generation 并清零调查预算。

该检测不调用额外模型，不依赖相同参数，因此能覆盖变换关键词/文件的语义游走。阈值只在 strict SWE 运行时生效。

### 4. Verification terminal signal

strict 模式中，当 `verify_changed_files` 返回可接受结果，且 RuntimeState 已确认非空 source diff 时，当前工具批次结算后返回 terminal action。Session/trace 仍持久化完整 tool result 和唯一 run terminal；不再额外请求模型“决定是否停止”。失败或无 diff 的验证不会终止。

### 5. Structured code-navigation triggers

把 `repo_map`、`read_symbol`、`find_symbol_callers`、`code_references` 和 `analyze_impact` 的具体使用条件写入真实 SWE system prompt。该提示用于减少整文件读取和重复 grep，不改变工具实现。

## Error handling

- IPC timeout 必须清理子进程；异常退出必须包含 exit code；Queue 资源在所有路径关闭。
- `bash.workdir` 为空时沿用 workspace，绝对路径或 `..` 逃逸返回 `Error:`，不启动 shell。
- progress detector 只拦截 read/search 类工具，不拦截 edit、diff、verify 或最终文本响应。
- terminal verification 仅接受无 dispatch failure、无 command failure 的成功结果。

## Verification

- 构造超过 64KB 工具事件的 Agent，证明其在短 timeout 内正常返回。
- 覆盖真实 sleep timeout、子进程异常、无结果退出。
- 覆盖 `bash.workdir` 正常子目录和路径逃逸，以及 strict 拒绝建议。
- 覆盖 progress soft nudge、hard gate、成功 edit 后 reset，且普通模式不触发。
- 覆盖 strict 验证成功直接 terminal、验证失败继续、普通模式继续。
- 运行聚焦 pytest、相关 loop/runtime/swebench 回归、静态编译；不启动新的付费 SWE-bench 批测。

## Scope exclusions

- 本轮不重跑 Lite 300 或 Verified 500，不宣称分数提升。
- 不放宽 strict 网络/history 限制。
- 不为普通终端 Agent 强制首次编辑 deadline。
- 不引入 Agent 框架或新的第三方依赖。
