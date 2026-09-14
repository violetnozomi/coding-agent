# 重复调用、停滞判断与一次性纠偏

对照版本：NZ-Coder `2279cbfbfc601fb047fcb6f3e10958395afdf6c5`；InfCodeX `d3a812379b589597347f5be12d5b68477e577f02`。

| 参考行为 | 当前行为 | 差异 | 对齐/适配 | 验收 |
|---|---|---|---|---|
| L1 重复调用触发异步 L2；L2 结合工具轨迹后只发一次 nudge | 已有 `StallDetector`、`StallSidecarOrchestrator`、epoch 和一次性 nudge | `RecoveryState.observe_tool_call` 仍可能独立把重复调用当最终结论 | 保留 L1/L2；在现有 RecoveryState 记录同调用的实际结果摘要，只有结果变化才重置连续计数 | 新证据的重复读取继续执行；unchanged 结果仍达到阈值阻断 |
| L2 `is_stuck=false` 表示本段不是停滞 | 已有 L2 verdict，但不影响 RecoveryState 的硬计数 | not-stuck 可能迟到且无法解除旧计数 | 仅接受 `trace=sidecar_ok` 的结构化 not-stuck，在现有 loop 绑定处清除旧计数；timeout/error/invalid 保持 unknown | 受控 sidecar 返回 not-stuck 时不误挡后续合法调用 |
| nudge 进入主 Agent 后续历史并给具体下一步 | 已有 pending nudge、投影和后续消息路径 | 需要避免旧结果在新证据后继续施加影响 | 复用 epoch、取消和现有 nudge；不执行 nudge 中的写操作 | 既有 Runner/sidecar 测试保持通过 |

`record_tool_result_evidence` 只保存同一工具与参数的输出哈希，避免引入进展评分器。读取缓存命中、失败结果、被策略拒绝的调用不会被当成新证据；成功写入仍由既有事务提交重置。L2 失败不证明“没有停滞”，因此不会解除硬保护。runtime-owned 验证调用继续沿用既有排除路径。
