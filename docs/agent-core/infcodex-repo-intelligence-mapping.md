# InfCodeX 与 NZ-Coder Repo Intelligence Core 对照

对照版本：InfCodeX `Tokfinity/InfCodeX@d3a812379b589597347f5be12d5b68477e577f02`；NZ-Coder 基线 `81dd05b4fde8add42f24a3115ca32bf9bb07c133`。

| 参考行为（实际调用点） | 当前 NZ-Coder 行为 | 真实差异 | 修改位置 | 标签 | 验收场景 |
|---|---|---|---|---|---|
| `runSubstrate` 在首轮和 reroute 通过 `buildReasoningExecutionState` 组合 repo context，再交给 prompt builder（InfCodeX `run-substrate.ts:784`, `reasoning-plan-entry.ts:75-93`） | `ProductionPromptBuilder.build()` 每次请求调用 `_repo_retrieval_block()`；`AgentLoop` 另有 `_implementation_bundle_block()` | 主链已接通，但 NZ-Coder 的 retrieval block 在 `turn_count > 1` 丢弃 auto-context | `loop.py:_repo_retrieval_block` | 本项目适配 | 后续回合收到 ready/generation 更新 |
| InfCodeX middleware 组合 caller context、preturn bundle、overview/changed scope、module、impact 和低置信度 guidance（`middleware/repo-intelligence.ts:212-430`） | `RepoRetrievalPolicy.decide()` 只把 accepted 候选格式化为 title/locator/identity/score；`repo_context` 工具本身可返回较丰富 JSON | 自动 prompt 缺少关系、测试线索、来源、freshness、confidence 和精确读取入口 | `retrieval_policy.py:_format_auto_context`，复用 `RepoIntelligenceService` 查询 | 本项目适配 | 未知位置多文件缺陷的请求包含结构化证据 |
| InfCodeX preturn 用有界等待、in-flight promise 复用和 TTL 缓存；ready 结果由后续合法调用消费（`middleware/repo-intelligence.ts:94-210`，`runtime.ts` caches） | `RepoIntelligenceService.prewarm()`、单 worker、generation、query cache、watcher 已存在；policy cache 按 generation，但 loop 首轮限制使后续请求看不到更新 | NZ-Coder 缺少按 readiness/generation/证据指纹决定是否刷新模型可见 block 的衔接 | `loop.py` 增加轻量证据缓存键和刷新判断；不重建 service cache | 本项目适配 | warming→ready、文件修改后 generation 更新 |
| InfCodeX semantic render 明确输出 module/symbol/impact 的位置、依赖/被依赖、入口、测试、freshness、confidence、alternatives（`semantic-render.ts:24-128`） | NZ-Coder service 和 graph 已能提供 module/symbol/changed scope，但自动块不投影这些字段 | 能力已存在，模型默认路径没有获得紧凑可追溯证据 | 复用 service 返回值，在 policy 中做 bounded render | 本项目适配 | 低置信度/同名符号保留 alternatives |
| InfCodeX `repoIntelligenceContext` 仅在 prompt capability section 有值时进入最终 system prompt（`capability-sections.ts:177-184`） | NZ-Coder prompt builder 将动态块注入 system/user 请求并统一预算 | 注入位置已有等价能力；本轮只改内容和刷新，不改 prompt 架构 | `retrieval_policy.py`、`loop.py` | 已有等价能力，无需修改 | 请求中可见 evidence 且预算内 |
| InfCodeX `repoIntelligenceMode=off` 直接返回 caller context；underlying failure best-effort fallback（middleware:218-220, 391-400） | NZ-Coder `off`、`tool-only`、`guidance` 由 runtime override 和 policy 分支控制；service fallback 标记状态 | 语义已有，需确保刷新逻辑不绕过显式模式 | 相关策略回归测试 | 已有等价能力，无需修改 | off/tool-only/guidance、锁/超时退化 |
| InfCodeX contract tests 覆盖 injection、budget，并保留 fault-injection TODO；不等同于真实编码成功率（`__contract-tests__/cap-001-*`） | NZ-Coder 有 policy/service/prompt 测试，但缺少真实 Agent 请求的 warming→ready 和变化刷新回归 | 需要补穿过主 Agent 请求的离线集成测试 | `tests/test_runtime_prompt.py`、`tests/test_retrieval_intelligence.py` | 本项目适配 | 固定任务、工作区、查询与注入统计 |

## 结论

本轮只把已有 structural index/graph 的候选结果接成主 Agent 可用的、带 freshness/confidence/关系/测试/读取入口的 bounded evidence，并按任务、工作区、generation 和内容变化更新。不会把 InfCodeX 的 TypeScript substrate、可选 semantic provider 或 2000ms/60s 数值移植为 NZ-Coder 的新框架或默认配置。
