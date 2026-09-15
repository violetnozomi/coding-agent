# InfCodeX 对照入口校准

固定版本：NZ-Coder `c58c947402c5d156c68c783df1d9167466aa4b7a`；InfCodeX `d3a812379b589597347f5be12d5b68477e577f02`。

| 请求配置 | 实际有效配置 | 执行路径 | 覆盖能力 |
|---|---|---|---|
| `ReferenceRunRequest.reasoning=medium/high` | InfCodeX `--effort medium/high`；`off/auto/quick/balanced/deep` 仍走兼容 `--reasoning` | `--mode json` + 位置参数 prompt + `--agent-mode sa` | 真实 JSONL 工具/终止事件、SA 原生循环；不代表 managed-task |
| `--print` 文本请求 | 不与 JSON 模式组合；使用 `--mode json` | JSONL 事件采集与 changed-files 快照 | 可检查终止事件、工具结果和 usage；缺失事件保持 unknown |
| `BehaviorBenchmarkConfig` 生产默认 | `guidance` 等配置按显式值传入；核心工具列表包含 `read_tool_result` | `ProductionAgentBehaviorDriver` → `AgentClient` → Native Runner | Artifact 读取、编辑恢复、STALE_READ、stall 反馈在生产工具面可达；旧 `v3/lookup` 保留为实验映射 |
| `run_reference_behavior_matrix(repetitions=N)` | 严格执行 `max(1,N)` 次；N=1 就是一轮 smoke | `AgentBehaviorBenchmark` + 选定 reference adapter | 不静默增加主模型或辅助调用 |

InfCodeX 适配器只固定 SA 对照入口，不把 SA 结果概括为 managed 能力。当前本地参考依赖与 Provider 未授权时只执行 probe/受控 fixture；不通过 npx 下载运行时，也不报告自主编码成功率。事件归一化依据结构化 `status`、`exit_code` 或 `returncode` 判断失败；日志中的单词不会自动改变命令状态。
