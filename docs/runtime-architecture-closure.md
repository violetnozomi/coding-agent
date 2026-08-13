# Runtime Architecture Closure (第四阶段)

日期：2026-08-11

## 结论

本阶段把执行内核收口到 `AgentRunner + SessionRuntime + RunContext + RuntimeServices`。Native、Main facade、Child、Background 与 Workflow Agent node 最终都进入同一个 Runner 状态机；Background 和 Workflow 只保留调度职责。核心边界现冻结，新增能力不得再创建 Provider/Tool/Session 循环。

```text
CLI / HTTP / SDK / Evaluation
              |
          RunRequest
              v
 AgentRunner -- MiddlewarePipeline -- RuntimeEventSink
      |               |                    |
 SessionRuntime   run/model/tool       SessionEventBus
      |
 RunContext -> Session(transcript + durable usage)

Workflow DAG -> BackgroundAgentManager -> run_subagent -> Agent -> AgentRunner
Foreground child --------------------------^
Process child ---- spawn boundary -> run_subagent -------^
```

## 已收口内容

- Native Runner 不 import `AgentLoop`，并独立完成 Model → Tool → Model → Final。
- Main 的 `AgentLoop.run()` 是 facade；循环只存在于 `AgentRunner._run_turns()`。
- `MiddlewarePipeline` 是唯一新增公共抽象：before 正序、after/error 逆序，覆盖 run/model/tool batch；原始执行异常保持权威。
- `RuntimeEvent` 是核心事件信封，生产 sink 投影到已有 `SessionEventBus`，没有创建第二套 UI 协议。Legacy Main 已显式抑制重复投影。
- SDK 可直接注入 Runner，支持 typed run、parent-linked child 与相同 session ID resume，不需要构造 `AgentLoop`。
- Background thread/process 和 Workflow Agent node 统一经过 `run_subagent()`；TaskRecord 只拥有调度、取消、scope/worktree、artifact 与结果引用。
- `runtime/core` 有静态 import guard，禁止依赖 CLI、AgentLoop、LSP、repo intelligence、verification/project-creation 等 coding concrete。

## Legacy Adapter burn-down

| Adapter | 源码消费者 | `host._*` | 本阶段决定 | 删除条件 |
|---|---:|---:|---|---|
| context | 2 | 4 | 保留兼容边界 | Main 不再投影旧 Context owner |
| lifecycle | 2 | 21 | 保留兼容边界 | CLI/HTTP 全部直接使用 native lifecycle |
| model | 2 | 0 | 保留薄投影 | Provider context 原生构造完成 |
| runner | 2 | 0 | 保留 facade | AgentLoop 从产品组合根移除 |
| tool | 3 | 3 | 保留同步兼容 | sync legacy tool path 消失 |

没有发现零消费者 adapter，因此没有为“看起来清爽”而删除仍承担兼容责任的代码。

## AgentLoop ownership

| 类别 | 当前 owner | AgentLoop 状态 |
|---|---|---|
| transcript / durable usage / lineage | Session / RunContext | facade 投影，不是事实源 |
| turn/iteration/retry/compaction counters | RunContext / focused execution contexts | 兼容字段尚存，禁止新增 |
| model/tool/context execution | RuntimeServices + RunnerExecutionContext | capability projection |
| permissions/provider/MCP/worktree | product composition scope | 配置与资源 owner |
| CLI renderer/callbacks | host layer/EventSink | 不进入 core |
| planning/verification/memory/snapshot | stateful services/observers | 不机械 middleware 化 |

## 量化

| 指标 | 阶段前 | 阶段后 |
|---|---:|---:|
| AgentLoop LOC | 3,826 | 3,826 |
| AgentRunner LOC | 1,005 | 1,057 |
| AgentRunner 实例属性 | 2（旧口径） | 7（含单一 pipeline/service/factory） |
| Runtime middleware | 0 | 1 pipeline / 1 event middleware |
| Legacy adapters | 5 | 5（均有消费者） |
| Core coding-concrete import violations | 未设 guard | 0 |
| Python import SCC | 未重测 | 4，最大 21 modules |

AgentLoop 本轮没有缩短，是因为目标是先冻结行为和阻止反向增长；剩余 3,826 LOC 主要仍是产品能力组合与兼容 facade，后续只能按 adapter 消费者归零逐块清退。

## 冻结规则

1. `AgentRunner` 是唯一 model/tool turn loop。
2. `Session` 是 transcript 与 durable usage 唯一事实源。
3. Workflow/Background 不能 import Provider 或执行 Tool batch。
4. Core 只能依赖抽象 contract；coding concrete 在 composition edge 注入。
5. 新横切能力优先 middleware/observer/service，不向 Runner 增加产品细节。
6. Legacy adapter 只允许减少，不允许新增 host-private 读取。
