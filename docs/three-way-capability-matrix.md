# Three-way Capability Matrix

Fresh scan: 2026-08-11. `强/有/部分/无` 表示源码实现深度，不以文件是否存在为判断标准。InfCodeX 证据主要来自 `packages/coding/src`; infcode-dev 来自 `packages/opencode/src` 与 `packages/kilo-indexing/src`; NZ-Coder 来自 `nz_coder/runtime`, `tools`, `lsp`, `state`, `skills.py`, `session_events.py`。

| Domain / Capability | InfCodeX | infcode-dev/OpenCode | NZ-Coder | Gap / Priority |
|---|---|---|---|---|
| Core / Agent Definition | 强 | 强 | 强：immutable declaration/handoff | 对齐 |
| Core / Runner | 强 | 强 | 强：唯一 AgentRunner loop | 对齐 |
| Core / Session | 强 | 强 | 强：durable SessionRuntime | 对齐 |
| Core / Message/Part | 强 | 强 | 强：identity/part/processor | 对齐 |
| Core / Streaming | 强 | 强 | 强 | 对齐 |
| Core / Provider | 强 | 最强：多 provider | 有：OpenAI-compatible + adapters | P1 广度 |
| Core / Retry | 强 | 强 | 强：诊断/backoff/recovery | 对齐 |
| Core / Cancellation | 强 | 强 | 强：model/tool/child | 对齐 |
| Core / Usage | 强 | 强 | 强：RunContext/Session | 对齐 |
| Core / Events | 强 | 强 | 强：RuntimeEvent→SessionEventBus | 本轮对齐 |
| Core / Middleware | 强 | 强 | 有：run/model/tool pipeline | P1 扩展，但边界已对齐 |
| Context / Budget | 强 | 强 | 强：model-aware budget | 对齐 |
| Context / Selection | 强 | 强 | 有：history/service selection | P1 精度 |
| Context / Compaction | 强 | 强 | 强：async retry/summary | 对齐 |
| Context / Micro-compaction | 强 | 强 | 有 | 基本对齐 |
| Context / Large Tool Output | 强 | 强 | 强：projection/persistence | 对齐 |
| Context / History Cleanup | 强 | 强 | 强 | 对齐 |
| Context / Attachment Retention | 强 | 强 | 强 | 对齐 |
| Tools / Registry | 强 | 强 | 强：ContextVar dynamic scope | 对齐 |
| Tools / Definition | 强 | 强 | 强：schema+handler+effect | 对齐 |
| Tools / Tool Search | 强 | 有 | 部分：registry/search，无解锁协议 | P1 |
| Tools / Dynamic Exposure | 最强：planner+hints+unlock | 部分 | 无运行时过滤 | P1，需模型 A/B |
| Tools / Permission | 强 | 强 | 强 | 对齐 |
| Tools / Parallel Tools | 强 | 强 | 强：read parallel | 对齐 |
| Tools / Serial Tools | 强 | 强 | 强：write/unknown serial | 对齐 |
| Tools / Cancellation | 强 | 强 | 强 | 对齐 |
| Tools / Output Projection | 强 | 强 | 强：ToolOutput/processor | 对齐 |
| Tools / Schema Budget | 强 | 强 | 仅测量，无 exposure policy | P1 |
| Tools / MCP Tools | 强 | 强 | 强：stdio/SSE/HTTP/OAuth | 基本对齐 |
| Coding / Read | 强 | 强 | 强 | 对齐 |
| Coding / Write/Edit | 强 | 强 | 强：transaction/change tracker | 对齐 |
| Coding / Patch | 强 | 强 | 强 | 对齐 |
| Coding / Bash | 强 | 强 | 强：policy/cancel | 对齐 |
| Coding / Search | 强 | 强 | 强：grep/ranking | 对齐 |
| Coding / Git | 强 | 强 | 强 | 对齐 |
| Coding / LSP | 强 | 强 | 有：definition/reference/symbol | P1 call hierarchy breadth |
| Coding / Repo Map | 强 | 强 | 有：multi-language map | P1 incremental freshness |
| Coding / Symbol Search | 强 | 强 | 有：LSP + AST | P1 cross-language |
| Coding / Reference Search | 强 | 强 | 有：LSP | 基本对齐 |
| Coding / Call Graph | 强 | 强 | 部分：Python AST | P1 |
| Coding / Code Index | 强 | 最强：Tree-sitter watcher | 部分：cache/map | P1 |
| Coding / Semantic Index | 强 | 最强：embeddings/vector stores | 无通用 index | P1（证据后做） |
| Coding / Changed Scope | 强 | 有 | 强 | 对齐 |
| Coding / Impact Analysis | 强 | 强 | 部分：references/scope | P1 |
| Multi-Agent / Child | 强 | 强 | 强：Session child | 对齐 |
| Multi-Agent / Background | 强 | 强 | 强：same run_subagent chain | 本轮对齐 |
| Multi-Agent / Parallel Child | 强 | 强 | 强：thread/process caps | 对齐 |
| Multi-Agent / Messaging | 强 | 强 | 强：mailbox/cycle guard | 对齐 |
| Multi-Agent / Handoff | 强 | 强 | 强：continuation/as-tool | 对齐 |
| Multi-Agent / Steering | 强 | 强 | 有：message/stop/followup | 基本对齐 |
| Multi-Agent / Resume | 强 | 强 | 强：durable session activation | 本轮 SDK 对齐 |
| Multi-Agent / Lineage | 强 | 强 | 强 | 对齐 |
| Multi-Agent / Worktree | 强 | 强 | 强：scope/conflict/rollback | 对齐 |
| Intelligence / Planning | 强 | 强 | 强 | 对齐 |
| Intelligence / Replanning | 强 | 强 | 强 | 对齐 |
| Intelligence / Reflection | 强 | 强 | 强 | 对齐 |
| Intelligence / Verification | 强 | 强 | 强：gates/evidence | 对齐 |
| Intelligence / Sidecar | 强 | 有 | 强：stall/verifier | 对齐 |
| Intelligence / Stall Detection | 强 | 有 | 强 | 对齐 |
| Intelligence / Recovery | 强 | 强 | 强：diagnostic/retry | 对齐 |
| Intelligence / Guardrail | 强 | 强 | 强：input/output/tool | 对齐 |
| Memory / Persistent | 强 | 有 | 强：workspace/session stores | 对齐 |
| Memory / Extraction | 强 | 部分 | 强 | 对齐 |
| Memory / Proposal | 强 | 部分 | 强 | 对齐 |
| Memory / Review | 强 | 部分 | 强 | 对齐 |
| Memory / Governance | 强 | 部分 | 强：policy/lineage | 对齐 |
| Memory / Safe Apply | 强 | 部分 | 强：reviewed apply | 对齐 |
| Extension / MCP | 强 | 强 | 强 | 基本对齐 |
| Extension / Skills | 强 | 强 | 有：bundled/user/workspace | P1 lifecycle/precedence proof |
| Extension / Skill Registry | 强 | 强 | 有：manifest/catalog | 基本对齐 |
| Extension / Discovery | 强 | 强 | 有 | P1 diagnostics |
| Extension / Loader | 强 | 强 | 有 | 基本对齐 |
| Extension / Workflow | 强 | 强 | 强：DAG/cache/budget/artifact | 对齐 |
| Extension / Plugins | 强 | 强 | 部分：optional packs/MCP | P2 |
| Platform / SDK | 强 | 强 | 强：native run/child/resume | 本轮对齐 |
| Platform / HTTP Runtime | 有 | 强 | 强：HTTP/SSE/session API | 对齐 |
| Platform / CLI | 有 | 强 | 有 | P3 产品体验 |
| Platform / TUI | 部分 | 强 | 有 | P3 |
| Platform / Tracing | 强 | 强 | 强：JSONL/session facts | 对齐 |
| Platform / Metrics | 强 | 强 | 有：usage/eval/trace summary | P2 aggregation |
| Platform / Observability | 强 | 强 | 有：events/trace/model calls | P2 unified dashboards |
| Platform / Evaluation | 强 | 有 | 强：SWE runner/official format | 对齐 |
| Platform / Benchmark | 强 | 有 | 强：SWE + tool exposure | 对齐 |

## 本轮能力决策

只实现了一个经过架构和行为测试支持的 P0/P1 能力：统一 Runtime middleware/event/SDK boundary（同一项架构闭环）。Dynamic Tool Exposure 仅完成 benchmark 与设计，不上线过滤：20/60/120 synthetic 工具 recall@8 均为 1.0，schema 粗估从 1,036 增至 6,275 tokens，检索中位批延迟从 0.245ms 墠至 1.512ms；这证明 schema 成本和检索可行性，但没有证明真实模型任务成功率不下降。

## Skills 深度判断

NZ-Coder 已具 discovery、manifest 解析、workspace/user/bundled scopes、prompt loading 与验证；差距不是“缺 skills.py”，而是 precedence 冲突的系统级验收、加载/卸载生命周期事件、权限结合和真实任务 benchmark。因此列为 P1，不在本阶段继续 cargo-cult。

## Observability 深度判断

运行事件、SessionEventBus、trace、model call、tool progress、child/workflow event、usage/cost 已存在；剩余差距是跨运行聚合和 query surface，不是再建一套事件模型。
