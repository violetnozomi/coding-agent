# Repo Intelligence Gap Report

## 源码结论

NZ-Coder 已有多语言 repo map、语言检测、grep/ranking、Python AST、LSP definition/reference/workspace symbols、changed scope 与轻量 call information，足以支持中小仓库的 SWE-bench 定向修复；但尚未达到 InfCodeX 或 infcode-dev 的大型仓库持续索引能力。

## Phase 5 detailed ability matrix

| Ability | InfCodeX | OpenCode/Kilo | NZ-Coder after Phase 5 |
|---|---|---|---|
| Persistent file inventory | Strong | Strong | SQLite code index + graph cache |
| Incremental indexing | Strong | Strong watcher | Fingerprint reuse/write refresh; full inventory scan |
| Symbol index | Strong | Strong | Python deep + multi-language extraction |
| Reference index | Strong | Strong | Python persistent + LSP runtime |
| Module graph | Strong | Strong | Python/TS/JS/Go/Rust graph |
| Import graph | Strong | Strong | Persistent resolved internal imports |
| Dependency direction | Strong | Strong | Dependencies + dependents |
| Cyclic dependency | Strong | Partial | Tarjan SCC query |
| Call graph | Strong | Strong | Python AST + LSP, not persistent cross-language |
| Changed scope | Strong | Strong | Git/ChangeTracker + related modules |
| Public API impact | Strong | Partial | Partial heuristics |
| Related tests | Strong | Partial | Verification/impact heuristics, no durable edge |
| Module context | Strong | Strong | Language/dependencies/dependents |
| Symbol context | Strong | Strong | read_symbol/references/callers, not unified |
| Semantic natural-language search | Strong | Strong embeddings | Missing; not falsely claimed |
| Worker/prewarm | Strong | Strong | Missing |
| Cache | Strong | Strong | SQLite + JSON fingerprints |
| Budget | Strong | Strong | File/query/output caps |
| Fallback | Strong analyzers | Strong | lexical/structural/LSP |

| 子能力 | InfCodeX | infcode-dev/OpenCode | NZ-Coder | 差距 |
|---|---|---|---|---|
| 结构索引 | repo-intelligence 服务与语义索引 | Tree-sitter 多语言 parser/scanner | repo map + Python AST | P1：跨语言 symbol/call graph 不完整 |
| 增量更新 | 运行时 refresh/trace | file watcher + indexing orchestrator | cache + 显式 refresh | P1：编辑后缺少统一增量失效 |
| 语义检索 | semantic-index / semantic_lookup | embedding provider + LanceDB/Qdrant | lexical/ranking 为主 | P1：大型仓库概念检索偏弱 |
| 引用/调用 | LSP navigation + repo tools | LSP + index | LSP references，Python AST calls | P1：跨语言 call hierarchy/impact graph |
| changed scope | 一等工具 | git/session integration | 已有 changed scope/ChangeTracker | 基本对齐 |
| 可观测性 | repo trace events | indexing telemetry/status | trace 但缺 index health 指标 | P2 |

## 不在本阶段实现的原因

真正对齐需要 watcher、Tree-sitter query 集、embedding 生命周期、向量存储与索引一致性协议，会引入新的依赖和运维面；这违反本阶段“最多两个 benchmark 证明的能力”和项目“不新增外部依赖”的约束。下一步先用 SWE trace 测量 `No matches`、错误文件选择率和 repo-map stale 次数，再决定优先补增量失效还是跨语言 call hierarchy。
