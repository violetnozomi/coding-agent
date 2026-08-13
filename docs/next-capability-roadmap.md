# Next Capability Roadmap

## P0：直接影响修复成功率

1. 用 SWE-bench trace 建立 repo retrieval benchmark：目标文件 recall、symbol/reference 命中率、首次有效编辑前工具次数。
2. 分层验证闭环：静态检查 → 定向测试 → 回归测试，并把失败证据作为下一轮激活，而非另建 loop。
3. 将 AgentLoop legacy adapter 消费者逐个迁到 native composition；每次只删除消费者归零的 wrapper。

## P1：大型仓库和长任务

1. 基于 benchmark 选择跨语言增量 symbol index 或 call hierarchy；不同时铺开 embedding 与 watcher。
2. Dynamic Tool Exposure 进行真实模型 A/B：全 schema vs hints + exact unlock，测任务成功率与误选率。当前仅有 provider-free recall 证据，不上线过滤。
3. Skills 补 workspace/user/bundled 的确定性 precedence、验证结果和生命周期事件。
4. 缩小 21-module SCC，优先拆 `runtime.services ↔ tools/project_creation/subagent` composition cycle。

## P2：平台能力

- SDK composition builder，使第三方只提供 ModelGateway/ToolRuntime/SessionStore 即可构造 native Runner。
- trace metrics 聚合：model/tool latency、compaction、retry、stall、child critical path、cost confidence。
- MCP/HTTP SDK 的版本化 event schema 与兼容测试。

## P3：体验

- TUI event consumer、交互式 tool/skill discovery、session timeline；不得反向依赖 core。

## 下一轮进入条件

先取得至少 20 个真实 SWE trace 的检索与验证统计；没有 benchmark 证据时不继续横向抄功能。
