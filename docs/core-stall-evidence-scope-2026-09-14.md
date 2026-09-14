# 停滞证据稳定性与异步结论作用域

对照：NZ-Coder `8484e2d7b6cc9e2d2f3bddd0cfb31987d599df30`；InfCodeX `d3a812379b589597347f5be12d5b68477e577f02`。

| 参考行为 | 当前链路 | 本轮适配 | 验收 |
|---|---|---|---|
| L1 候选、对应轨迹交给异步 L2，结论只对原片段生效 | `find_repeated_tool_calls` 启动现有 `StallSidecarOrchestrator`，`RecoveryState` 维护连续计数 | 发送 L2 时捕获 RecoveryState 的 revision、签名和计数；回调应用前用三者核对，不让 A 的迟到结论清除 B | 同签名新计数、证据变化、reset/取消后的旧结论均不改变当前计数 |
| 工具轨迹中的有效变化才是进展 | 结果投影后已有同步/异步 evidence 回调 | 接收 `command_failed` 与 metadata；失败命令保留退出码和诊断，去除时间戳/耗时噪声；状态轮询只比较结构化 state/status/phase | 同一失败仅耗时变化、pending 仅时间变化不清零；相关诊断或状态变化可放行 |
| 有效 not-stuck 产生一次反馈，未知不替代判断 | 已有一次性 nudge、epoch、取消和 trace | 仅 `sidecar_ok + is_stuck=false` 且 scope 仍匹配时清除计数；timeout/error/invalid 仍 unknown | 现有 Runner/Sidecar 测试和受控回调通过 |

本轮复用现有 StallDetector、StallSidecarOrchestrator、RecoveryState、ToolRuntime pipeline 和 loop trace 回调，没有新增检测器、Sidecar、状态机或评分器。`command_failed` 继续与 `dispatch_failed` 分离；无法可靠区分的输出保留当前计数并交给既有 L2/有界策略。
