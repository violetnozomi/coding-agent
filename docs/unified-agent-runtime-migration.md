# NZ-Coder Unified Agent Runtime Migration Report

## 1. Executive Summary

NZ-Coder 已从“Main 与 child 各自拥有执行循环”的结构迁移为一套生产执行内核。CLI、HTTP、evaluation、SWE-bench 和 Python SDK 由 `runtime/execution/composition.py` 构造生产 Agent；Main、read/write child、background 与 workflow 最终进入 `AgentRunner.run()`，资源绑定由 `ProductionRuntimeHost` 负责，唯一 turn 状态机是 `AgentRunner._run_turns()`。本报告中的“完成”只表示下方验收矩阵有源码和测试证据的架构事项，不代表 SWE-bench 分数、第三方 Provider 互操作或终端 UI 产品体验已经等同 InfCodeX。

本次重构没有删除已有 Provider、MCP、Memory、LSP、Workflow、Verification、Transaction 或 Session 能力。主要变化是重新分配 ownership：模型调用归 focused `ModelExecutionContext` + `ProductionModelGateway`，工具批生命周期归 `ToolExecutionContext` + `ProductionToolRuntime`，上下文预检归 `ContextExecutionContext` + `ProductionContextManager`，唯一 turn 状态机归 host-free `AgentRunner._run_turns()`，run 初始化与终止策略归 `LifecycleExecutionContext` + `ProductionRunLifecycle`，完整 transcript/checkpoint/finalize 归 `SessionRuntime`，workspace/MCP/ContextVar 生命周期归 `ProductionRuntimeHost`。

原先五个最高风险问题的当前状态：

1. Main/child 双循环：已消除；`run_subagent()` 不再包含 Provider/tool turn loop。
2. `AgentLoop` 同时拥有 orchestration：生产 turn loop、run lifecycle、资源宿主、模型异步调用/取消结算、工具批及其策略/结果投影、上下文预检、附件预处理、guardrail、Agent transition、session checkpoint、Memory recall/learning 和 completion verification 均已移出。`AgentLoop` 仍是 3K+ 行的兼容与 coding capability host，但上述生产执行语义只保留短 facade，不再拥有第二套实现；后续继续拆 planning/reflection/trace adapter 属于维护性收敛，不能再被描述为“核心 Runtime 尚未接线”。
3. 反向依赖：`state -> runtime`、`runtime -> interface` 已由 AST 守卫禁止。
4. 抽象未接入生产：Tool/Context/Session/Runner 均已接入生产 facade；不再以孤立单元测试代表完成。
5. 公共 SDK：`nz_coder.sdk.AgentClient` 和 `run_agent()` 使用与 CLI/HTTP 相同的生产 Agent 链并返回 `RunResult`；`AgentDefinition` 的 guardrails、nested handoffs、output schema、tool/model/provider/effort 声明会投影为真实 `AgentGraph`，不再是未消费字段。

2026-08-10 最终 Session-first 收口进一步确认：

- `AgentRunner._run_turns()`、`ProductionTurnModelRuntime`、
  `ProductionRunLifecycle`、异步 Tool policy/result projection 均为零 `host` token；
- Native `_run_request` 不读取 AgentLoop 或 legacy adapter；product compatibility 路径仍在
  `runtime/adapters/*` 和稳定 legacy facade 边界投影 AgentLoop 能力；
- Main、foreground child、background child 和 workflow child 都进入同一 Runner；
- child transcript 存在 parent-linked native Session，task `state.json` 不再保存完整
  messages 或顶层 usage/iterations，只保存调度、verification 和终态结果投影；旧任务
  仅在首次迁移前读取 legacy messages；
- `RuntimeServices` 不再包含 `sessions`/FileSessionRepository 第二 owner；旧类型仅为
  非生产直接导入兼容。

## 2. InfCodeX Architecture

真实参考链路的关键源码是：

- `references/InfCodeX/packages/agent/src/primitives/agent.ts`：声明式 Agent 与 coding preset。
- `references/InfCodeX/packages/agent/src/primitives/runner.ts`：公共 Runner frame 与 preset dispatcher。
- `references/InfCodeX/packages/coding/src/agent.ts`：`runKodaX()` coding runtime 入口。
- `references/InfCodeX/packages/coding/src/agent-runtime/run-substrate.ts`：coding substrate 生命周期。
- `references/InfCodeX/packages/coding/src/child-executor.ts`：child 隔离、配置、结果包装，调用 `runKodaX()`，不维护第二套 Provider loop。
- `references/InfCodeX/packages/coding/src/tools/registry.ts`：coding tool registry。
- `references/InfCodeX/packages/coding/src/agent-runtime/context-budget.ts`：模型窗口预算。

```text
REPL / SDK / managed task
          |
          v
       Runner
          |
    preset dispatcher
          |
          v
      runKodaX
          |
     run-substrate
   +------+------+----------------+
   |             |                |
 provider     tool runtime    context/session
   |             |                |
 stream       results         compaction/events
   +-------------+----------------+
                 |
              terminal

child-executor = isolation/profile/result owner -> runKodaX
```

InfCodeX 的重点不是 TypeScript 文件形状，而是 child 只拥有差异化配置和资源隔离，Provider、tool calling、context、retry、cancellation 与 terminal semantics 复用 coding substrate。

## 3. NZ-Coder Current Architecture

```text
CLI / HTTP / SWE / evaluation / SDK
                 |
                 v
       RuntimeCompositionRoot
                 |
                 v
             AgentLoop
        (compatibility/capability host)
                 |
                 v
          AgentRunner.run
                 |
        ProductionRuntimeHost
  (workspace/session/MCP/ContextVar lifecycle)
                 |
                 v
       AgentRunner._run_turns
      +----------+-----------+----------------+
      |          |           |                |
 ModelGateway ToolRuntime ContextManager SessionRuntime
      |          |           |                |
 providers  policy/result  budget/compact  existing JSON store
                 |
      guardrails / inputs / transitions / lifecycle
```

一次生产 turn 的实际链路：

```text
User message
 -> AgentLoop.run compatibility facade
 -> AgentRunner.run
 -> ProductionRuntimeHost binds workspace/session/MCP/memory/skills/tools
 -> AgentRunner._run_turns
 -> ProductionContextManager.prepare_async
 -> ProductionModelGateway through AgentLoop model adapter
 -> normalized Assistant message/usage/parts
 -> ProductionToolRuntime.execute_batch_async
 -> tool policy/guardrail/scheduler/transaction/result projection
 -> SessionRuntime.checkpoint(RunContext)
 -> verification/reflection/terminal hooks
 -> stable status dictionary (legacy hosts) or RunResult (SDK)
```

Child 链路为：

```text
task / AgentManager / Workflow
 -> run_subagent
 -> prepare/resume state + worktree + model route + child graph
 -> `declared_runtime(graph).build()` constructs child capability host and services
 -> agent.runner.run(agent, messages)
 -> same ProductionRuntimeHost + AgentRunner._run_turns
 -> verification/result packaging + cleanup
```

## 4. InfCodeX vs NZ-Coder

| Layer | InfCodeX | NZ-Coder after migration | Status |
|---|---|---|---|
| Agent declaration | `Agent` primitive/preset | `AgentDefinition`, `AgentGraph`, `AgentRuntimeAssembly` | wired |
| Public Runner | `Runner` | `AgentRunner` | trace verified |
| Coding substrate | `runKodaX` / `run-substrate` | `ProductionRuntimeHost` + `_run_turns` | trace verified |
| Main/child reuse | child calls `runKodaX` | child calls `agent.runner.run` | trace verified |
| Model boundary | LLM/provider layer | `ProductionTurnModelRuntime` + `ProductionModelGateway` | production verified |
| Tool lifecycle | coding registry/execution | `ProductionToolRuntime` + policy + scheduler + result projector | contract verified |
| Context budget | context-budget/runtime middleware | `ProductionContextManager` | production wired |
| Session persistence | coding session/runtime state | `Session` + `SessionRuntime` + `SessionStore` | production wired |
| Background/workflow | managed task delegates coding run | AgentManager/Workflow delegate `run_subagent` | wired |
| SDK | Runner/coding client | `AgentClient`, `AgentHandoff`, `run_agent`, `RunResult` | declaration projection verified |

InfCodeX 仍有更大的 TypeScript SDK/daemon/client 生态；这不等于 NZ-Coder 的 Agent Core 有第二套循环。NZ-Coder 当前保留 Python CLI/HTTP 产品边界，没有机械复制 Node worker/Promise/EventEmitter。

## 5. Architecture Mapping Table

| InfCodeX abstraction | Responsibility | Old NZ owner | Current target owner |
|---|---|---|---|
| Agent | immutable role/tool/model policy | prompt flags in `AgentLoop` | `AgentDefinition` / `AgentSpec` |
| Runner | execution orchestration | `AgentLoop._run` + child for-loop | `AgentRunner` |
| Runtime context | per-run resource scope | scattered ContextVars | `ProductionRuntimeHost` |
| LLM provider | wire protocol | loop/provider mixed calls | `ProductionModelGateway` + adapters |
| Streaming | deltas/fallback/settlement | loop-specific code | shared gateway called by Runner chain |
| Tool registry | schemas and dispatch lookup | `tools` registry | existing registry, unchanged public names |
| Tool executor/policy | admission and single call | `ToolExecutor` + loop | `ToolExecutor` + `ProductionToolPolicy` + guardrail service |
| Tool batch runtime | concurrency/txn/postprocess | `AgentLoop` | `ProductionToolRuntime` |
| Context budget | model-aware window | loop helpers | `ProductionContextManager` + prompt budget |
| Compaction | bounded semantic reduction | loop helpers | context manager trigger + shared compaction service |
| Session | durable transcript | direct `save_session` calls | `SessionRuntime` / `SessionStore` |
| Events/trace | ordered observations | mixed callbacks | SessionEventBus + trace, Runner entry event |
| Cancellation | settle model/tools/resources | separate Main/child paths | gateway/tool runtime/runtime host chain |
| Guardrail/handoff | policy and role transition | loop-specific | required guardrail/transition Runtime services |
| Input preflight | unsupported media conversion | loop-specific | required `ProductionInputPreflight` service |
| Run lifecycle | initialize/terminal settlement | loop-specific | required `ProductionRunLifecycle` service |
| SubAgent | isolation/profile/result | independent loop | `run_subagent` facade + shared Runner |
| Background | scheduling/cancel/slots | AgentManager | AgentManager, execution delegated |
| Workflow | plan/aggregation | Workflow runtime | Workflow runtime, execution delegated |
| Memory | recall/learning | loop + MemoryManager | `ProductionMemoryService` + scoped MemoryManager |
| Repo/LSP/MCP | coding substrate tools | tools/loop | existing capability services bound by host |
| SDK | embedding API | absent/experimental | `nz_coder.sdk` production chain |
| CLI/HTTP | presentation/transport | mixed construction | composition root + host callbacks |

## 6. Critical Architecture Problems

### Resolved P0

- Duplicated Main/child Provider/tool loop.
- Production `AgentLoop.run()` owning the turn state machine.
- State layer importing AgentManager/Worktree implementations.
- Runtime importing terminal-only interface helpers.
- Session repository existing without a production consumer.
- SDK using a simplified fake state machine different from the product.

### Remaining non-blocking P1/P2 boundaries

- `runtime/execution/loop.py` remains large because it is the compatibility host for planning/reflection, coding evidence, trace adapters and legacy public methods. 不能把文件仍大说成“完全解耦”；本阶段的可核验边界是核心 production execution ownership 已迁出，并由 AST 守卫防止回流。
- `runtime.core.RuntimeServices` 是生产服务图，必需端口包括 model、tools、context、sessions、events、host、memory、verifier、lifecycle、guardrails、inputs、transitions。Runner 对这些核心边界不得退回 `host._...` 私有实现。
- Public network Provider/MCP interoperability and SWE-bench score parity are external evidence tasks, not architecture facts; this migration used no paid Provider and makes no score-equivalence claim.

## 7. Target Package Architecture

```text
nz_coder/
  sdk.py                         public RunRequest/RunResult entry
  runtime/
    core/                        immutable contracts/profiles/state/events
    model_gateway/               provider-neutral invocation policy
    agent/                       admission, guardrails, handoffs, child execution
      subagent.py                isolation/profile/result facade
      agent_manager.py           background scheduling, not execution loop
    conversation/                prompts, context, input, and message handling
      context_manager.py         budget/preflight/compaction trigger
      input_preflight.py         image/document conversion
    execution/                   production construction and state machine
      composition.py             only production construction owner
      runner.py                  one orchestration state machine
      host.py                    run-scoped resources and cleanup
      loop.py                    product capability host and legacy AgentLoop
      run_lifecycle.py           initialize and terminal settlement
    tool_runtime/                scheduling and complete batch lifecycle
      policy.py                 admission/convergence/scheduling policy
      result_projection.py      durable contiguous tool results
    verification/                gates, evidence, recovery, judge, and stalls
    workflows/                   workflow definitions, scheduling, and aggregation
    process/                     workdir, subprocess, and snapshot services
    session/                     transcript, repository, lifecycle, and cleanup
    adapters/                    legacy-host projections into focused contexts
    observability/               run-scoped evidence
    worktree/                    isolated child workspace lifecycle
  providers/                     protocol adapters and capabilities
  tools/                         schemas, handlers, safe dispatch
  state/                         persistence and state primitives only
  interface/                     terminal presentation/interactions
  http_service/                  transport/session host
```

Dependency rules enforced by tests:

```text
interface/HTTP/SWE/SDK -> composition -> runtime -> capability services
state -X-> runtime implementations
runtime -X-> interface
tools/providers -X-> AgentLoop
```

## 8. Runtime Execution Model

`RunProfile` distinguishes `main`, `read_child`, `write_child`, `background` and `workflow`. A profile carries policy, not clients or mutable session state. Background and workflow retain scheduling, slot, worktree and aggregation ownership; they do not own a Provider loop.

`AgentRunner.run()` is the public kernel entry and writes `agent_runner_enter` to trace. Native `RunRequest` execution opens `SessionRuntime` directly and enters `_run_turns()` without `ProductionRuntimeHost`; only the explicit legacy compatibility path delegates resource binding to `ProductionRuntimeHost`. Both paths share the same single turn state machine.

Compatibility is explicit: `AgentLoop.run()` remains stable; `_run()` remains a thin facade; a legacy instance-level `_run` override is honored for old embedding/tests without creating a production loop.

## 9. Migration Roadmap

| Phase | Result | Completion evidence |
|---|---|---|
| 0 Characterization | Main/child facade and behavior frozen | provider-free tests |
| 1 Core contracts | Agent/profile/request/result/state/events/ports | contract tests |
| 2 Provider runtime | resolution/retry/stream/cancel/usage unified | production consumers and gateway tests |
| 3 Tool runtime | scheduling/transactions/result lifecycle extracted | focused tool/cancel/transaction tests |
| 4 Context/session | preflight and repository extracted | production delegation + persistence tests |
| 5 Shared Runner | Main facade delegates shared kernel | AST + production trace |
| 6 Child | old child turn loop deleted | AST + child trace |
| 7 Background/workflow | execution delegates child/shared Runner | manager/workflow suites |
| 8 Dependencies/SDK | reverse imports removed, SDK uses product chain | AST, SDK tests, real CLI smoke |

## 9.1 Original-prompt acceptance matrix

| Requirement from original prompt | Implementation evidence | Test evidence | Verdict |
|---|---|---|---|
| One execution kernel | `AgentRunner._run_turns` is the only Provider/tool turn loop | AST architecture guard | pass |
| Main/SubAgent reuse | Main uses composition; child uses `declared_runtime(graph).build()` and the same Runner | Main/child trace tests | pass |
| Provider/stream/cancel semantics shared | `ProductionTurnModelRuntime` owns sync selection, async worker and cancellation settlement | provider, stream and cancellation suites | pass |
| Tool/context/session shared | required `RuntimeServices` ports are production-composed and called by Runner | contract + architecture tests | pass |
| Guardrail/input/transition/lifecycle ownership | production Runner/ToolRuntime call required services; Loop methods are compatibility facades | architecture + behavior regression tests | pass |
| Memory and completion boundaries | `ProductionMemoryService` and `ProductionCompletionVerifier` are required services | memory + stop-consumer tests | pass |
| SDK public declaration | nested handoffs, guardrails, schema and model/tool policy become an `AgentGraph` | SDK projection tests | pass |
| Dependency direction | state cannot import runtime; runtime cannot import interface | AST dependency guards | pass |
| Cross-profile parity | coding and declared profiles share Runner/services and event ordering | profile parity trace test | pass |
| Existing feature preservation | compatibility facades retained; no tool/provider/session public names removed | focused regression suites | pass |
| SWE-bench score parity / live third-party interoperability | deliberately not inferred from architecture | requires separate external run | not claimed |

## 10. Testing Strategy

The migration is guarded at four levels:

- contract tests for immutable models, profiles, gateway, tool/context/session services;
- characterization tests for existing Main/child signatures and result shapes;
- architecture AST tests preventing duplicate loops and forbidden dependencies;
- production-chain trace tests proving Main and child enter the shared Runner.

Coverage includes buffered/streaming model calls, retry/fallback, context overflow, compaction, tool error classification, permissions, transaction rollback, cancellation settlement, session persistence/resume, handoff, background manager and workflow behavior。第三阶段最终验收结果为 `1613 passed`，Ruff、compileall 和依赖方向扫描通过。SWE-bench 和付费 Provider 调用不属于本次架构验收，不据此宣称分数或公网互操作等价。

## 11. Files Changed

| Priority | File | Old responsibility | Current action |
|---|---|---|---|
| P0 | `runtime/execution/runner.py` | absent/simplified parallel loop | owns the one production state machine |
| P0 | `runtime/agent/subagent.py` | independent Provider/tool loop | isolation and result facade; calls shared Runner |
| P0 | `runtime/execution/loop.py` | orchestration plus all capabilities | compatibility/coding adapter; delegates lifecycle |
| P0 | `runtime/execution/host.py` | responsibilities embedded in loop | owns resource scopes and cleanup |
| P0 | `runtime/tool_runtime/*` | scheduling embedded in loop | owns tool-batch lifecycle |
| P0 | `runtime/agent/guardrail_runtime.py` | guardrail policy embedded in loop | owns declared guardrails |
| P0 | `runtime/conversation/input_preflight.py` | image/document conversion embedded in loop | owns media preflight |
| P0 | `runtime/agent/agent_transition_runtime.py` | handoff/schema policy embedded in loop | owns Agent transitions |
| P0 | `runtime/execution/run_lifecycle.py` | init/finalize embedded in loop | owns run lifecycle |
| P0 | `runtime/conversation/context_manager.py` | context preflight embedded in loop | owns budget and compaction trigger |
| P0 | `runtime/session/session_repository.py` | direct Session writes | owns production checkpoints |
| P1 | `state/sessions.py` | imported runtime cleanup managers | exposes callbacks only |
| P1 | `runtime/session/lifecycle.py` | absent | installs concrete cleanup adapters |
| P1 | `sdk.py` | simplified service-only loop | uses production Agent chain |
| P1 | `message_schema.py` | fork reference logic in terminal layer | owns transport-neutral re-keying |
| P1 | `interface/timeline.py` | mixed projection and state mutation | terminal projection, re-export compatibility |

## 12. Third-stage Native Runtime and Child Session Closure (2026-08-11)

第三阶段在第二阶段 shared turn loop 之上补上真正的 Native API：

```python
result = await AgentRunner(runtime_services, execution_context_factory=...).run(
    request,
    options=RunOptions(stream=False),
)
```

该调用无需构造 `AgentLoop`，由 `SessionRuntime` 创建 `RunContext` 并完成 Mock
Model→Tool→Model→Final。Native `_run_request` 不调用 legacy adapter/RuntimeHost；只有
显式 `_run_legacy` 兼容方法局部导入旧 adapter。Main 的稳定 `AgentLoop.run()` API 保留，
但它先在边界生成 `RunRequest/RunOptions`，再单向进入同一 Native Runner。

`RunnerExecutionContext` 从 47 fields/45 callbacks 收敛到 10 fields/0 callbacks，剩余
字段是 lifecycle/policy/planning/control/hooks/messages/snapshots 等具名 service owner，
不是平面函数袋。需要准确说明：其中 planning/control/message/snapshot 的生产实现仍由
legacy coding facade 投影，所以 AgentLoop 尚未达到可删除的纯 facade 状态。

Child execution 使用与 Main 相同的 Runner、SessionRuntime、SessionProcessor、Context、
Model 和 async Tool runtime。新 TaskRecord 不创建 messages；旧 messages 只在原生
Session 不存在时 bootstrap 一次。native child 终态持久化会删除 messages、tokens、cost、
cost_known 和 iterations；`TaskStatus` 与 `SessionStatus` 分域，worktree/scope/conflict/
verification 继续留在任务编排层。

完整 callback ownership、state ownership、前后量化、SCC 审计、三方能力矩阵、Q1—Q6 与
Top 10 差距记录在 `docs/infcode-alignment-learning-log.md` 的 A238。最终架构验收为
`1613 passed`，另有 Ruff、compileall、import smoke、diff check；未用架构测试代替
SWE-bench、付费 Provider 或公网互操作证据。
# Fourth-stage closure update (2026-08-11)

Runtime core is now frozen around AgentRunner/SessionRuntime. One ordered
MiddlewarePipeline owns run/model/tool crosscuts; RuntimeEvent projects into the
existing SessionEventBus; the SDK has a direct typed Runner path with child
resume; Background and Workflow Agent nodes are statically guarded from owning
model/tool loops. Five legacy adapters remain because each still has source
consumers; deletion is permitted only after its consumer count reaches zero.
